import os
import re
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import *
from telegram.ext import *
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
from bs4 import BeautifulSoup
import arabic_reshaper
from bidi.algorithm import get_display
import psycopg2
import json
from functools import partial

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']
NEWS_URL = "https://www.argaam.com/ar"

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

Base.metadata.create_all(engine)

# ------------------ Utility Functions ------------------
def arabic_text(text):
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)

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

# ------------------ Data Management ------------------
def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first() or StockData(symbol=symbol)
            stock.data = data.to_json()
            
            data['MA50'] = data['Close'].rolling(50).mean()
            data['MA200'] = data['Close'].rolling(200).mean()
            data['RSI'] = calculate_rsi(data)
            data['MACD'] = calculate_macd(data)
            
            stock.technicals = {
                'trend': 'صاعد' if data['MA50'].iloc[-1] > data['MA200'].iloc[-1] else 'هابط',
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
async def check_opportunities(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                continue
                
            data = pd.read_json(stock.data)
            tech = stock.technicals
            
            if tech['trend'] == 'صاعد' and data['MA50'].iloc[-2] < data['MA200'].iloc[-2]:
                create_opportunity(session, symbol, 'golden_cross', data)
                
            if tech['rsi'] < 30 and data['Close'].iloc[-1] < data['Close'].iloc[-2]:
                create_opportunity(session, symbol, 'rsi_divergence', data)
                
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

async def track_targets(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        opportunities = session.query(Opportunity).filter_by(status='active').all()
        for opp in opportunities:
            stock = session.query(StockData).filter_by(symbol=opp.symbol).first()
            data = pd.read_json(stock.data)
            current_price = data['Close'].iloc[-1]
            
            if current_price >= opp.targets[opp.current_target]:
                await update_opportunity_target(context, opp, current_price)
                
            elif current_price <= opp.stop_loss:
                await close_opportunity(context, opp, 'stopped')
                
            elif current_price >= opp.stop_profit:
                await update_stop_profit(context, opp)
                
    except Exception as e:
        logging.error(f"Tracking error: {e}")
    finally:
        session.close()

async def update_opportunity_target(context, opp, current_price):
    session = Session()
    try:
        opp.current_target += 1
        opp.updated_at = get_saudi_time()
        
        alert_msg = arabic_text(f"""
        🎯 تم تحقيق الهدف {opp.current_target} لـ {opp.symbol}
        الاستراتيجية: {opp.strategy}
        السعر الحالي: {current_price:.2f}
        """)
        
        groups = session.query(GroupSettings).all()
        for group in groups:
            if group.settings['strategies'].get(opp.strategy, False):
                await context.bot.send_message(
                    chat_id=group.chat_id,
                    text=alert_msg,
                    reply_to_message_id=opp.message_id
                )
        
        if opp.current_target >= len(opp.targets):
            await close_opportunity(context, opp, 'completed')
            await create_new_targets(context, opp)
            
        session.commit()
    finally:
        session.close()

async def close_opportunity(context, opp, status):
    session = Session()
    try:
        opp.status = status
        session.commit()
        
        status_msg = arabic_text(f"""
        🏁 إنتهاء الفرصة لـ {opp.symbol}
        الحالة: {'مكتملة' if status == 'completed' else 'متوقفة'}
        """)
        
        await context.bot.send_message(
            chat_id=opp.message_id,
            text=status_msg,
            reply_to_message_id=opp.message_id
        )
    finally:
        session.close()

async def create_new_targets(context, opp):
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
        
        message = arabic_text(f"""
        🚀 فرصة متابعة جديدة لـ {opp.symbol}
        الأهداف الجديدة: {', '.join(map(str, new_targets))}
        """)
        
        await context.bot.send_message(
            chat_id=opp.message_id,
            text=message
        )
    finally:
        session.close()

# ------------------ Reporting System ------------------
async def generate_hourly_report(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        report = await calculate_top_movers(session, 'hourly')
        groups = session.query(GroupSettings).filter_by(settings__reports__hourly=True).all()
        await send_report(context, groups, report)
    finally:
        session.close()

async def generate_daily_report(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        price_report = await calculate_price_analysis(session)
        volume_report = await calculate_volume_analysis(session)
        full_report = f"{price_report}\n\n{volume_report}"
        groups = session.query(GroupSettings).filter_by(settings__reports__daily=True).all()
        await send_report(context, groups, full_report)
    finally:
        session.close()

async def generate_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        weekly_report = await calculate_weekly_analysis(session)
        opportunity_report = await calculate_opportunity_performance(session)
        full_report = f"{weekly_report}\n\n{opportunity_report}"
        groups = session.query(GroupSettings).filter_by(settings__reports__weekly=True).all()
        await send_report(context, groups, full_report)
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
    
    report = arabic_text(f"📊 تقرير {period}:\n")
    report += arabic_text("\n🏆 أعلى 5 شركات:\n") + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(top5)]
    )
    report += arabic_text("\n\n🔻 أقل 5 شركات:\n") + "\n".join(
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
    
    report = arabic_text("📈 التقرير اليومي:\n")
    report += arabic_text("\n📈 أعلى 5 شركات:\n") + "\n".join(
        [f"{i+1}. {item['symbol']}: {item['change']:.2f}%" for i, item in enumerate(gainers)]
    )
    report += arabic_text("\n\n📉 أقل 5 شركات:\n") + "\n".join(
        [f"{i+1}. {item['symbol']}: {item['change']:.2f}%" for i, item in enumerate(losers)]
    )
    
    total_gainers = len([x for x in analysis if x['change'] > 0])
    total_losers = len([x for x in analysis if x['change'] < 0])
    report += arabic_text(f"\n\n📊 الإجمالي:\nالرابحون: {total_gainers}\nالخاسرون: {total_losers}")
    
    return report

async def calculate_volume_analysis(session):
    volumes = []
    for symbol in STOCK_SYMBOLS:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        volumes.append((symbol, data['Volume'].iloc[-1]))
    
    volumes.sort(key=lambda x: x[1], reverse=True)
    report = arabic_text("\n📊 تحليل الحجم:\n") + "\n".join(
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
    report = arabic_text("📅 التقرير الأسبوعي:\n")
    report += arabic_text("\n🏆 أعلى 5 شركات:\n") + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(analysis[:5])]
    )
    report += arabic_text("\n\n🔻 أقل 5 شركات:\n") + "\n".join(
        [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(analysis[-5:])]
    )
    return report

async def send_report(context, groups, report):
    for group in groups:
        try:
            await context.bot.send_message(
                chat_id=group.chat_id,
                text=report
            )
        except Exception as e:
            logging.error(f"Error sending report: {e}")

# ------------------ Main Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = arabic_text("""
    مرحباً بك في بوت الراصد السعودي المتقدم!
    --------------------------
    /settings - لوحة التحكم الشاملة
    /report - إنشاء تقرير فوري
    """)
    
    keyboard = [
        [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings')],
        [InlineKeyboardButton("الدعم الفني 💬", url='t.me/your_support')]
    ]
    
    await update.message.reply_text(
        welcome_msg,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(arabic_text("⚠️ هذا الأمر للمشرفين فقط"))
        return
    
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(update.effective_chat.id)).first()
        
        keyboard = [
            [InlineKeyboardButton("إدارة التقارير 📊", callback_data='report_settings')],
            [InlineKeyboardButton("إدارة الاستراتيجيات 🛠️", callback_data='strategy_settings')],
            [InlineKeyboardButton("إغلاق ❌", callback_data='close_settings')]
        ]
        
        await update.message.reply_text(
            arabic_text("⚙️ لوحة التحكم الرئيسية:"),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    finally:
        session.close()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(query.message.chat.id)).first()
        
        if query.data == 'settings':
            await settings_menu(update, context)
        elif query.data == 'report_settings':
            await handle_report_settings(query, group)
        elif query.data == 'strategy_settings':
            await handle_strategy_settings(query, group)
        elif query.data.startswith('toggle_'):
            await toggle_setting(query, group)
        elif query.data == 'close_settings':
            await query.delete_message()
            
    finally:
        session.close()

async def handle_report_settings(query, group):
    keyboard = [
        [InlineKeyboardButton(f"التقارير الساعية {'✅' if group.settings['reports']['hourly'] else '❌'}",
                            callback_data='toggle_reports_hourly')],
        [InlineKeyboardButton(f"التقارير اليومية {'✅' if group.settings['reports']['daily'] else '❌'}",
                            callback_data='toggle_reports_daily')],
        [InlineKeyboardButton(f"التقارير الأسبوعية {'✅' if group.settings['reports']['weekly'] else '❌'}",
                            callback_data='toggle_reports_weekly')],
        [InlineKeyboardButton("العودة ←", callback_data='settings')]
    ]
    await query.edit_message_text(
        arabic_text("⚙️ إعدادات التقارير:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_strategy_settings(query, group):
    keyboard = [
        [InlineKeyboardButton(f"التقاطع الذهبي {'✅' if group.settings['strategies']['golden_cross'] else '❌'}",
                            callback_data='toggle_golden_cross')],
        [InlineKeyboardButton(f"الانفراج RSI {'✅' if group.settings['strategies']['rsi_divergence'] else '❌'}",
                            callback_data='toggle_rsi_divergence')],
        [InlineKeyboardButton(f"زيادة الحجم {'✅' if group.settings['strategies']['volume_spike'] else '❌'}",
                            callback_data='toggle_volume_spike')],
        [InlineKeyboardButton("العودة ←", callback_data='settings')]
    ]
    await query.edit_message_text(
        arabic_text("⚙️ إعدادات الاستراتيجيات:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def toggle_setting(query, group):
    setting_map = {
        'toggle_reports_hourly': ('reports', 'hourly'),
        'toggle_reports_daily': ('reports', 'daily'),
        'toggle_reports_weekly': ('reports', 'weekly'),
        'toggle_golden_cross': ('strategies', 'golden_cross'),
        'toggle_rsi_divergence': ('strategies', 'rsi_divergence'),
        'toggle_volume_spike': ('strategies', 'volume_spike')
    }
    
    category, setting = setting_map[query.data]
    group.settings[category][setting] = not group.settings[category][setting]
    session.commit()
    
    await query.answer(f"تم {'تفعيل' if group.settings[category][setting] else 'تعطيل'} الإعداد")

# ------------------ Scheduler Setup ------------------
def schedule_jobs(scheduler, application):
    scheduler.add_job(
        check_opportunities,
        'interval',
        minutes=15,
        args=[application.context],
        timezone=SAUDI_TIMEZONE
    )
    scheduler.add_job(
        track_targets,
        'interval',
        minutes=5,
        args=[application.context],
        timezone=SAUDI_TIMEZONE
    )
    scheduler.add_job(
        generate_hourly_report,
        CronTrigger(minute=0, timezone=SAUDI_TIMEZONE),
        args=[application.context]
    )
    scheduler.add_job(
        generate_daily_report,
        CronTrigger(hour=15, minute=30, timezone=SAUDI_TIMEZONE),
        args=[application.context]
    )
    scheduler.add_job(
        generate_weekly_report,
        CronTrigger(day_of_week='sun', hour=16, timezone=SAUDI_TIMEZONE),
        args=[application.context]
    )

async def main():
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_menu))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Scheduler setup
    scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
    schedule_jobs(scheduler, application)
    scheduler.start()
    
    # Run webhook
    await application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN,
        secret_token='WEBHOOK_SECRET'
    )

if __name__ == "__main__":
    asyncio.run(main())