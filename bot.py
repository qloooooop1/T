import os
import logging
from datetime import datetime, timedelta
from time import sleep
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
import yfinance as yf
import pandas as pd
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, ConversationHandler, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
from arabic_reshaper import ArabicReshaper
from bidi.algorithm import get_display

# تفعيل الشاشة البيئية
load_dotenv()

# إعداد اللوجر
logging.basicConfig(filename='error_log.txt', level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask app for Webhook
app = Flask(__name__)

# إعداد قاعدة البيانات
Base = declarative_base()

class Stock(Base):
    __tablename__ = 'stocks'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True)
    name = Column(String)
    last_price = Column(Float)
    updated_at = Column(DateTime)

class Strategy(Base):
    __tablename__ = 'strategies'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    description = Column(String)
    enabled = Column(Boolean, default=True)
    emoji = Column(String)

class Opportunity(Base):
    __tablename__ = 'opportunities'
    id = Column(Integer, primary_key=True)
    stock_id = Column(Integer, ForeignKey('stocks.id'))
    strategy_id = Column(Integer, ForeignKey('strategies.id'))
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default='active')
    stop_loss = Column(Float)
    take_profit = Column(Float)

    stock = relationship("Stock", back_populates="opportunities")
    strategy = relationship("Strategy", back_populates="opportunities")
    targets = relationship("Target", back_populates="opportunity", cascade="all, delete-orphan")

class Target(Base):
    __tablename__ = 'targets'
    id = Column(Integer, primary_key=True)
    opportunity_id = Column(Integer, ForeignKey('opportunities.id'))
    price = Column(Float)
    achieved = Column(Boolean, default=False)

    opportunity = relationship("Opportunity", back_populates="targets")

class Alert(Base):
    __tablename__ = 'alerts'
    id = Column(Integer, primary_key=True)
    stock_id = Column(Integer, ForeignKey('stocks.id'))
    alert_type = Column(String)
    alert_value = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# إعداد الاتصال بقاعدة البيانات
engine = create_engine(os.getenv('DATABASE_URL'))
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# إعداد الجدولة
sched = BackgroundScheduler()
sched.start()

def retry_operation(func, max_retries=3, delay=5):
    def wrapper(*args, **kwargs):
        retries = 0
        while retries < max_retries:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"حدث خطأ في {func.__name__}: {e}")
                retries += 1
                if retries == max_retries:
                    raise
                sleep(delay)
    return wrapper

@retry_operation
def fetch_stock_data(symbol, period='1d', interval='1h'):
    end = datetime.now()
    start = end - timedelta(days=365) if period == '1d' else end - timedelta(days=7)
    
    stock = yf.Ticker(symbol)
    try:
        data = stock.history(period=period, interval=interval)
        if data.empty:
            raise ValueError(f"البيانات فارغة للسهم {symbol}")
        return data
    except Exception as e:
        raise e

def calculate_rsi(data, window=14):
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def detect_opportunities(data, stock, strategies):
    opportunities = []
    for strategy in strategies:
        if strategy.name == "RSI":
            rsi = calculate_rsi(data)
            if rsi.iloc[-1] > 70:  
                opportunities.append(create_opportunity(stock, strategy, data))
        # إضافة استراتيجيات أخرى هنا
    return opportunities

def create_opportunity(stock, strategy, data):
    high, low = data['High'].max(), data['Low'].min()
    current_price = data['Close'].iloc[-1]
    opportunity = Opportunity(stock_id=stock.id, strategy_id=strategy.id, 
                              stop_loss=low, take_profit=high * 1.1)
    opportunity.targets = [Target(price=current_price + (high - low) * i / 4) for i in range(1, 5)]
    return opportunity

def monitor_opportunities(context):
    session = Session()
    stocks = session.query(Stock).all()
    strategies = session.query(Strategy).filter_by(enabled=True).all()
    
    for stock in stocks:
        try:
            data = fetch_stock_data(stock.symbol)
            if data is not None:
                new_opportunities = detect_opportunities(data, stock, strategies)
                for opp in new_opportunities:
                    session.add(opp)
                    send_opportunity_alert(context, opp, stock)
        except Exception as e:
            logger.error(f"خطأ في مراقبة الفرص للسهم {stock.symbol}: {e}")
    
    session.commit()
    session.close()

def send_opportunity_alert(context, opportunity, stock):
    targets = "\n".join([f"🎯 الهدف {i+1}: {target.price:.2f}" for i, target in enumerate(opportunity.targets)])
    message = f"""⚡ فرصة جديدة للسهم {stock.symbol} باستراتيجية {opportunity.strategy.emoji} {opportunity.strategy.name}!
    - **وقف الخسارة:** {opportunity.stop_loss:.2f}
    - **الأهداف:** 
    {targets}
    """
    context.bot.send_message(chat_id=os.getenv('CHAT_ID'), text=message, parse_mode='Markdown')

def check_targets(context):
    session = Session()
    for opportunity in session.query(Opportunity).filter_by(status='active'):
        stock_data = fetch_stock_data(opportunity.stock.symbol)
        if stock_data is not None:
            current_price = stock_data['Close'].iloc[-1]
            for target in opportunity.targets:
                if not target.achieved and current_price >= target.price:
                    target.achieved = True
                    send_target_alert(context, opportunity, target, stock_data)
            if all(target.achieved for target in opportunity.targets):
                opportunity.status = 'closed'
                update_opportunity(context, opportunity, stock_data)
    session.commit()
    session.close()

def send_target_alert(context, opportunity, target, stock_data):
    message = f"🎉 تم تحقيق الهدف {opportunity.targets.index(target) + 1} للفرصة {opportunity.strategy.emoji} {opportunity.strategy.name} بسعر {target.price:.2f}"
    context.bot.send_message(chat_id=os.getenv('CHAT_ID'), text=message)

def update_opportunity(context, opportunity, stock_data):
    high, low = stock_data['High'].max(), stock_data['Low'].min()
    opportunity.stop_loss = opportunity.take_profit  
    opportunity.take_profit = high * 1.1  
    opportunity.targets = [Target(price=opportunity.take_profit + (high - low) * i / 4) for i in range(1, 5)]
    message = f"🔄 تم تحديث الفرصة {opportunity.strategy.emoji} {opportunity.strategy.name} مع أهداف جديدة!"
    context.bot.send_message(chat_id=os.getenv('CHAT_ID'), text=message)

def admin_panel(update: Update, context):
    if update.message.from_user.id not in context.bot_data.get('admins', []):
        update.message.reply_text('ليس لديك صلاحية للوصول إلى هذه الإعدادات.')
        return

    strategies = session.query(Strategy).all()
    keyboard = [
        [InlineKeyboardButton(f"{'✅' if s.enabled else '❌'} {s.emoji} {s.name}", callback_data=f"toggle_{s.id}")] 
        for s in strategies
    ]
    keyboard.append([InlineKeyboardButton("إضافة استراتيجية جديدة", callback_data="add_strategy")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("إدارة الاستراتيجيات:", reply_markup=reply_markup)

def handle_strategy_action(update: Update, context):
    query = update.callback_query
    query.answer()
    action, strategy_id = query.data.split('_', 1)
    
    if action == "toggle":
        strategy = session.query(Strategy).get(int(strategy_id))
        strategy.enabled = not strategy.enabled
        session.commit()
        query.edit_message_text(text=f"تم {'' if strategy.enabled else 'إلغاء'} تفعيل الاستراتيجية: {strategy.emoji} {strategy.name}")
    elif action == "add_strategy":
        context.user_data['adding'] = True
        query.edit_message_text(text="أدخل اسم الاستراتيجية الجديدة مع إيموجي (مثال: RSI 📈):")
        return ADDING

    return ConversationHandler.END

def add_new_strategy(update: Update, context):
    if context.user_data.pop('adding', False):
        name, emoji = update.message.text.split(' ', 1)
        new_strategy = Strategy(name=name, description="إضافة وصف", emoji=emoji)
        session.add(new_strategy)
        session.commit()
        update.message.reply_text(f"تم إضافة الاستراتيجية {emoji} {name}")
    return ConversationHandler.END

# إضافة التحاليل الفنية والتنبيهات
def check_price_alerts(data, stock):
    if data is None or len(data) < 2:
        return []
    
    current_price = data['Close'].iloc[-1]
    high_monthly = data['High'].resample('M').max().iloc[-1] if len(data) > 30 else None
    high_yearly = data['High'].resample('Y').max().iloc[-1] if len(data) > 365 else None
    high_all_time = data['High'].max()
    low_all_time = data['Low'].min()
    
    alerts = []

    if high_monthly and current_price >= high_monthly:
        alerts.append(f"🎉 سعر السهم {stock.symbol} وصل إلى أعلى مستوى شهري: {current_price:.2f} SAR")
    if high_yearly and current_price >= high_yearly:
        alerts.append(f"🚀 سعر السهم {stock.symbol} وصل إلى أعلى مستوى سنوي: {current_price:.2f} SAR")
    if current_price >= high_all_time:
        alerts.append(f"🌟 سعر السهم {stock.symbol} وصل إلى أعلى مستوى تاريخي: {current_price:.2f} SAR")
    if current_price <= low_all_time:
        alerts.append(f"⛔ سعر السهم {stock.symbol} وصل إلى أدنى مستوى تاريخي: {current_price:.2f} SAR")

    # التحقق من النسبة اليومية (10%)
    if len(data) > 1:
        previous_close = data['Close'].iloc[-2]
        change_percent = ((current_price - previous_close) / previous_close) * 100
        if change_percent >= 10:
            alerts.append(f"📈 زيادة ملحوظة في سهم {stock.symbol}: {change_percent:.2f}%")
        elif change_percent <= -10:
            alerts.append(f"📉 انخفاض ملحوظ في سهم {stock.symbol}: {change_percent:.2f}%")

    return alerts

def monitor_price_alerts(context):
    for stock in session.query(Stock).all():
        try:
            data = fetch_stock_data(stock.symbol, period='1d', interval='1h')
            alerts = check_price_alerts(data, stock)
            for alert in alerts:
                context.bot.send_message(chat_id=os.getenv('CHAT_ID'), text=alert)
        except Exception as e:
            logger.error(f"خطأ في مراقبة التنبيهات للسهم {stock.symbol}: {e}")

# إضافة التقارير اليومية والأسبوعية
def create_professional_report(data, period):
    # تحليل البيانات
    patterns = []
    strategies = []
    stock = session.query(Stock).filter_by(symbol=data.name).first()
    
    if period in ['daily', 'weekly']:
        pattern_results = [advanced_head_and_shoulders(data), advanced_cup_and_handle(data), advanced_triangle_pattern(data)]
        patterns = [p for p in pattern_results if p]
        strategies = [advanced_rsi_strategy(data), advanced_moving_averages_strategy(data)]
        strategies = [s for s in strategies if s]
    
    # تجميع البيانات الأساسية
    last_price = data['Close'].iloc[-1]
    volume = data['Volume'].iloc[-1]
    change = last_price - data['Close'].iloc[-2] if len(data) > 1 else 0
    percentage_change = (change / data['Close'].iloc[-2]) * 100 if len(data) > 1 else 0

    # تنسيق التقرير
    report = [
        f"## تقرير {period.capitalize()} للسهم **{stock.symbol}** - {stock.name}",
        f"**السعر الأخير:** {last_price:.2f}",
        f"**التغير:** {change:.2f} ({percentage_change:.2f}%)",
        f"**حجم التداول:** {volume}",
    ]

    # إضافة الأنماط الفنية
    if patterns:
        report.append("\n### الأنماط الفنية المكتشفة:")
        for pattern in patterns:
            report.append(f"- **{pattern['pattern']}**: {'اختراق' if pattern['breakout'] else 'لم يخترق'} خط الاختراق. **الهدف:** {pattern['target']:.2f}")

    # إضافة الاستراتيجيات
    if strategies:
        report.append("\n### الاستراتيجيات التجارية:")
        for strategy in strategies:
            report.append(f"- **{strategy['action'].capitalize()}**: {strategy['reason']}")

    # إضافة جدول لأداء السهم خلال الفترة
    if period != 'hourly':
        performance = data[['Open', 'High', 'Low', 'Close', 'Volume']].tail(7 if period == 'weekly' else 1)
        report.append("\n### أداء السهم:")
        report.append("```\n" + performance.to_string(index=False) + "\n```")

    # النص النهائي
    return "\n".join(report)

def generate_and_send_report(context, period):
    for stock in session.query(Stock).all():
        try:
            data = fetch_stock_data(stock.symbol, period='1d' if period in ['daily', 'hourly'] else '1wk')
            if data is not None:
                report = create_professional_report(data, period)
                context.bot.send_message(chat_id=os.getenv('CHAT_ID'), text=report, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"خطأ في إنشاء التقرير للسهم {stock.symbol}: {e}")

# إضافة الجدولة للتقارير و التنبيهات
sched.add_job(lambda: monitor_opportunities(CallbackContext(None, None)), 'interval', minutes=30)
sched.add_job(lambda: check_targets(CallbackContext(None, None)), 'interval', minutes=15)
sched.add_job(lambda: monitor_price_alerts(CallbackContext(None, None)), 'interval', minutes=5)
sched.add_job(lambda: generate_and_send_report(CallbackContext(None, None), 'hourly'), 'interval', hours=1)
sched.add_job(lambda: generate_and_send_report(CallbackContext(None, None), 'daily'), 'cron', hour=17)
sched.add_job(lambda: generate_and_send_report(CallbackContext(None, None), 'weekly'), 'cron', day_of_week='fri', hour=17)

# إعدادات Telegram Bot
updater = Updater(os.getenv('BOT_TOKEN'), use_context=True)
dp = updater.dispatcher

# إضافة المعالجات إلى الديسبتشر
dp.add_handler(CommandHandler('admin_panel', admin_panel))
dp.add_handler(CallbackQueryHandler(handle_strategy_action))
dp.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(handle_strategy_action, pattern='^add_strategy')],
    states={
        ADDING: [MessageHandler(Filters.text & ~Filters.command, add_new_strategy)]
    },
    fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text('تم الإلغاء'))]
))

# Webhook setup
@app.route('/' + os.getenv('BOT_TOKEN'), methods=['POST'])
def webhook_handler():
    update = Update.de_json(request.get_json(force=True), updater.bot)
    dp.process_update(update)
    return 'ok'

@app.route('/')
def index():
    return 'Welcome to the bot server'

# Heroku port
PORT = int(os.environ.get('PORT', '8443'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)