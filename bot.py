import os
import re
import logging
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
import psycopg2  # ضروري لPostgreSQL

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']  # قائمة افتراضية يمكن تحديثها

# إصلاح مشكلة PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL)
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
        'delete_messages': True
    })
    report_times = Column(JSON, default={'hourly': True, 'daily': True, 'weekly': True})

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(4), primary_key=True)
    data = Column(JSON)
    historical_data = Column(JSON)
    last_updated = Column(DateTime)

class GoldenOpportunity(Base):
    __tablename__ = 'golden_opportunities'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(4))
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    current_target = Column(Integer, default=0)
    message_id = Column(Integer)
    status = Column(String, default='active')

class TechnicalPattern(Base):
    __tablename__ = 'technical_patterns'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(4))
    pattern_type = Column(String)
    targets = Column(JSON)
    stop_loss = Column(Float)
    detected_at = Column(DateTime)

class Hadith(Base):
    __tablename__ = 'hadiths'
    id = Column(Integer, primary_key=True)
    text = Column(Text)
    day_type = Column(String)

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

def is_trading_time():
    now = get_saudi_time()
    start = now.replace(hour=TRADING_HOURS['start'][0], minute=TRADING_HOURS['start'][1], second=0)
    end = now.replace(hour=TRADING_HOURS['end'][0], minute=TRADING_HOURS['end'][1], second=0)
    return start <= now <= end

# ------------------ Enhanced Stock Data Management ------------------
def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            if not stock:
                stock = StockData(symbol=symbol)
            
            # تحديث البيانات التاريخية
            historical = {
                'daily_high': data['High'].max(),
                'daily_low': data['Low'].min(),
                'weekly_high': data['High'].resample('W').max().to_dict(),
                'all_time_high': data['High'].cummax().iloc[-1]
            }
            
            stock.data = data.to_json()
            stock.historical_data = historical
            stock.last_updated = get_saudi_time()
            session.add(stock)
            session.commit()
            session.close()
    except Exception as e:
        logging.error(f"Error updating {symbol}: {e}")

# ------------------ Advanced Technical Analysis ------------------
def calculate_rsi(data, period=14):
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def detect_historical_extremes(symbol):
    session = Session()
    stock = session.query(StockData).filter_by(symbol=symbol).first()
    data = pd.read_json(stock.data)
    
    current_price = data['Close'].iloc[-1]
    alerts = []
    
    if current_price >= stock.historical_data['all_time_high']:
        alerts.append('سجل أعلى مستوى تاريخي جديد! 📈')
    
    # إضافة تنبيهات أخرى...
    return alerts

# ------------------ Dynamic Settings Menu ------------------
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("التقارير التلقائية", callback_data='settings:reports')],
        [InlineKeyboardButton("الفرص الذهبية", callback_data='settings:golden')],
        [InlineKeyboardButton("الإشعارات الفورية", callback_data='settings:alerts')],
        [InlineKeyboardButton("إدارة المجموعة", callback_data='settings:group')]
    ]
    
    await update.message.reply_text(
        arabic_text("⚙️ لوحة التحكم المتقدمة:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def settings_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    action = query.data.split(':')[1]
    
    session = Session()
    group = session.query(GroupSettings).filter_by(chat_id=query.message.chat.id).first()
    
    new_status = not group.settings[action]
    group.settings[action] = new_status
    
    session.commit()
    session.close()
    
    await query.answer(f"تم {'تفعيل' if new_status else 'إيقاف'} الميزة بنجاح")
    await update_settings_display(query.message, group.settings)

# ------------------ Enhanced Message Templates ------------------
def format_stock_alert(symbol, alert_type):
    templates = {
        'all_time_high': "🔥 {symbol} سجل أعلى مستوى تاريخي جديد!",
        'weekly_high': "📈 {symbol} وصل أعلى مستوى أسبوعي",
        'rsi_break': "🚨 إشارة بيع قوية لـ {symbol} (RSI فوق 70)"
    }
    return arabic_text(templates[alert_type].format(symbol=symbol))

# ------------------ Main Bot Setup ------------------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CallbackQueryHandler(settings_handler, pattern='^settings:'))
    
    # Webhook configuration
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN
    )
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(check_market_alerts, 'interval', minutes=5)
    scheduler.add_job(send_daily_reports, CronTrigger(hour=18, minute=0))
    scheduler.start()

if __name__ == "__main__":
    main()