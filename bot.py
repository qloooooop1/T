import os
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
import pandas_ta as ta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0)
Session = sessionmaker(bind=engine)

# ------------------ Database Models ------------------
class Group(Base):
    __tablename__ = 'groups'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True)
    is_approved = Column(Boolean, default=False)
    subscription_end = Column(DateTime)
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {'golden': True, 'earthquake': True, 'volcano': True, 'lightning': True},
        'protection': {'max_messages': 100, 'antiflood': True, 'max_warnings': 3}
    })
    opportunities = relationship('Opportunity', back_populates='group')

class Opportunity(Base):
    __tablename__ = 'opportunities'
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    strategy = Column(String)
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    current_target = Column(Integer, default=0)
    status = Column(String, default='active')
    group_id = Column(Integer, ForeignKey('groups.id'))
    group = relationship('Group', back_populates='opportunities')
    created_at = Column(DateTime, default=datetime.now(SAUDI_TIMEZONE))

class ApprovalRequest(Base):
    __tablename__ = 'approvals'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    requester_id = Column(String)
    requested_at = Column(DateTime, default=datetime.now(SAUDI_TIMEZONE))
    handled = Column(Boolean, default=False)

Base.metadata.create_all(engine)

class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.setup_handlers()
        self.setup_scheduler()
        
    def setup_handlers(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('settings', self.settings))
        self.app.add_handler(CommandHandler('approve', self.approve_group))
        self.app.add_handler(CallbackQueryHandler(self.handle_button, pattern=r'^settings_|^opportunity_|^approve_|^close_'))
        
    def setup_scheduler(self):
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=15)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=15, minute=30))
        self.scheduler.add_job(self.send_weekly_report, CronTrigger(day_of_week=6, hour=17))
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await update.message.reply_html(
            f"مرحبًا {user.mention_html()}! 👑 بوت الأسهم السعودية المتقدم\n"
            "📈 احصل على أحدث التحليلات الفنية والتنبيهات الذكية",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ الإعدادات", callback_data='settings_main')],
                [InlineKeyboardButton("📊 التقارير", callback_data='settings_reports')]
            ])
        )
        
    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        try:
            group = session.query(Group).filter_by(chat_id=str(update.effective_chat.id)).first()
            if not group or not group.is_approved:
                return await update.message.reply_text("⚠️ يلزم الموافقة على المجموعة أولاً")
                
            buttons = [
                [InlineKeyboardButton("🗂 إدارة التقارير", callback_data='settings_reports')],
                [InlineKeyboardButton("⚔️ الاستراتيجيات", callback_data='settings_strategies')],
                [InlineKeyboardButton("🔐 الحماية", callback_data='settings_protection')]
            ]
            await update.message.reply_text(
                "⚙️ لوحة التحكم الرئيسية:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        finally:
            session.close()
    
    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period='1d', interval='15m')
                if self.detect_golden_cross(data):
                    await self.create_opportunity(symbol, 'golden', data)
                if self.detect_volume_spike(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
        except Exception as e:
            logging.error(f"Error checking opportunities: {str(e)}")
        finally:
            session.close()
    
    def detect_golden_cross(self, data):
        ema50 = ta.ema(data['Close'], length=50)
        ema200 = ta.ema(data['Close'], length=200)
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]
    
    def detect_volume_spike(self, data):
        return data['Volume'].iloc[-1] > (data['Volume'].rolling(20).mean().iloc[-1] * 3)
    
    async def create_opportunity(self, symbol, strategy, data):
        session = Session()
        try:
            entry = data['Close'].iloc[-1]
            targets = self.calculate_targets(strategy, entry)
            stop_loss = data['Low'].iloc[-2] * 0.99
            
            opp = Opportunity(
                symbol=symbol,
                strategy=strategy,
                entry_price=entry,
                targets=targets,
                stop_loss=stop_loss,
                group_id=1  # Default group
            )
            
            session.add(opp)
            session.commit()
            
            groups = session.query(Group).filter(
                Group.settings['strategies'][strategy].as_boolean()
            ).all()
            
            for group in groups:
                await self.send_alert(group.chat_id, opp)
                
        except Exception as e:
            logging.error(f"Error creating opportunity: {str(e)}")
        finally:
            session.close()
    
    async def send_alert(self, chat_id, opportunity):
        text = f"🚨 فرصة {opportunity.strategy} جديدة!\n"
        text += f"📈 السهم: {opportunity.symbol}\n"
        text += f"💰 السعر الحالي: {opportunity.entry_price:.2f}\n"
        text += f"🎯 الأهداف: {' → '.join(map(str, opportunity.targets))}\n"
        text += f"🛑 وقف الخسارة: {opportunity.stop_loss:.2f}"
        
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("متابعة الأهداف", callback_data=f"track_{opportunity.id}")],
                [InlineKeyboardButton("إغلاق الفرصة", callback_data=f"close_{opportunity.id}")]
            ])
        )
    
    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period='1d')
                change = ((data['Close'].iloc[-1] - data['Open'].iloc[-1]) / data['Open'].iloc[-1]) * 100
                report += f"{symbol}: {change:.2f}%\n"
            
            groups = session.query(Group).filter(
                Group.settings['reports']['daily'].as_boolean()
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report
                )
        finally:
            session.close()
    
    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        action = query.data.split('_')[0]
        
        if action == 'settings':
            await self.handle_settings(query)
        elif action == 'approve':
            await self.handle_approval(query)
        elif action == 'close':
            await query.message.delete()
    
    async def run(self):
        await self.app.initialize()
        await self.app.start()
        self.scheduler.start()
        
        await self.app.updater.start_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get('PORT', 5000)),
            url_path=TOKEN,
            webhook_url=WEBHOOK_URL
        )
        
        logging.info("Bot is running...")
        await asyncio.Event().wait()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    bot = SaudiStockBot()
    asyncio.run(bot.run())