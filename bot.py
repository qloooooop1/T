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
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
from bs4 import BeautifulSoup
import arabic_reshaper
from bidi.algorithm import get_display
import psycopg2
from tradingview_ta import TA_Handler, Interval  # مكتبة التحليل الفني

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}

# إعدادات قاعدة البيانات
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0)
Base = declarative_base()
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
        'analysis_limit': 5  # عدد الاستعلامات اليومية
    })
    report_times = Column(JSON, default={'hourly': True, 'daily': True, 'weekly': True})

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(4), primary_key=True)
    data = Column(JSON)
    historical_data = Column(JSON)
    last_updated = Column(DateTime)
    sector = Column(String)

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
    original_message = Column(Text)  # نص الرسالة الأصلية
    strategy_name = Column(String)    # اسم الاستراتيجية

class TechnicalPattern(Base):
    __tablename__ = 'technical_patterns'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(4))
    pattern_type = Column(String)
    targets = Column(JSON)
    stop_loss = Column(Float)
    detected_at = Column(DateTime)

class UserActivity(Base):
    __tablename__ = 'user_activity'
    user_id = Column(Integer, primary_key=True)
    last_analysis = Column(DateTime)
    analysis_count = Column(Integer, default=0)

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
def update_all_stocks():
    symbols = get_all_saudi_stocks()
    for symbol in symbols:
        update_stock_data(symbol)
        check_fibonacci_levels(symbol)
        check_historical_extremes(symbol)

def get_all_saudi_stocks():
    # جلب جميع الأسهم السعودية من موقع تداول
    url = "https://www.tadawul.com.sa"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')
    symbols = [tag.text.strip() for tag in soup.select('.ticker-item')]
    return symbols[:100]  # تحديث أول 100 سهم لأغراض الاختبار

def update_stock_data(symbol):
    try:
        data = yf.download(f"{symbol}.SR", period="1y", interval="1d")
        if not data.empty:
            session = Session()
            stock = session.query(StockData).filter_by(symbol=symbol).first() or StockData(symbol=symbol)
            
            # حساب المؤشرات الفنية
            data['RSI'] = calculate_rsi(data)
            data['SMA_50'] = data['Close'].rolling(50).mean()
            data['SMA_200'] = data['Close'].rolling(200).mean()
            
            stock.data = data.to_json()
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

def detect_breakouts(symbol):
    session = Session()
    stock = session.query(StockData).filter_by(symbol=symbol).first()
    data = pd.read_json(stock.data)
    
    alerts = []
    current_price = data['Close'].iloc[-1]
    
    # الكشف عن اختراقات المستويات
    timeframes = {
        'ساعة': Interval.INTERVAL_1_HOUR,
        'يومي': Interval.INTERVAL_1_DAY,
        'أسبوعي': Interval.INTERVAL_1_WEEK,
        'شهري': Interval.INTERVAL_1_MONTH
    }
    
    for tf_name, tf in timeframes.items():
        handler = TA_Handler(symbol=f"{symbol}.SR", screener="saudi", exchange="SAUDI", interval=tf)
        analysis = handler.get_analysis()
        
        if analysis.summary['RECOMMENDATION'] == 'STRONG_BUY':
            alerts.append(f"📈 إختراق {tf_name} - إشارة شراء قوية")
            
    return alerts

# ------------------ Trading Strategies ------------------
def create_opportunity(symbol, entry, targets, stop_loss, strategy_name):
    session = Session()
    opportunity = GoldenOpportunity(
        symbol=symbol,
        entry_price=entry,
        targets=targets,
        stop_loss=stop_loss,
        strategy_name=strategy_name
    )
    session.add(opportunity)
    session.commit()
    session.close()

def check_targets():
    session = Session()
    opportunities = session.query(GoldenOpportunity).filter_by(status='active').all()
    
    for opp in opportunities:
        data = pd.read_json(session.query(StockData).filter_by(symbol=opp.symbol).first().data)
        current_price = data['Close'].iloc[-1]
        
        # تحديث الأهداف
        if current_price >= opp.targets[-1]:
            new_targets = [round(t * 1.1, 2) for t in opp.targets]  # زيادة الأهداف 10%
            opp.targets = new_targets
            session.commit()
            
            # إرسال إشعار التحديث
            context.bot.send_message(
                chat_id=opp.chat_id,
                text=f"🎯 تم تحديث الأهداف لـ {opp.symbol}\nالأهداف الجديدة: {new_targets}",
                reply_to_message_id=opp.message_id
            )

# ------------------ Reporting System ------------------
async def send_hourly_report(context):
    session = Session()
    top_gainers = session.query(StockData).order_by(text("data->>'Close' DESC")).limit(5).all()
    top_losers = session.query(StockData).order_by(text("data->>'Close' ASC")).limit(5).all()
    
    report = "📊 تقرير الساعة:\n\n"
    report += "📈 أكبر 5 صاعدين:\n" + "\n".join([f"{stock.symbol}: +{get_price_change(stock):.2f}%" for stock in top_gainers])
    report += "\n\n📉 أكبر 5 هابطين:\n" + "\n".join([f"{stock.symbol}: {get_price_change(stock):.2f}%" for stock in top_losers])
    
    await context.bot.send_message(chat_id=context.job.chat_id, text=arabic_text(report))

def get_price_change(stock):
    data = pd.read_json(stock.data)
    return ((data['Close'].iloc[-1] - data['Open'].iloc[-1]) / data['Open'].iloc[-1]) * 100

# ------------------ Command Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = [
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data='settings')],
        [InlineKeyboardButton("📈 تحليل سهم", callback_data='analyze')],
        [InlineKeyboardButton("📆 التقارير", callback_data='reports')]
    ]
    
    await update.message.reply_text(
        arabic_text("مرحبا! أنا بوت متابعة الأسهم السعودية 🛰️"),
        reply_markup=InlineKeyboardMarkup(menu)
    )

async def analyze_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = Session()
    user = session.query(UserActivity).filter_by(user_id=user_id).first()
    
    if not user:
        user = UserActivity(user_id=user_id)
        session.add(user)
    
    if user.analysis_count >= context.chat_data.get('analysis_limit', 5):
        await update.message.reply_text(arabic_text("⚠️ لقد تجاوزت الحد اليومي للاستعلامات."))
        return
    
    symbol = context.args[0]
    data = get_stock_analysis(symbol)
    
    # إرسال التحليل مع الصور والمخططات
    await update.message.reply_photo(
        photo=generate_chart(symbol),
        caption=arabic_text(data['analysis']),
        parse_mode='HTML'
    )
    
    user.analysis_count += 1
    user.last_analysis = get_saudi_time()
    session.commit()

# ------------------ Main Bot Setup ------------------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # تسجيل ال handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analyze", analyze_stock))
    
    # المهام المجدولة
    scheduler = BackgroundScheduler(timezone=SAUDI_TIMEZONE)
    scheduler.add_job(update_all_stocks, 'interval', hours=1)
    scheduler.add_job(send_hourly_report, CronTrigger(minute=0))
    scheduler.start()
    
    # تشغيل البوت
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 5000)),
        webhook_url=WEBHOOK_URL,
        url_path=TOKEN
    )

if __name__ == "__main__":
    main()