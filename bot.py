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
            'punishment': 'delete'  # delete/mute/ban
        },
        'notifications': {
            'interval': 15  # دقائق
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
    status = Column(String, default='active')
    created_at = Column(DateTime)
    profit = Column(Float, default=0.0)

# إعادة إنشاء الجداول
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

# ------------------ Utility Functions ------------------
def arabic_text(text):
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)

async def delete_message(chat_id, message_id):
    try:
        await application.bot.delete_message(chat_id, message_id)
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

# ------------------ Enhanced Protection System ------------------
async def message_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(update.effective_chat.id)).first()
        if not group:
            return

        text = update.message.text
        if not text:
            return

        # كشف المخالفات
        violations = []
        if group.settings['protection']['delete_phones'] and re.search(r'(\+?\d{10,13}|۰۹|٠٩)', text):
            violations.append('رقم هاتف')
        
        if group.settings['protection']['delete_links'] and re.search(r'(https?://|t\.me|wa\.me)', text):
            violations.append('روابط خارجية')

        if violations:
            await delete_message(update.effective_chat.id, update.message.message_id)
            action = group.settings['protection']['punishment']
            message = arabic_text(f"⚠️ تم حذف الرسالة بسبب: {', '.join(violations)}")
            
            if action == 'mute':
                await context.bot.restrict_chat_member(
                    chat_id=update.effective_chat.id,
                    user_id=update.effective_user.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        until_date=int((get_saudi_time() + timedelta(hours=1)).timestamp())
                    )
                )
                message += "\n⏳ تم تقييد العضو لمدة ساعة"
            elif action == 'ban':
                await context.bot.ban_chat_member(
                    chat_id=update.effective_chat.id,
                    user_id=update.effective_user.id
                )
                message += "\n🚫 تم حظر العضو"
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=message
            )
    finally:
        session.close()

# ------------------ Advanced Technical Analysis ------------------
def calculate_technical_indicators(symbol):
    session = Session()
    try:
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        data = pd.read_json(stock.data)
        
        # حساب المؤشرات
        data['MA50'] = data['Close'].rolling(50).mean()
        data['MA200'] = data['Close'].rolling(200).mean()
        data['RSI'] = calculate_rsi(data)
        
        # تحديد الاتجاه
        trend = "صاعد" if data['MA50'].iloc[-1] > data['MA200'].iloc[-1] else "هابط"
        
        # تحديث البيانات
        stock.technicals = {
            'trend': trend,
            'support': data['Low'].min(),
            'resistance': data['High'].max(),
            'rsi': data['RSI'].iloc[-1]
        }
        session.commit()
        
        return stock.technicals
    except Exception as e:
        logging.error(f"Technical analysis error: {e}")
    finally:
        session.close()

# ------------------ Dynamic Settings Menu ------------------
async def build_settings_menu(group):
    keyboard = [
        [InlineKeyboardButton(f"التقارير {'✅' if group.settings['reports']['hourly'] else '❌'}",
                             callback_data='toggle_reports')],
        [InlineKeyboardButton(f"الحماية {'✅' if group.settings['protection']['delete_phones'] else '❌'}",
                             callback_data='toggle_protection')],
        [InlineKeyboardButton("إدارة الاستراتيجيات", callback_data='strategies_menu')],
        [InlineKeyboardButton("إعدادات العقوبات", callback_data='punishment_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    session = Session()
    try:
        group = session.query(GroupSettings).filter_by(chat_id=str(query.message.chat.id)).first()
        
        if query.data == 'toggle_reports':
            group.settings['reports']['hourly'] = not group.settings['reports']['hourly']
        elif query.data == 'toggle_protection':
            group.settings['protection']['delete_phones'] = not group.settings['protection']['delete_phones']
        
        session.commit()
        await query.edit_message_reply_markup(await build_settings_menu(group))
    finally:
        session.close()

# ------------------ Main Application ------------------
def main():
    global application
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_menu))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_filter))
    application.add_handler(CallbackQueryHandler(settings_handler))
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(lambda: asyncio.run(send_hourly_report()), CronTrigger(minute=0))
    scheduler.start()
    
    # Webhook Setup
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN,
        secret_token='WEBHOOK_SECRET'
    )

if __name__ == "__main__":
    main()