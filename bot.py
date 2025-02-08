import os
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))  # إضافة متغير المالك

DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0)
Session = sessionmaker(bind=engine)

# ------------------ Database Models ------------------
class GroupSettings(Base):
    __tablename__ = 'group_settings'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    settings = Column(JSON, default={
        'reports': {
            'hourly': True,
            'daily': True,
            'weekly': True,
            'include_losses': True
        },
        'strategies': {
            'golden_cross': True,
            'rsi_divergence': False,
            'macd_crossover': True,
            'volume_spike': True
        },
        'protection': {
            'delete_phones': True,
            'delete_links': True,
            'punishment': 'delete',
            'mute_duration': 1
        },
        'notifications': {
            'price_alerts': True,
            'volume_alerts': True,
            'news_alerts': True
        }
    })

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(4), primary_key=True)
    data = Column(JSON)
    technicals = Column(JSON)
    last_updated = Column(DateTime)

class Opportunity(Base):
    __tablename__ = 'opportunities'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(4))
    strategy = Column(String)
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    stop_profit = Column(Float)
    current_target = Column(Integer, default=0)
    status = Column(String, default='active')
    message_id = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

class PendingApproval(Base):  # نموذج جديد لطلبات الموافقة
    __tablename__ = 'pending_approvals'
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    chat_id = Column(String)
    command = Column(String)
    created_at = Column(DateTime)
    handled = Column(Boolean, default=False)

Base.metadata.create_all(engine)

# ------------------ Utility Functions ------------------
async def delete_message(context, chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception as e:
        logging.error(f"Error deleting message: {e}")

def get_saudi_time():
    return datetime.now(SAUDI_TIMEZONE)

async def is_admin(update: Update):
    chat = update.effective_chat
    if chat.type == 'private':
        return True
    admins = await chat.get_administrators()
    return update.effective_user.id in [admin.user.id for admin in admins]

def is_owner(user_id):  # دالة التحقق من المالك
    return user_id == OWNER_ID

# ------------------ Data Management ------------------
def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first() or StockData(symbol=symbol)
            stock.data = data.to_json()
            
            # Calculate technical indicators
            data['MA50'] = data['Close'].rolling(50).mean()
            data['MA200'] = data['Close'].rolling(200).mean()
            data['RSI'] = calculate_rsi(data)
            data['MACD'] = calculate_macd(data)
            
            stock.technicals = {
                'trend': 'up' if data['MA50'].iloc[-1] > data['MA200'].iloc[-1] else 'down',
                'support': data['Low'].min(),
                'resistance': data['High'].max(),
                'rsi': data['RSI'].iloc[-1],
                'macd': data['MACD'].iloc[-1],
                'volume': data['Volume'].iloc[-1]
            }
            
            stock.last_updated = get_saudi_time()
            session.add(stock)
            session.commit()
    except Exception as e:
        logging.error(f"Error updating {symbol}: {e}")
    finally:
        session.close()

def calculate_rsi(data, period=14):
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(data, fast=12, slow=26, signal=9):
    ema_fast = data['Close'].ewm(span=fast).mean()
    ema_slow = data['Close'].ewm(span=slow).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal).mean()
    return macd - signal_line

# ------------------ Opportunity System ------------------
async def check_opportunities(app: Application):
    session = Session()
    try:
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                continue
                
            data = pd.read_json(stock.data)
            tech = stock.technicals
            
            # Golden Cross Strategy
            if tech['trend'] == 'up' and data['MA50'].iloc[-2] < data['MA200'].iloc[-2]:
                create_opportunity(session, symbol, 'golden_cross', data)
                
            # RSI Divergence Strategy
            if tech['rsi'] < 30 and data['Close'].iloc[-1] < data['Close'].iloc[-2]:
                create_opportunity(session, symbol, 'rsi_divergence', data)
                
            # Volume Spike Strategy
            if tech['volume'] > (data['Volume'].mean() * 2):
                create_opportunity(session, symbol, 'volume_spike', data)
                
            session.commit()
    except Exception as e:
        logging.error(f"Opportunity error: {e}")
    finally:
        session.close()

def create_opportunity(session, symbol, strategy, data):
    entry_price = data['Close'].iloc[-1]
    targets = [entry_price * (1 + (i * 0.05)) for i in range(1, 5)]
    stop_loss = data['Low'].iloc[-2]
    
    opp = Opportunity(
        symbol=symbol,
        strategy=strategy,
        entry_price=entry_price,
        targets=targets,
        stop_loss=stop_loss,
        stop_profit=entry_price * 0.98,
        created_at=get_saudi_time()
    )
    session.add(opp)
    return opp

async def track_targets(app: Application):
    session = Session()
    try:
        opportunities = session.query(Opportunity).filter_by(status='active').all()
        for opp in opportunities:
            stock = session.query(StockData).filter_by(symbol=opp.symbol).first()
            data = pd.read_json(stock.data)
            current_price = data['Close'].iloc[-1]
            
            if current_price >= opp.targets[opp.current_target]:
                await update_opportunity_target(app, opp, current_price)
                
            elif current_price <= opp.stop_loss:
                await close_opportunity(app, opp, 'stopped')
                
            elif current_price >= opp.stop_profit:
                await update_stop_profit(app, opp)
                
    except Exception as e:
        logging.error(f"Tracking error: {e}")
    finally:
        session.close()

async def update_opportunity_target(app, opp, current_price):
    session = Session()
    try:
        opp.current_target += 1
        opp.updated_at = get_saudi_time()
        
        alert_msg = f"""
        🎯 Target {opp.current_target} achieved for {opp.symbol}
        Strategy: {opp.strategy}
        Current Price: {current_price:.2f}
        """
        
        groups = session.query(GroupSettings).all()
        for group in groups:
            if group.settings['strategies'].get(opp.strategy, False):
                await app.bot.send_message(
                    chat_id=group.chat_id,
                    text=alert_msg,
                    reply_to_message_id=opp.message_id
                )
        
        if opp.current_target >= len(opp.targets):
            await close_opportunity(app, opp, 'completed')
            await create_new_targets(app, opp)
            
        session.commit()
    finally:
        session.close()

async def close_opportunity(app, opp, status):
    session = Session()
    try:
        opp.status = status
        session.commit()
        
        status_msg = f"""
        🏁 Opportunity closed for {opp.symbol}
        Status: {'completed' if status == 'completed' else 'stopped'}
        """
        
        await app.bot.send_message(
            chat_id=opp.message_id,
            text=status_msg,
            reply_to_message_id=opp.message_id
        )
    finally:
        session.close()

async def create_new_targets(app, opp):
    session = Session()
    try:
        new_targets = [opp.targets[-1] * (1 + (i * 0.03)) for i in range(1, 4)]
        new_opp = Opportunity(
            symbol=opp.symbol,
            strategy=opp.strategy,
            entry_price=opp.targets[-1],
            targets=new_targets,
            stop_loss=opp.stop_profit,
            created_at=get_saudi_time()
        )
        session.add(new_opp)
        session.commit()
        
        message = f"""
        🚀 New follow-up opportunity for {opp.symbol}
        New targets: {', '.join(map(str, new_targets))}
        """
        
        await app.bot.send_message(
            chat_id=opp.message_id,
            text=message
        )
    finally:
        session.close()

# ------------------ Reporting System ------------------
async def generate_hourly_report(app: Application):
    session = Session()
    try:
        report = await calculate_top_movers(session, 'hourly')
        groups = session.query(GroupSettings).filter_by(settings__reports__hourly=True).all()
        await send_report(app, groups, report)
    finally:
        session.close()

async def generate_daily_report(app: Application):
    session = Session()
    try:
        price_report = await calculate_price_analysis(session)
        volume_report = await calculate_volume_analysis(session)
        full_report = f"{price_report}\n\n{volume_report}"
        groups = session.query(GroupSettings).filter_by(settings__reports__daily=True).all()
        await send_report(app, groups, full_report)
    finally:
        session.close()

async def generate_weekly_report(app: Application):
    session = Session()
    try:
        weekly_report = await calculate_weekly_analysis(session)
        opportunity_report = await calculate_opportunity_performance(session)
        full_report = f"{weekly_report}\n\n{opportunity_report}"
        groups = session.query(GroupSettings).filter_by(settings__reports__weekly=True).all()
        await send_report(app, groups, full_report)
    finally:
        session.close()

async def calculate_top_movers(session, period):
    movers = []
    for symbol in STOCK_SYMBOLS:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        change = (data['Close'].iloc[-1] - data['Open'].iloc[-1]) / data['Open'].iloc[-1] * 100
        movers.append((symbol, change))
    
    movers.sort(key=lambda x: x[1], reverse=True)
    top5 = movers[:5]
    bottom5 = movers[-5:]
    
    report = f"📊 {period.capitalize()} Report:\n"
    report += "\n🏆 Top 5 Companies:\n" + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(top5)]
    )
    report += "\n\n🔻 Bottom 5 Companies:\n" + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(bottom5)]
    )
    return report

async def calculate_price_analysis(session):
    analysis = []
    for symbol in STOCK_SYMBOLS:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        analysis.append({
            'symbol': symbol,
            'open': data['Open'].iloc[-1],
            'close': data['Close'].iloc[-1],
            'change': (data['Close'].iloc[-1] - data['Open'].iloc[-1]) / data['Open'].iloc[-1] * 100
        })
    
    gainers = sorted(analysis, key=lambda x: x['change'], reverse=True)[:5]
    losers = sorted(analysis, key=lambda x: x['change'])[:5]
    
    report = "📈 Daily Report:\n"
    report += "\n📈 Top 5 Companies:\n" + "\n".join(
        [f"{i+1}. {item['symbol']}: {item['change']:.2f}%" for i, item in enumerate(gainers)]
    )
    report += "\n\n📉 Bottom 5 Companies:\n" + "\n".join(
        [f"{i+1}. {item['symbol']}: {item['change']:.2f}%" for i, item in enumerate(losers)]
    )
    
    total_gainers = len([x for x in analysis if x['change'] > 0])
    total_losers = len([x for x in analysis if x['change'] < 0])
    report += f"\n\n📊 Total:\nGainers: {total_gainers}\nLosers: {total_losers}"
    
    return report

async def calculate_volume_analysis(session):
    volumes = []
    for symbol in STOCK_SYMBOLS:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        volumes.append((symbol, data['Volume'].iloc[-1]))
    
    volumes.sort(key=lambda x: x[1], reverse=True)
    report = "\n📊 Volume Analysis:\n" + "\n".join(
        [f"{i+1}. {sym}: {vol:,}" for i, (sym, vol) in enumerate(volumes[:5])]
    )
    return report

async def calculate_weekly_analysis(session):
    analysis = []
    for symbol in STOCK_SYMBOLS:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        weekly_data = data.resample('W').last()
        change = (weekly_data['Close'].iloc[-1] - weekly_data['Open'].iloc[-1]) / weekly_data['Open'].iloc[-1] * 100
        analysis.append((symbol, change))
    
    analysis.sort(key=lambda x: x[1], reverse=True)
    report = "📅 Weekly Report:\n"
    report += "\n🏆 Top 5 Companies:\n" + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(analysis[:5])]
    )
    report += "\n\n🔻 Bottom 5 Companies:\n" + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(analysis[-5:])]
    )
    return report

async def send_report(app, groups, report):
    for group in groups:
        try:
            await app.bot.send_message(
                chat_id=group.chat_id,
                text=report,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logging.error(f"Error sending report: {e}")

# ------------------ Main Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = """
    Welcome to the Advanced Saudi Stock Bot!
    --------------------------
    /settings - Control Panel
    /report - Generate Instant Report
    """
    
    keyboard = [
        [InlineKeyboardButton("Settings ⚙️", callback_data='settings_menu')],
        [InlineKeyboardButton("Support 💬", url='t.me/your_support')]
    ]
    
    await update.message.reply_text(
        welcome_msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not is_owner(user_id):  # التحقق من المالك
        session = Session()
        try:
            # Save approval request
            req = PendingApproval(
                user_id=str(user_id),
                chat_id=str(chat_id),
                command='settings_menu',
                created_at=get_saudi_time()
            )
            session.add(req)
            session.commit()
            
            # Notify owner
            approve_button = InlineKeyboardButton(
                text="✅ Approve", 
                callback_data=f"approve_{req.id}"
            )
            deny_button = InlineKeyboardButton(
                text="❌ Deny", 
                callback_data=f"deny_{req.id}"
            )
            
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"Access request from user: {user_id}\nTime: {req.created_at}",
                reply_markup=InlineKeyboardMarkup([[approve_button, deny_button]])
            )
            
            await update.message.reply_text("📬 Your request has been sent to the owner for approval.")
            return
        finally:
            session.close()
    
    # Rest of original settings_menu code here...

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = Session()
    try:
        if query.data == 'settings_menu':
            await settings_menu(update, context)
        elif query.data == 'report_settings':
            await handle_report_settings(query)
        elif query.data == 'strategy_settings':
            await handle_strategy_settings(query)
        elif query.data == 'close_settings':
            await query.delete_message()
    except Exception as e:
        logging.error(f"Button handler error: {e}")
    finally:
        session.close()

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, req_id = query.data.split('_')
    session = Session()
    
    try:
        req = session.query(PendingApproval).get(int(req_id))
        if not req or req.handled:
            return
        
        req.handled = True
        session.commit()
        
        if action == 'approve':
            # Execute original command
            if req.command == 'settings_menu':
                await settings_menu(update, context)
            
            await context.bot.send_message(
                chat_id=req.chat_id,
                text="✅ Your request has been approved."
            )
        else:
            await context.bot.send_message(
                chat_id=req.chat_id,
                text="❌ Your request has been denied."
            )
            
        await query.edit_message_text(f"Request handled: {action}")
        
    except Exception as e:
        logging.error(f"Approval error: {e}")
    finally:
        session.close()

# ------------------ Scheduler & Main ------------------
async def main():
    application = Application.builder().token(TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CallbackQueryHandler(button_handler, pattern='^settings_menu$|^report_settings$|^strategy_settings$|^close_settings$'))
    application.add_handler(CallbackQueryHandler(handle_approval, pattern='^approve_|^deny_'))  # إضافة handler للموافقة
    
    # Initialize scheduler
    scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(check_opportunities, 'interval', minutes=15, args=[application])
    scheduler.add_job(track_targets, 'interval', minutes=5, args=[application])
    scheduler.add_job(generate_hourly_report, CronTrigger(minute=0), args=[application])
    scheduler.add_job(generate_daily_report, CronTrigger(hour=15, minute=30), args=[application])
    scheduler.add_job(generate_weekly_report, CronTrigger(day_of_week='sun', hour=16), args=[application])
    scheduler.start()
    
    # Start webhook
    await application.initialize()
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        url_path=TOKEN,
        webhook_url=WEBHOOK_URL,
        secret_token='WEBHOOK_SECRET'
    )
    
    logging.info("Bot is running...")
    await asyncio.Event().wait()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    asyncio.run(main())