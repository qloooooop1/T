import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yfinance as yf
import pandas as pd
import ta
import emoji
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import ClientSession
import re
import json

# تهيئة السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=engine)

# جداول قاعدة البيانات
class Stock(Base):
    __tablename__ = 'stocks'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True)
    last_price = Column(Float)
    last_checked = Column(DateTime)

class Opportunity(Base):
    __tablename__ = 'opportunities'
    id = Column(Integer, primary_key=True)
    stock_symbol = Column(String)
    strategy_name = Column(String)
    targets = Column(String)
    stop_loss = Column(Float)
    status = Column(String, default='active')
    message_id = Column(Integer)

class GroupSettings(Base):
    __tablename__ = 'group_settings'
    id = Column(Integer, primary_key=True)
    group_id = Column(String, unique=True)
    allow_discussion = Column(Boolean, default=True)
    delete_phone_numbers_and_links = Column(Boolean, default=True)
    enable_hourly_report = Column(Boolean, default=False)
    enable_daily_report = Column(Boolean, default=True)
    enable_weekly_report = Column(Boolean, default=True)
    strategies = Column(String, default='{"golden_opportunity": true, "fibonacci_breakout": false}')

class Strategy(Base):
    __tablename__ = 'strategies'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    indicator = Column(String)
    threshold = Column(Float)
    enabled = Column(Boolean, default=True)

Base.metadata.create_all(engine)

async def fetch_stock_data(symbol, session):
    async with session.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.SR") as response:
        if response.status == 200:
            data = await response.json()
            df = pd.DataFrame(data['chart']['result'][0]['indicators']['quote'][0])
            df['timestamp'] = pd.to_datetime(data['chart']['result'][0]['timestamp'], unit='s')
            return df.set_index('timestamp')
    return None

def calculate_rsi(data, period=14):
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_fibonacci_levels(data):
    high = data['high'].max()
    low = data['low'].min()
    fib_levels = [0.618, 0.786, 1.0, 1.272, 1.618]
    return [low + (high - low) * level for level in fib_levels]

async def fetch_saudi_stocks(session):
    saudi_stocks = yf.Tickers("^TASI")  # الفهرس العام للأسهم السعودية
    tickers = saudi_stocks.tickers
    symbols = [t for t in tickers if t.endswith('.SR')]
    
    session_db = Session()
    try:
        for symbol in symbols:
            stock = session_db.query(Stock).filter_by(symbol=symbol).first()
            if not stock:
                session_db.add(Stock(symbol=symbol))
        session_db.commit()
    finally:
        session_db.close()
    return symbols

async def update_stock_data():
    session_db = Session()
    async with ClientSession() as session_client:
        symbols = await fetch_saudi_stocks(session_client)
        for symbol in symbols:
            data = await fetch_stock_data(symbol, session_client)
            if data is not None:
                stock = session_db.query(Stock).filter_by(symbol=symbol).first()
                if stock:
                    stock.last_price = data['close'].iloc[-1]
                    stock.last_checked = datetime.utcnow()
                else:
                    session_db.add(Stock(symbol=symbol, last_price=data['close'].iloc[-1], last_checked=datetime.utcnow()))
        session_db.commit()
    session_db.close()

async def send_report(context, period):
    session_db = Session()
    try:
        stocks = session_db.query(Stock).all()
        async with ClientSession() as session_client:
            stock_data = {s.symbol: await fetch_stock_data(s.symbol, session_client) for s in stocks if await fetch_stock_data(s.symbol, session_client) is not None}
        sorted_stocks = sorted(stock_data.items(), key=lambda x: x[1]['close'].iloc[-1], reverse=True)
        
        top_5 = [s[0] for s in sorted_stocks[:5]]
        bottom_5 = [s[0] for s in sorted_stocks[-5:]]
        report_text = f"🇸🇦 **تقرير {period}**:\n" + \
                      f"📈 **أعلى 5 شركات:**\n" + "\n".join([f"- {s}" for s in top_5]) + "\n" + \
                      f"📉 **أقل 5 شركات:**\n" + "\n".join([f"- {s}" for s in bottom_5])
        await context.bot.send_message(context.job.chat_id, report_text, parse_mode='Markdown')
    finally:
        session_db.close()

async def check_strategies(context):
    session_db = Session()
    strategies = json.loads(session_db.query(GroupSettings.strategies).filter_by(group_id=str(context.job.chat_id)).first().strategies)
    try:
        async with ClientSession() as session_client:
            for stock in session_db.query(Stock).all():
                data = await fetch_stock_data(stock.symbol, session_client)
                if data is not None:
                    if strategies.get('golden_opportunity', False):
                        rsi = calculate_rsi(data)
                        if rsi.iloc[-1] > 70:
                            await send_opportunity(stock.symbol, "فرصة ذهبية", data, context, rsi, "rsi")
                    if strategies.get('fibonacci_breakout', False):
                        fib_levels = calculate_fibonacci_levels(data)
                        if data['close'].iloc[-1] > fib_levels[0]:  # اختراق مستوى 61.8%
                            await send_opportunity(stock.symbol, "التوقعات السرية", data, context, fib_levels, "fibonacci")
                    # إضافة مزيد من الاستراتيجيات هنا
    finally:
        session_db.close()

async def send_opportunity(symbol, strategy_name, data, context, indicator, strategy_type):
    current_price = data['close'].iloc[-1]
    if strategy_type == "rsi":
        targets = [current_price * (1 + i * 0.05) for i in range(1, 6)]
    elif strategy_type == "fibonacci":
        targets = indicator[1:]  # تجاهل مستوى 61.8% حيث تم اختراقه بالفعل
    else:
        targets = []
    
    stop_loss = data['low'].min()
    
    message = await context.bot.send_message(context.job.chat_id, 
        f"{emoji.emojize(':star:')} **{strategy_name}** للسهم {symbol}:\n" +
        f"أهداف:\n" + "\n".join([f"{i+1}- {t:.2f}" for i, t in enumerate(targets)]) +
        f"\nوقف الخسارة: {stop_loss:.2f}", parse_mode='Markdown')
    
    session_db = Session()
    try:
        session_db.add(Opportunity(stock_symbol=symbol, strategy_name=strategy_name, 
                                   targets=','.join(map(str, targets)), stop_loss=stop_loss, 
                                   status='active', message_id=message.message_id))
        session_db.commit()
    finally:
        session_db.close()

async def track_opportunity_targets(context):
    session_db = Session()
    try:
        async with ClientSession() as session_client:
            for opportunity in session_db.query(Opportunity).filter_by(status='active').all():
                data = await fetch_stock_data(opportunity.stock_symbol, session_client)
                if data is not None:
                    current_price = data['close'].iloc[-1]
                    targets = [float(t) for t in opportunity.targets.split(',')]
                    for i, target in enumerate(targets):
                        if current_price >= target:
                            original_message = await context.bot.get_message(chat_id=context.job.chat_id, message_id=opportunity.message_id)
                            congrat_message = f"{emoji.emojize(':tada:')} **مبروك! تم تحقيق الهدف رقم {i+1}** للسهم {opportunity.stock_symbol} باستراتيجية {opportunity.strategy_name}\n\n" + \
                                              f"الرسالة الأصلية:\n```\n{original_message.text}\n```"
                            await context.bot.send_message(context.job.chat_id, congrat_message, parse_mode='Markdown')
                            
                            if i == len(targets) - 1:
                                opportunity.status = 'completed'
                                new_targets = [current_price * (1 + i * 0.05) for i in range(1, 6)]
                                opportunity.targets = ','.join(map(str, new_targets))
                                opportunity.stop_loss = current_price
                                updated_message = f"{emoji.emojize(':star:')} **تم تحديث فرصة {opportunity.strategy_name}** للسهم {opportunity.stock_symbol}:\n" + \
                                                  f"أهداف جديدة:\n" + "\n".join([f"{j+1}- {t:.2f}" for j, t in enumerate(new_targets)]) + \
                                                  f"\nوقف الربح: {current_price:.2f}"
                                new_msg = await context.bot.send_message(context.job.chat_id, updated_message, parse_mode='Markdown')
                                opportunity.message_id = new_msg.message_id
                            else:
                                opportunity.targets = ','.join(map(str, targets[i+1:]))
                            session_db.commit()
                            break
                    if current_price <= opportunity.stop_loss:
                        opportunity.status = 'stop_loss'
                        session_db.commit()
                        await context.bot.send_message(context.job.chat_id, 
                            f"**تم وقف الخسارة** للسهم {opportunity.stock_symbol} باستراتيجية {opportunity.strategy_name}", parse_mode='Markdown')
    finally:
        session_db.close()

# إدارة الإعدادات
async def manage_settings(update, context):
    if update.message.chat.type == 'group':
        group_id = str(update.message.chat_id)
        session_db = Session()
        try:
            settings = session_db.query(GroupSettings).filter_by(group_id=group_id).first()
            if not settings:
                settings = GroupSettings(group_id=group_id)
                session_db.add(settings)
            
            strategies = json.loads(settings.strategies)
            keyboard = [
                [InlineKeyboardButton("تفعيل النقاشات", callback_data='toggle_discussion')],
                [InlineKeyboardButton("حذف الأرقام والروابط", callback_data='toggle_delete_numbers_links')],
                [InlineKeyboardButton("تقرير ساعي", callback_data='toggle_hourly_report')],
                [InlineKeyboardButton("تقرير يومي", callback_data='toggle_daily_report')],
                [InlineKeyboardButton("تقرير أسبوعي", callback_data='toggle_weekly_report')],
                [InlineKeyboardButton(f"فرصة ذهبية - {'مفعل' if strategies['golden_opportunity'] else 'غير مفعل'}", callback_data='toggle_golden_opportunity')],
                [InlineKeyboardButton(f"التوقعات السرية - {'مفعل' if strategies['fibonacci_breakout'] else 'غير مفعل'}", callback_data='toggle_fibonacci_breakout')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("إعدادات البوت:", reply_markup=reply_markup)
        finally:
            session_db.close()

# معالجة الضغط على الزر
async def button(update, context):
    query = update.callback_query
    await query.answer()
    session_db = Session()
    try:
        group_id = str(query.message.chat_id)
        settings = session_db.query(GroupSettings).filter_by(group_id=group_id).first()
        if not settings:
            settings = GroupSettings(group_id=group_id)
            session_db.add(settings)
        
        strategies = json.loads(settings.strategies)
        
        if query.data == 'toggle_discussion':
            settings.allow_discussion = not settings.allow_discussion
        elif query.data == 'toggle_delete_numbers_links':
            settings.delete_phone_numbers_and_links = not settings.delete_phone_numbers_and_links
        elif query.data == 'toggle_hourly_report':
            settings.enable_hourly_report = not settings.enable_hourly_report
        elif query.data == 'toggle_daily_report':
            settings.enable_daily_report = not settings.enable_daily_report
        elif query.data == 'toggle_weekly_report':
            settings.enable_weekly_report = not settings.enable_weekly_report
        elif query.data == 'toggle_golden_opportunity':
            strategies['golden_opportunity'] = not strategies['golden_opportunity']
        elif query.data == 'toggle_fibonacci_breakout':
            strategies['fibonacci_breakout'] = not strategies['fibonacci_breakout']
        
        settings.strategies = json.dumps(strategies)
        session_db.commit()
        await query.edit_message_text(f"تم تحديث الإعدادات: {query.data}", parse_mode='Markdown')
    finally:
        session_db.close()

# إدارة رسائل المجموعة
async def handle_group_message(update, context):
    message = update.message
    session_db = Session()
    try:
        settings = session_db.query(GroupSettings).filter_by(group_id=str(message.chat_id)).first()
        if settings and settings.delete_phone_numbers_and_links:
            text = message.text.lower()
            if re.search(r'\+?\d[\d\-\s\.\(\)]{8,}\d', text) or \
               re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text) or \
               re.search(r'whatsapp\.com|wa\.me', text):
                await message.delete()
        if settings and not settings.allow_discussion:
            await message.delete()
    finally:
        session_db.close()

# تهيئة البوت
async def start(update, context):
    await update.message.reply_text(f"مرحبًا بك في بوت الراصد لرصد السوق السعودي! اضغط هنا للإعدادات.",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إعدادات", callback_data='settings')]]))
    group_id = str(update.message.chat_id)
    session_db = Session()
    try:
        if not session_db.query(GroupSettings).filter_by(group_id=group_id).first():
            session_db.add(GroupSettings(group_id=group_id))
            session_db.commit()
    finally:
        session_db.close()

# جدولة المهام
scheduler = AsyncIOScheduler()
scheduler.add_job(update_stock_data, 'interval', minutes=5)
scheduler.add_job(lambda: asyncio.create_task(check_strategies(ContextTypes.DEFAULT_TYPE())), 'interval', minutes=30)
scheduler.add_job(lambda: asyncio.create_task(track_opportunity_targets(ContextTypes.DEFAULT_TYPE())), 'interval', minutes=15)
scheduler.add_job(lambda: asyncio.create_task(send_report(ContextTypes.DEFAULT_TYPE(), "ساعي")) if Session().query(GroupSettings).filter_by(enable_hourly_report=True).first() else None, 'interval', minutes=60)
scheduler.add_job(lambda: asyncio.create_task(send_report(ContextTypes.DEFAULT_TYPE(), "يومي")) if Session().query(GroupSettings).filter_by(enable_daily_report=True).first() else None, 'cron', hour=9)
scheduler.add_job(lambda: asyncio.create_task(send_report(ContextTypes.DEFAULT_TYPE(), "أسبوعي")) if Session().query(GroupSettings).filter_by(enable_weekly_report=True).first() else None, 'cron', day_of_week='mon', hour=9)
scheduler.start()

# تشغيل البوت
async def main():
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))
    application.add_handler(CallbackQueryHandler(button))
    
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())