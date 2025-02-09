import os
import logging
import asyncio
import pandas as pd
import numpy as np  # إعادة استيراد numpy
import yfinance as yf
import pandas_ta as ta  # إعادة استيراد pandas_ta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.environ.get('OWNER_ID'))
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL)
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

    # ------------------ Handlers Setup ------------------
    def setup_handlers(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('settings', self.settings))
        self.app.add_handler(CommandHandler('approve', self.approve_group))
        self.app.add_handler(CallbackQueryHandler(self.handle_button))

    # ------------------ Scheduler Setup ------------------
    def setup_scheduler(self):
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=5)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=16, minute=0))

    # ------------------ Core Handlers ------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings'),
             InlineKeyboardButton("التقارير 📊", callback_data='reports')],
            [InlineKeyboardButton("الدعم الفني 📞", url='t.me/support')]
        ]
        await update.message.reply_text(
            "مرحبًا بكم في بوت الأسهم السعودية المتقدم! 📈",
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

    # ------------------ Opportunity Detection ------------------
    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period='1d', interval='1h')
                if data.empty:
                    continue

                # Golden Cross Strategy
                if self.detect_golden_cross(data):
                    await self.create_opportunity(symbol, 'golden', data)
                
                # Earthquake Strategy (Breakout)
                if self.detect_earthquake(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
                
                # Volcano Strategy (Fibonacci)
                if self.detect_volcano(data):
                    await self.create_opportunity(symbol, 'volcano', data)
                
                # Lightning Strategy (Candlestick Pattern)
                if self.detect_lightning(data):
                    await self.create_opportunity(symbol, 'lightning', data)

        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}")
        finally:
            session.close()

    def detect_golden_cross(self, data):
        ema50 = ta.ema(data['Close'], length=50)
        ema200 = ta.ema(data['Close'], length=200)
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]

    def detect_earthquake(self, data):
        return (data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2] and
                data['Volume'].iloc[-1] > data['Volume'].mean() * 2)

    def detect_volcano(self, data):
        fib_levels = self.calculate_fibonacci(data)
        return data['Close'].iloc[-1] > fib_levels['61.8%']

    def detect_lightning(self, data):
        patterns = ta.cdl_pattern(
            data['Open'], data['High'], 
            data['Low'], data['Close']
        )
        return any(patterns.iloc[-1] != 0)

    # ------------------ Opportunity Management ------------------
    async def create_opportunity(self, symbol, strategy, data):
        session = Session()
        try:
            entry_price = data['Close'].iloc[-1]
            stop_loss = self.calculate_stop_loss(strategy, data)
            targets = self.calculate_targets(strategy, entry_price)
            
            opp = Opportunity(
                symbol=symbol,
                strategy=strategy,
                entry_price=entry_price,
                targets=targets,
                stop_loss=stop_loss,
                created_at=datetime.now(SAUDI_TIMEZONE)
            )
            
            session.add(opp)
            session.commit()
            
            await self.send_alert_to_groups(opp)
        except Exception as e:
            logging.error(f"Create Opportunity Error: {str(e)}")
        finally:
            session.close()

    def calculate_targets(self, strategy, entry):
        strategies = {
            'golden': [round(entry * (1 + i*0.05), 2) for i in range(1,5)],
            'earthquake': [round(entry * (1 + i*0.08), 2) for i in range(1,3)],
            'volcano': [round(entry * (1 + i*0.1), 2) for i in range(1,6)],
            'lightning': [round(entry * (1 + i*0.07), 2) for i in range(1,3)]
        }
        return strategies.get(strategy, [])

    def calculate_stop_loss(self, strategy, data):
        if strategy == 'golden':
            return data['Low'].iloc[-2] * 0.98
        elif strategy == 'earthquake':
            return data['Close'].iloc[-1] * 0.95
        else:
            return data['Close'].iloc[-1] * 0.97

    # ------------------ Alert System ------------------
    async def send_alert_to_groups(self, opportunity):
        session = Session()
        try:
            groups = session.query(Group).filter(
                Group.settings['strategies'][opportunity.strategy].as_boolean(),
                Group.is_approved == True
            ).all()
            
            text = (
                f"🚨 إشارة {self.get_strategy_name(opportunity.strategy)}\n"
                f"📈 السهم: {opportunity.symbol}\n"
                f"💰 السعر: {opportunity.entry_price:.2f}\n"
                f"🎯 الأهداف: {', '.join(map(str, opportunity.targets))}\n"
                f"🛑 وقف الخسارة: {opportunity.stop_loss:.2f}"
            )
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
        finally:
            session.close()

    def get_strategy_name(self, strategy):
        names = {
            'golden': 'ذهبية 💰',
            'earthquake': 'زلزالية 🌋',
            'volcano': 'بركانية 🌋',
            'lightning': 'برقية ⚡'
        }
        return names.get(strategy, '')

    # ------------------ Reporting System ------------------
    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            top_gainers = await self.get_top_movers('1d')
            
            report += "🏆 أعلى 5 شركات:\n"
            report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[:5])])
            
            report += "\n\n🔻 أقل 5 شركات:\n"
            report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[-5:])])
            
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

    async def get_top_movers(self, period):
        movers = []
        for symbol in STOCK_SYMBOLS:
            data = yf.download(symbol, period=period)
            if len(data) < 2:
                continue
            change = ((data['Close'].iloc[-1] - data['Open'].iloc[0]) / data['Open'].iloc[0]) * 100
            movers.append((symbol, round(change, 2)))
        return sorted(movers, key=lambda x: x[1], reverse=True)

    # ------------------ Group Approval ------------------
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

    # ------------------ Button Handlers ------------------
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

    # ------------------ Run Bot ------------------
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