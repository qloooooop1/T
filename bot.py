import os
import logging
import asyncio
import pandas as pd
import numpy as np
from pyalgotrade import strategy
from pyalgotrade.barfeed import yahoofeed
from pyalgotrade.technical import ma
from pyalgotrade.technical import bollinger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Configuration
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# Database Models
class Group(Base):
    __tablename__ = 'groups'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True)
    is_approved = Column(Boolean, default=False)
    subscription_end = Column(DateTime)
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {
            'golden': True, 'earthquake': True,
            'volcano': True, 'lightning': True
        },
        'protection': {
            'max_messages': 200,
            'antiflood': True,
            'max_warnings': 3
        }
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
    message_id = Column(Integer)
    group_id = Column(Integer, ForeignKey('groups.id'))
    group = relationship('Group', back_populates='opportunities')
    created_at = Column(DateTime, default=lambda: datetime.now(SAUDI_TIMEZONE))

class ApprovalRequest(Base):
    __tablename__ = 'approval_requests'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    requester_id = Column(String)
    requested_at = Column(DateTime, default=lambda: datetime.now(SAUDI_TIMEZONE))
    handled = Column(Boolean, default=False)

Base.metadata.create_all(engine)

class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.setup_handlers()
        self.setup_scheduler()

    # Handlers Setup
    def setup_handlers(self):
        handlers = [
            CommandHandler('start', self.start),
            CommandHandler('settings', self.settings),
            CommandHandler('approve', self.approve_group),
            CallbackQueryHandler(self.handle_button)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    # Scheduler Setup
    def setup_scheduler(self):
        self.scheduler.add_job(
            self.check_opportunities,
            'interval',
            minutes=5,
            max_instances=3
        )
        self.scheduler.add_job(
            self.send_daily_report,
            CronTrigger(hour=16, minute=0, timezone=SAUDI_TIMEZONE)
        )

    # Core Handlers
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings'),
             InlineKeyboardButton("التقارير 📊", callback_data='reports')],
            [InlineKeyboardButton("الدعم الفني 📞", url='t.me/support')]
        ]
        await update.message.reply_text(
            "مرحبًا بك في بوت الأسهم السعودية المتقدم! 📈",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        try:
            chat_id = str(update.effective_chat.id)
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group or not group.is_approved:
                return await update.message.reply_text("⚠️ يلزم تفعيل المجموعة أولاً")
            
            settings_text = (
                "⚙️ إعدادات المجموعة:\n\n"
                f"📊 التقارير:\n"
                f"- ساعية: {'✅' if group.settings['reports']['hourly'] else '❌'}\n"
                f"- يومية: {'✅' if group.settings['reports']['daily'] else '❌'}\n"
                f"- أسبوعية: {'✅' if group.settings['reports']['weekly'] else '❌'}\n\n"
                f"🔍 الاستراتيجيات:\n"
                f"- ذهبية: {'✅' if group.settings['strategies']['golden'] else '❌'}\n"
                f"- زلزالية: {'✅' if group.settings['strategies']['earthquake'] else '❌'}\n"
                f"- بركانية: {'✅' if group.settings['strategies']['volcano'] else '❌'}\n"
                f"- برقية: {'✅' if group.settings['strategies']['lightning'] else '❌'}"
            )

            buttons = [
                [InlineKeyboardButton("تعديل التقارير", callback_data='edit_reports'),
                 InlineKeyboardButton("تعديل الاستراتيجيات", callback_data='edit_strategies')],
                [InlineKeyboardButton("إغلاق", callback_data='close')]
            ]
            
            await update.message.reply_text(
                settings_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logging.error(f"Settings Error: {str(e)}")
        finally:
            session.close()

    # Opportunity System
    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = self.fetch_stock_data(symbol, '1d', '30m')  # Fetch with PyAlgoTrade
                if len(data) < 50: 
                    continue

                # Golden Cross Strategy
                if self.detect_golden_cross(data):
                    await self.create_opportunity(symbol, 'golden', data)
                
                # Earthquake Strategy (Breakout)
                if self.detect_breakout(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}")
        finally:
            session.close()

    def fetch_stock_data(self, symbol, period, interval):
        # Mock data fetch. This would need to be implemented with PyAlgoTrade's datafeed
        feed = yahoofeed.Feed()
        feed.addBarsFromCSV(symbol, "path_to_your_csv_data.csv")
        bars = []
        for dateTime, bar in feed[symbol]:
            bars.append([dateTime, bar.getOpen(), bar.getHigh(), bar.getLow(), bar.getClose(), bar.getVolume()])
        return pd.DataFrame(bars, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])

    def detect_golden_cross(self, data):
        # Using PyAlgoTrade moving averages
        short_ema = ma.EMA(data['Close'], 50)
        long_ema = ma.EMA(data['Close'], 200)
        return short_ema[-1] > long_ema[-1] and short_ema[-2] <= long_ema[-2]

    def detect_breakout(self, data):
        # Simple breakout strategy
        return data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2]

    async def create_opportunity(self, symbol, strategy, data):
        session = Session()
        try:
            entry_price = data['Close'].iloc[-1]
            stop_loss = data['Low'].iloc[-2] * 0.98
            targets = self.calculate_targets(strategy, entry_price)
            
            opp = Opportunity(
                symbol=symbol,
                strategy=strategy,
                entry_price=entry_price,
                targets=targets,
                stop_loss=stop_loss
            )
            
            session.add(opp)
            session.commit()
            
            await self.send_alert(opp)
        except Exception as e:
            logging.error(f"Create Opportunity Error: {str(e)}")
        finally:
            session.close()

    def calculate_targets(self, strategy, entry):
        strategies = {
            'golden': [round(entry * (1 + i*0.05), 2) for i in range(1,5)],
            'earthquake': [round(entry * (1 + i*0.08), 2) for i in range(1,3)]
        }
        return strategies.get(strategy, [])

    async def send_alert(self, opportunity):
        session = Session()
        try:
            groups = session.query(Group).filter(
                Group.settings['strategies'][opportunity.strategy].as_boolean(),
                Group.is_approved == True
            ).all()
            
            text = (
                f"🚨 فرصة {self.get_strategy_name(opportunity.strategy)} جديدة!\n"
                f"📈 السهم: {opportunity.symbol}\n"
                f"💰 السعر: {opportunity.entry_price:.2f}\n"
                f"🎯 الأهداف: {', '.join(map(str, opportunity.targets))}\n"
                f"🛑 وقف الخسارة: {opportunity.stop_loss:.2f}"
            )
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=text
                )
        finally:
            session.close()

    def get_strategy_name(self, strategy):
        names = {
            'golden': 'ذهبية 💰',
            'earthquake': 'زلزالية 🌋'
        }
        return names.get(strategy, '')

    # Reporting System
    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            for symbol in STOCK_SYMBOLS:
                data = self.fetch_stock_data(symbol, '1d', '1d')
                change = ((data['Close'].iloc[-1] - data['Open'].iloc[0]) / data['Open'].iloc[0]) * 100
                report += f"{symbol}: {change:.2f}%\n"
            
            groups = session.query(Group).filter(
                Group.settings['reports']['daily'].as_boolean(),
                Group.is_approved == True
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report
                )
        finally:
            session.close()

    # Subscription Management
    async def approve_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return await update.message.reply_text("⛔ صلاحية مطلوبة!")
        
        try:
            _, chat_id = update.message.text.split()
            session = Session()
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group:
                group = Group(chat_id=chat_id)
                session.add(group)
            
            group.is_approved = True
            group.subscription_end = datetime.now(SAUDI_TIMEZONE) + timedelta(days=30)
            session.commit()
            
            await update.message.reply_text(f"✅ تم تفعيل المجموعة {chat_id}")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="🎉 تمت الموافقة على مجموعتك!"
            )
        except Exception as e:
            logging.error(f"Approval Error: {str(e)}")
        finally:
            session.close()

    # Button Handlers
    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == 'settings':
            await self.settings(update, context)
        elif query.data == 'edit_reports':
            await self.edit_reports(query)
        elif query.data == 'close':
            await query.message.delete()

    async def edit_reports(self, query):
        session = Session()
        try:
            chat_id = query.message.chat.id
            group = session.query(Group).filter_by(chat_id=str(chat_id)).first()
            
            keyboard = [
                [
                    InlineKeyboardButton(f"الساعية {'✅' if group.settings['reports']['hourly'] else '❌'}", 
                     callback_data='toggle_hourly'),
                    InlineKeyboardButton(f"اليومية {'✅' if group.settings['reports']['daily'] else '❌'}",
                     callback_data='toggle_daily')
                ],
                [
                    InlineKeyboardButton(f"الأسبوعية {'✅' if group.settings['reports']['weekly'] else '❌'}",
                     callback_data='toggle_weekly'),
                    InlineKeyboardButton("رجوع", callback_data='settings')
                ]
            ]
            
            await query.edit_message_text(
                "🛠 تعديل إعدادات التقارير:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        finally:
            session.close()

    # Deployment Setup
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