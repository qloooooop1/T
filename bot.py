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

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS_URL = "https://api.example.com/saudi_stocks"  # Replace with actual source
ANNOUNCEMENTS_URL = "https://example.com/announcements"
HADITHS_DATASET = [
    {"text": "حديث 1...", "day_type": "general"},
    # ... Add 30+ Hadiths
]

# Initialize database
Base = declarative_base()
engine = create_engine(os.environ.get('DATABASE_URL'))
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
    report_times = Column(JSON, default={
        'hourly': True,
        'daily': True,
        'weekly': True
    })

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(4), primary_key=True)
    data = Column(JSON)
    historical_highs = Column(JSON)
    historical_lows = Column(JSON)
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

class HadithSchedule(Base):
    __tablename__ = 'hadith_schedule'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    schedule = Column(JSON, default={
        'general': {'enabled': True, 'times': ['08:00', '12:00', '18:00']},
        'friday': {'enabled': True, 'times': ['07:00', '15:00']}
    })

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

# ------------------ Data Management ------------------
def update_stock_symbols():
    try:
        response = requests.get(STOCK_SYMBOLS_URL)
        symbols = response.json()
        session = Session()
        for symbol in symbols:
            if not session.query(StockData).filter_by(symbol=symbol).first():
                session.add(StockData(symbol=symbol))
        session.commit()
        session.close()
    except Exception as e:
        logging.error(f"Error updating symbols: {e}")

def refresh_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1wk")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first()
            stock.data = data.to_json()
            
            # Update historical records
            stock.historical_highs = {
                'monthly': data['High'].resample('M').max().to_dict(),
                'yearly': data['High'].resample('Y').max().to_dict(),
                'all_time': data['High'].max()
            }
            
            stock.historical_lows = {
                'monthly': data['Low'].resample('M').min().to_dict(),
                'yearly': data['Low'].resample('Y').min().to_dict(),
                'all_time': data['Low'].min()
            }
            
            session.commit()
            session.close()
    except Exception as e:
        logging.error(f"Error updating {symbol}: {e}")

# ------------------ Technical Analysis ------------------
def calculate_rsi(data, period=14):
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def detect_golden_opportunity(symbol):
    session = Session()
    stock = session.query(StockData).filter_by(symbol=symbol).first()
    if not stock:
        return None
    
    data = pd.read_json(stock.data)
    rsi = calculate_rsi(data)
    
    if rsi[-1] > 70 and rsi[-2] <= 70:
        entry_price = data['Close'][-1]
        fib_levels = calculate_fib_levels(data['High'].max(), data['Low'].min())
        targets = [round(entry_price * (1 - level), 2) for level in [0.236, 0.382, 0.5, 0.618]]
        stop_loss = data['High'][-2]
        
        opportunity = GoldenOpportunity(
            symbol=symbol,
            entry_price=entry_price,
            targets=targets,
            stop_loss=stop_loss
        )
        session.add(opportunity)
        session.commit()
        return opportunity
    return None

# ------------------ Message Handlers ------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    chat_id = str(update.effective_chat.id)
    group_settings = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
    
    # Message filtering
    if group_settings.settings['delete_messages']:
        if re.search(r'(\+?\d{10,13}|https?://)', update.message.text):
            await delete_message(context, chat_id, update.message.message_id)
            return
            
        if group_settings.settings['group_locked'] and not is_admin(update):
            await delete_message(context, chat_id, update.message.message_id)
            await context.bot.send_message(
                chat_id,
                arabic_text("⚠️ النشر مقفل حالياً أثناء ساعات التداول"),
                reply_to_message_id=update.message.message_id
            )
            return
    
    # Stock symbol analysis
    if re.match(r'^\d{4}$', update.message.text):
        symbol = update.message.text
        stock = session.query(StockData).filter_by(symbol=symbol).first()
        if stock:
            analysis = generate_stock_analysis(stock)
            await update.message.reply_text(arabic_text(analysis), parse_mode='HTML')

# ------------------ Scheduled Tasks ------------------
async def send_periodic_reports(context):
    session = Session()
    groups = session.query(GroupSettings).all()
    
    for group in groups:
        if group.settings['reports']:
            report = generate_market_report()
            await context.bot.send_message(
                group.chat_id,
                arabic_text(report),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

async def check_opportunities(context):
    session = Session()
    symbols = session.query(StockData.symbol).all()
    
    for symbol in symbols:
        opportunity = detect_golden_opportunity(symbol[0])
        if opportunity:
            groups = session.query(GroupSettings).filter_by(settings__golden_opportunities=True).all()
            message = format_opportunity_message(opportunity)
            for group in groups:
                sent = await context.bot.send_message(
                    group.chat_id,
                    arabic_text(message),
                    parse_mode='HTML'
                )
                opportunity.message_id = sent.message_id
                session.commit()

# ------------------ Admin Controls ------------------
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(':')
    
    session = Session()
    group = session.query(GroupSettings).filter_by(chat_id=query.message.chat_id).first()
    
    if data[0] == 'toggle':
        group.settings[data[1]] = not group.settings[data[1]]
    elif data[0] == 'set_time':
        group.report_times[data[1]] = data[2]
    
    session.commit()
    await update_settings_message(query.message, group)

async def update_settings_message(message, group):
    keyboard = []
    for setting, value in group.settings.items():
        text = f"{'✅' if value else '❌'} {setting.replace('_', ' ').title()}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f'toggle:{setting}')])
    
    await message.edit_text(
        arabic_text("⚙️ إعدادات المجموعة:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ------------------ Main Setup ------------------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(settings_callback))
    
    # Webhook setup
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        url_path=TOKEN,
        webhook_url=WEBHOOK_URL
    )
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(send_periodic_reports, CronTrigger(hour="*/1"))
    scheduler.add_job(check_opportunities, CronTrigger(day_of_week="sun-thu", hour="9-15", minute="*/15"))
    scheduler.start()

if __name__ == "__main__":
    main()
