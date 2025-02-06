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
import psycopg2
from technical_analysis import calculate_all_indicators  # ملف مخصص للتحليل الفني

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']  # قائمة رموز الأسهم

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
            'strategy_1': True,
            'strategy_2': False,
            'strategy_3': True
        }
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
    historical_data = Column(JSON)
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
    updated_at = Column(DateTime)

class PerformanceReport(Base):
    __tablename__ = 'performance_reports'
    id = Column(Integer, primary_key=True)
    week_number = Column(Integer)
    total_opportunities = Column(Integer)
    successful = Column(Integer)
    ongoing = Column(Integer)
    closed = Column(Integer)
    profit_loss = Column(Float)

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

# ------------------ Enhanced Stock Management ------------------
def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first() or StockData(symbol=symbol)
            stock.data = data.to_json()
            stock.last_updated = get_saudi_time()
            session.add(stock)
            session.commit()
            session.close()
    except Exception as e:
        logging.error(f"Error updating {symbol}: {e}")

# ------------------ Advanced Technical Analysis ------------------
def detect_strategies(symbol):
    session = Session()
    stock = session.query(StockData).filter_by(symbol=symbol).first()
    data = pd.read_json(stock.data)
    
    indicators = calculate_all_indicators(data)
    opportunities = []
    
    # Strategy 1: RSI Overbought
    if indicators['rsi'][-1] > 70:
        opportunities.append(create_opportunity(symbol, data, 'rsi'))
    
    # Strategy 2: Fibonacci Breakout
    if data['Close'][-1] > indicators['fib_levels'][0.618]:
        opportunities.append(create_opportunity(symbol, data, 'fibonacci'))
    
    # Strategy 3: Moving Average Crossover
    if indicators['ma50'][-1] > indicators['ma200'][-1]:
        opportunities.append(create_opportunity(symbol, data, 'ma_crossover'))
    
    session.close()
    return opportunities

def create_opportunity(symbol, data, strategy_type):
    entry_price = data['Close'][-1]
    if strategy_type == 'rsi':
        targets = [entry_price * (1 - level) for level in [0.05, 0.1, 0.15, 0.2]]
        stop_loss = data['High'][-2]
    elif strategy_type == 'fibonacci':
        targets = [entry_price * (1 + level) for level in [0.236, 0.382, 0.5, 0.618]]
        stop_loss = data['Low'][-2]
    else:
        targets = [entry_price * (1 + level) for level in [0.1, 0.2, 0.3, 0.4]]
        stop_loss = data['Low'][-2]
    
    return Opportunity(
        symbol=symbol,
        entry_price=entry_price,
        targets=targets,
        stop_loss=stop_loss,
        created_at=get_saudi_time()
    )

# ------------------ Performance Tracking ------------------
def generate_performance_report():
    session = Session()
    week_number = datetime.now().isocalendar()[1]
    
    report = PerformanceReport(
        week_number=week_number,
        total_opportunities=0,
        successful=0,
        ongoing=0,
        closed=0,
        profit_loss=0.0
    )
    
    opportunities = session.query(Opportunity).all()
    for opp in opportunities:
        report.total_opportunities += 1
        if opp.status == 'closed':
            report.closed += 1
            report.profit_loss += (opp.targets[-1] - opp.entry_price)
        elif opp.status == 'active':
            report.ongoing += 1
    
    session.add(report)
    session.commit()
    session.close()

# ------------------ Message Handlers ------------------
async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("الإستراتيجيات الفنية", callback_data='strategies')],
        [InlineKeyboardButton("التقارير الأسبوعية", callback_data='reports')],
        [InlineKeyboardButton("إدارة التنبيهات", callback_data='alerts')]
    ]
    
    await update.message.reply_text(
        arabic_text("⚙️ لوحة التحكم المتقدمة:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def strategy_settings(update: Update, context: CallbackContext):
    query = update.callback_query
    session = Session()
    group = session.query(GroupSettings).filter_by(chat_id=query.message.chat.id).first()
    
    keyboard = []
    for strategy, status in group.settings['strategies'].items():
        text = f"{'✅' if status else '❌'} {strategy.replace('_', ' ').title()}"
        callback_data = f"toggle_strategy:{strategy}"
        keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])
    
    await query.edit_message_text(
        arabic_text("الإستراتيجيات الفنية النشطة:"),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ------------------ Scheduled Tasks ------------------
async def daily_market_scan(context):
    session = Session()
    for symbol in STOCK_SYMBOLS:
        update_stock_data(symbol)
        opportunities = detect_strategies(symbol)
        for opp in opportunities:
            existing = session.query(Opportunity).filter_by(symbol=symbol, status='active').first()
            if not existing:
                session.add(opp)
    session.commit()
    session.close()

async def send_weekly_report(context):
    generate_performance_report()
    session = Session()
    report = session.query(PerformanceReport).order_by(PerformanceReport.id.desc()).first()
    
    message = arabic_text(f"""
    📊 تقرير أداء الأسبوع #{report.week_number}
    --------------------------
    الفرص المفتوحة: {report.ongoing}
    الفرص المغلقة: {report.closed}
    الأرباح/الخسائر الإجمالية: {report.profit_loss:.2f}%
    """)
    
    groups = session.query(GroupSettings).filter_by(settings__reports=True).all()
    for group in groups:
        await context.bot.send_message(group.chat_id, message)
    
    session.close()

# ------------------ Main Application ------------------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", handle_settings))
    application.add_handler(CommandHandler("settings", handle_settings))
    application.add_handler(CallbackQueryHandler(strategy_settings, pattern='^strategies'))
    
    # Webhook Configuration
    application.run_webhook(
    listen="0.0.0.0",
    port=int(os.environ.get('PORT', 5000)),
    webhook_url=WEBHOOK_URL,
    url_path=TOKEN
)
    
    # Scheduler
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(daily_market_scan, CronTrigger(hour=18, minute=0))
    scheduler.add_job(send_weekly_report, CronTrigger(day_of_week='sun', hour=8))
    scheduler.start()

if __name__ == "__main__":
    main()