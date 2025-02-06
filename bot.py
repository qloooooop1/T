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
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
from bs4 import BeautifulSoup
import arabic_reshaper
from bidi.algorithm import get_display
import psycopg2

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']
NEWS_URL = "https://www.argaam.com/ar"

# إصلاح مشكلة PostgreSQL
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
            'golden': True,
            'fibonacci': False,
            'moving_average': True
        },
        'protection': {
            'delete_phones': True,
            'delete_links': True,
            'punishment': 'delete',
            'mute_duration': 1
        },
        'notifications': {
            'interval': 15
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
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    current_target = Column(Integer, default=0)
    status = Column(String, default='active')
    message_id = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

# إعادة إنشاء الجداول
Base.metadata.drop_all(engine)
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
            
            # حساب المؤشرات الفنية
            data['MA50'] = data['Close'].rolling(50).mean()
            data['MA200'] = data['Close'].rolling(200).mean()
            data['RSI'] = calculate_rsi(data)
            
            stock.technicals = {
                'trend': 'صاعد' if data['MA50'].iloc[-1] > data['MA200'].iloc[-1] else 'هابط',
                'support': data['Low'].min(),
                'resistance': data['High'].max(),
                'rsi': data['RSI'].iloc[-1]
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

# ------------------ Protection System ------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(update.effective_chat.id)).first()
        if not group:
            return

        msg = update.message
        text = msg.text or msg.caption or ""
        
        violations = []
        
        # كشف أرقام الهواتف
        if group.settings['protection']['delete_phones']:
            phone_pattern = r'(\+?966|0)?5\d{8}'
            if re.search(phone_pattern, text):
                violations.append('أرقام هواتف')
        
        # كشف الروابط الخارجية
        if group.settings['protection']['delete_links']:
            link_pattern = r'(https?://|t\.me/|wa\.me/)'
            if re.search(link_pattern, text):
                violations.append('روابط خارجية')
        
        if violations:
            await delete_message(context, msg.chat_id, msg.message_id)
            action = group.settings['protection']['punishment']
            admin_msg = arabic_text(f"⚠️ تم حذف رسالة بسبب: {', '.join(violations)}")
            
            if action == 'mute':
                await context.bot.restrict_chat_member(
                    chat_id=msg.chat_id,
                    user_id=msg.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int((get_saudi_time() + timedelta(
                        hours=group.settings['protection']['mute_duration'])).timestamp())
                )
                admin_msg += f"\n⏳ تم كتم العضو لمدة {group.settings['protection']['mute_duration']} ساعة"
            elif action == 'ban':
                await context.bot.ban_chat_member(msg.chat_id, msg.from_user.id)
                admin_msg += "\n🚫 تم حظر العضو"
            
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=admin_msg
            )
    finally:
        session.close()

# ------------------ Technical Analysis ------------------
async def analyze_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text
    if not re.match(r'^\d{4}$', symbol):
        return
    
    session = Session()
    try:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        if not stock:
            await update.message.reply_text(arabic_text("⚠️ رمز السهم غير موجود"))
            return
        
        data = pd.read_json(stock.data)
        tech = stock.technicals
        
        analysis = arabic_text(f"""
        📊 تحليل سهم {symbol}
        ------------------
        السعر الحالي: {data['Close'].iloc[-1]:.2f}
        الاتجاه العام: {tech['trend']}
        الدعم: {tech['support']:.2f}
        المقاومة: {tech['resistance']:.2f}
        مؤشر RSI: {tech['rsi']:.2f}
        """)
        
        await update.message.reply_text(analysis)
    except Exception as e:
        logging.error(f"Analysis error: {e}")
    finally:
        session.close()

# ------------------ Opportunity Management ------------------
async def check_opportunities(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                continue
                
            data = pd.read_json(stock.data)
            if stock.technicals['rsi'] > 70:
                entry_price = data['Close'].iloc[-1]
                targets = [entry_price * (1 - level) for level in [0.05, 0.1, 0.15, 0.2]]
                stop_loss = data['High'].iloc[-2]
                
                opp = Opportunity(
                    symbol=symbol,
                    entry_price=entry_price,
                    targets=targets,
                    stop_loss=stop_loss,
                    created_at=get_saudi_time()
                )
                session.add(opp)
                session.commit()
                
                message = arabic_text(f"""
                🚀 فرصة ذهبية جديدة!
                ------------------
                السهم: {symbol}
                سعر الدخول: {entry_price:.2f}
                الأهداف: {', '.join(map(lambda x: f'{x:.2f}', targets))}
                وقف الخسارة: {stop_loss:.2f}
                """)
                
                groups = session.query(GroupSettings).filter_by(settings__strategies__golden=True).all()
                for group in groups:
                    sent = await context.bot.send_message(
                        chat_id=group.chat_id,
                        text=message
                    )
                    opp.message_id = sent.message_id
                    session.commit()
    finally:
        session.close()

async def track_targets(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        opportunities = session.query(Opportunity).filter_by(status='active').all()
        for opp in opportunities:
            stock = session.query(StockData).filter_by(symbol=opp.symbol).first()
            data = pd.read_json(stock.data)
            current_price = data['Close'].iloc[-1]
            
            if current_price >= opp.targets[opp.current_target]:
                opp.current_target += 1
                opp.updated_at = get_saudi_time()
                
                # إرسال إشعار تحقيق الهدف
                alert_msg = arabic_text(f"""
                🎉 تم تحقيق الهدف {opp.current_target} لـ {opp.symbol}
                السعر الحالي: {current_price:.2f}
                """)
                await context.bot.send_message(
                    chat_id=opp.message_id,
                    text=alert_msg,
                    reply_to_message_id=opp.message_id
                )
                
                if opp.current_target >= len(opp.targets):
                    opp.status = 'completed'
                    completion_msg = arabic_text(f"""
                    🏆 إنجاز! تم تحقيق جميع الأهداف لـ {opp.symbol}
                    الأرباح الإجمالية: {(current_price - opp.entry_price):.2f}
                    """)
                    await context.bot.send_message(
                        chat_id=opp.message_id,
                        text=completion_msg,
                        reply_to_message_id=opp.message_id
                    )
                
                session.commit()
    finally:
        session.close()

# ------------------ Reports ------------------
async def generate_hourly_report(context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        top_gainers = []
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            data = pd.read_json(stock.data)
            change = (data['Close'].iloc[-1] - data['Close'].iloc[-2]) / data['Close'].iloc[-2] * 100
            top_gainers.append((symbol, change))
        
        top_gainers.sort(key=lambda x: x[1], reverse=True)
        report = arabic_text("📈 أعلى 5 شركات ربحاً:\n") + "\n".join(
            [f"{i+1}. {sym}: {chg:.2f}%" for i, (sym, chg) in enumerate(top_gainers[:5])]
        )
        
        groups = session.query(GroupSettings).filter_by(settings__reports__hourly=True).all()
        for group in groups:
            await context.bot.send_message(
                chat_id=group.chat_id,
                text=report
            )
    finally:
        session.close()

# ------------------ Main Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = arabic_text("""
    مرحباً بك في بوت الراصد السعودي!
    --------------------------
    قم بإرسال رمز السهم المكون من 4 أرقام لتحليله
    /settings - لإدارة إعدادات المجموعة
    """)
    
    if update.message.chat.type == 'private':
        await update.message.reply_text(welcome_msg)
    else:
        msg = await update.message.reply_text(
            welcome_msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("فتح الإعدادات ⚙️", callback_data='open_settings')]
            ])
        )
        await asyncio.sleep(30)
        await delete_message(context, update.message.chat_id, msg.message_id)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text(arabic_text("⚠️ هذا الأمر متاح للمشرفين فقط"))
        return
    
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(update.effective_chat.id)).first()
        keyboard = [
            [InlineKeyboardButton(f"التقارير التلقائية {'✅' if group.settings['reports']['hourly'] else '❌'}",
                                callback_data='toggle_reports')],
            [InlineKeyboardButton(f"الفرص الذهبية {'✅' if group.settings['strategies']['golden'] else '❌'}",
                                callback_data='toggle_golden')],
            [InlineKeyboardButton(f"حذف الروابط {'✅' if group.settings['protection']['delete_links'] else '❌'}",
                                callback_data='toggle_links')],
            [InlineKeyboardButton("إغلاق ❌", callback_data='close_settings')]
        ]
        await update.message.reply_text(
            arabic_text("⚙️ إعدادات المجموعة:"),
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
        
        if query.data == 'toggle_reports':
            group.settings['reports']['hourly'] = not group.settings['reports']['hourly']
        elif query.data == 'toggle_golden':
            group.settings['strategies']['golden'] = not group.settings['strategies']['golden']
        elif query.data == 'toggle_links':
            group.settings['protection']['delete_links'] = not group.settings['protection']['delete_links']
        elif query.data == 'close_settings':
            await query.delete_message()
            return
        
        session.commit()
        keyboard = [
            [InlineKeyboardButton(f"التقارير التلقائية {'✅' if group.settings['reports']['hourly'] else '❌'}",
                                callback_data='toggle_reports')],
            [InlineKeyboardButton(f"الفرص الذهبية {'✅' if group.settings['strategies']['golden'] else '❌'}",
                                callback_data='toggle_golden')],
            [InlineKeyboardButton(f"حذف الروابط {'✅' if group.settings['protection']['delete_links'] else '❌'}",
                                callback_data='toggle_links')],
            [InlineKeyboardButton("إغلاق ❌", callback_data='close_settings')]
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    finally:
        session.close()

# ------------------ Main Application ------------------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_menu))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_stock))
    application.add_handler(MessageHandler(filters.ALL, handle_messages))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(check_opportunities, 'interval', minutes=15)
    scheduler.add_job(track_targets, 'interval', minutes=5)
    scheduler.add_job(generate_hourly_report, CronTrigger(minute=0))
    scheduler.start()
    
    # Webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN,
        secret_token='WEBHOOK_SECRET'
    )

if __name__ == "__main__":
    main()