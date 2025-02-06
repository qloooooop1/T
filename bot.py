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
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN  # إصلاح مسار الويب هوك
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
        'reports': True,
        'golden_opportunities': True,
        'market_alerts': True,
        'group_locked': False,
        'azkar_enabled': True,
        'technical_analysis': True,
        'announcements': True,
        'delete_messages': True,
        'strategies': {
            'الاستراتيجية الذهبية': True,
            'اختراق القمة': False,
            'المتوسطات المتحركة': True
        }
    })

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(4), primary_key=True)
    data = Column(JSON)
    historical_highs = Column(JSON)
    historical_lows = Column(JSON)
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
    created_at = Column(DateTime)
    profit = Column(Float, default=0.0)

class PerformanceReport(Base):
    __tablename__ = 'performance_reports'
    id = Column(Integer, primary_key=True)
    week_number = Column(Integer)
    total_opportunities = Column(Integer)
    company_profits = Column(JSON)
    total_profit = Column(Float)
    ongoing = Column(Integer)
    closed = Column(Integer)

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

def is_admin(update: Update):
    return update.effective_user.id in [admin.user.id for admin in update.effective_chat.get_administrators()]

# ------------------ Data Management ------------------
def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first() or StockData(symbol=symbol)
            stock.data = data.to_json()
            stock.historical_highs = {
                'daily': data['High'].max(),
                'weekly': data['High'].resample('W').max().to_dict(),
                'monthly': data['High'].resample('M').max().to_dict(),
                'yearly': data['High'].resample('Y').max().to_dict()
            }
            stock.last_updated = get_saudi_time()
            session.add(stock)
            session.commit()
            session.close()
    except Exception as e:
        logging.error(f"Error updating {symbol}: {e}")

# ------------------ Technical Analysis ------------------
def calculate_rsi(data, period=14):
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ------------------ Report Generation ------------------
def generate_top5_report(period='daily'):
    session = Session()
    results = []
    try:
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                continue
                
            data = pd.read_json(stock.data)
            if period == 'hourly':
                change = data['Close'].pct_change(periods=1).iloc[-1] * 100
            elif period == 'daily':
                change = data['Close'].pct_change(periods=1).iloc[-1] * 100
            elif period == 'weekly':
                change = data['Close'].pct_change(periods=5).iloc[-1] * 100
            
            results.append({'symbol': symbol, 'change': change})
        
        top5 = sorted(results, key=lambda x: x['change'], reverse=True)[:5]
        bottom5 = sorted(results, key=lambda x: x['change'])[:5]
        return top5, bottom5
    finally:
        session.close()

def format_report(top5, bottom5):
    report = "📈 أعلى 5 شركات:\n"
    for i, item in enumerate(top5, 1):
        report += f"{i}. {item['symbol']}: {item['change']:.2f}%\n"
    
    report += "\n📉 أدنى 5 شركات:\n"
    for i, item in enumerate(bottom5, 1):
        report += f"{i}. {item['symbol']}: {item['change']:.2f}%\n"
    
    return arabic_text(report)

# ------------------ Message Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == 'private':
        await update.message.reply_text(arabic_text("مرحبا! أدخل /settings لإدارة الإعدادات"))
    else:
        await update.message.reply_text(arabic_text("مرحبا! أنا بوت الراصد السعودي 🏅"))

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(arabic_text("⚠️ هذا الأمر متاح للمشرفين فقط"))
        return
    
    keyboard = [
        [InlineKeyboardButton("التقارير التلقائية", callback_data='reports_settings')],
        [InlineKeyboardButton("الفرص الذهبية", callback_data='golden_settings')],
        [InlineKeyboardButton("الإشعارات الفورية", callback_data='alerts_settings')]
    ]
    
    await update.message.reply_text(
        arabic_text("⚙️ لوحة التحكم الرئيسية:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ------------------ Scheduled Tasks ------------------
async def send_hourly_report():
    session = Session()
    try:
        top5, bottom5 = generate_top5_report('hourly')
        message = arabic_text("📊 تقرير أعلى 5 وأدنى 5 شركات (ساعة):\n\n") + format_report(top5, bottom5)
        
        groups = session.query(GroupSettings).filter_by(settings__reports=True).all()
        for group in groups:
            await application.bot.send_message(group.chat_id, message)
    finally:
        session.close()

async def check_real_time_alerts():
    session = Session()
    try:
        for symbol in STOCK_SYMBOLS:
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                continue
                
            data = pd.read_json(stock.data)
            current_price = data['Close'].iloc[-1]
            
            if current_price >= stock.historical_highs['daily']:
                alert = arabic_text(f"🚨 {symbol} سجل أعلى سعر يومي جديد!")
                await send_group_alerts(alert)
    finally:
        session.close()

async def send_group_alerts(message):
    session = Session()
    try:
        groups = session.query(GroupSettings).filter_by(settings__market_alerts=True).all()
        for group in groups:
            await application.bot.send_message(group.chat_id, message)
    finally:
        session.close()

# ------------------ Main Application ------------------
def main():
    global application
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_menu))
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(lambda: asyncio.run(send_hourly_report()), CronTrigger(minute=0))
    scheduler.add_job(lambda: asyncio.run(check_real_time_alerts()), CronTrigger(minute='*/15'))
    scheduler.start()
    
    # إصلاح مسار الويب هوك
    PORT = int(os.environ.get('PORT', 5000))
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN,
        secret_token='WEBHOOK_SECRET'  # إضافة سرية الويب هوك
    )

if __name__ == "__main__":
    main()