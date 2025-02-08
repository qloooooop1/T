import os
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
import talib
import requests

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
TRADING_HOURS = {'start': (9, 30), 'end': (15, 0)}
STOCK_SYMBOLS = ['1211', '2222', '3030', '4200']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
NEWS_API_KEY = os.environ.get('NEWS_API_KEY')

DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0)
Session = sessionmaker(bind=engine)

# ------------------ Database Models ------------------
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    daily_limit = Column(Integer, default=5)
    messages_sent = Column(Integer, default=0)
    last_message = Column(DateTime)

class GroupSettings(Base):
    __tablename__ = 'groups'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True)
    subscription_end = Column(DateTime)
    is_approved = Column(Boolean, default=False)
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {
            'golden': True, 'earthquake': True, 
            'volcano': True, 'lightning': True
        },
        'protection': {
            'max_messages': 100, 'antiflood': True,
            'max_warnings': 3, 'mute_duration': 3600
        }
    })
    opportunities = relationship('Opportunity', backref='group')

class Opportunity(Base):
    __tablename__ = 'opportunities'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(4))
    strategy = Column(String)
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    current_target = Column(Integer, default=0)
    status = Column(String, default='active')
    message_id = Column(Integer)
    group_id = Column(Integer, ForeignKey('groups.id'))
    created_at = Column(DateTime)

class PendingApproval(Base):
    __tablename__ = 'approvals'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    requester_id = Column(String)
    requested_at = Column(DateTime)
    handled = Column(Boolean, default=False)

Base.metadata.create_all(engine)

class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.setup_handlers()
        self.setup_scheduler()
        
    def setup_handlers(self):
        handlers = [
            CommandHandler('start', self.start),
            CommandHandler('settings', self.settings),
            CommandHandler('report', self.report),
            CallbackQueryHandler(self.handle_button, pattern=r'^settings::'),
            CallbackQueryHandler(self.handle_approval, pattern=r'^approve::|^deny::'),
            CallbackQueryHandler(self.handle_opportunity, pattern=r'^target::|^close::')
        ]
        for handler in handlers:
            self.app.add_handler(handler)
    
    def setup_scheduler(self):
        jobs = [
            {'func': self.check_opportunities, 'trigger': 'interval', 'minutes': 15},
            {'func': self.track_targets, 'trigger': 'interval', 'minutes': 5},
            {'func': self.send_hourly_report, 'trigger': CronTrigger(minute=0)},
            {'func': self.send_daily_report, 'trigger': CronTrigger(hour=15, minute=30)},
            {'func': self.send_weekly_report, 'trigger': CronTrigger(day_of_week=6, hour=17)}
        ]
        for job in jobs:
            self.scheduler.add_job(job['func'], job['trigger'])
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        msg = f"مرحبا {user.mention_html()}!\n" + \
              "✨ البوت المتقدم لتحليل الأسهم السعودية\n" + \
              "📈 إحصائيات حية - 📊 تقارير مفصلة - 🔔 تنبيهات فورية"
        
        buttons = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings::main'),
             InlineKeyboardButton("الدعم 📞", url='t.me/support')],
            [InlineKeyboardButton("الفرص الذهبية 💰", callback_data='opportunity::golden')]
        ]
        
        await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(buttons))
    
    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        try:
            chat_id = str(update.effective_chat.id)
            group = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
            
            if not group or not group.is_approved:
                return await update.message.reply_text("⚠️ المجموعة غير مفعلة، يرجى التواصل مع الدعم")
                
            settings = group.settings
            text = "⚙️ إعدادات المجموعة:\n\n"
            text += f"📊 التقارير: {'✅' if settings['reports']['hourly'] else '❌'} ساعة - " + \
                    f"{'✅' if settings['reports']['daily'] else '❌'} يومية\n"
            text += f"🔍 الاستراتيجيات: {'✅' if settings['strategies']['golden'] else '❌'} ذهبية - " + \
                    f"{'✅' if settings['strategies']['earthquake'] else '❌'} زلزالية\n"
            
            buttons = [
                [InlineKeyboardButton("تعديل التقارير", callback_data='settings::reports')],
                [InlineKeyboardButton("إدارة الاستراتيجيات", callback_data='settings::strategies')],
                [InlineKeyboardButton("إغلاق", callback_data='settings::close')]
            ]
            
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            session.close()
    
    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data.split('::')[1]
        
        if data == 'reports':
            await self.edit_report_settings(query)
        elif data == 'strategies':
            await self.edit_strategy_settings(query)
        elif data == 'close':
            await query.message.delete()
    
    async def edit_report_settings(self, query):
        session = Session()
        try:
            group = session.query(GroupSettings).filter_by(chat_id=str(query.message.chat_id)).first()
            current = group.settings['reports']
            
            buttons = [
                [InlineKeyboardButton(f"التقارير الساعية {'✅' if current['hourly'] else '❌'}", 
                 callback_data='toggle::reports::hourly')],
                [InlineKeyboardButton(f"التقارير اليومية {'✅' if current['daily'] else '❌'}", 
                 callback_data='toggle::reports::daily')],
                [InlineKeyboardButton("رجوع", callback_data='settings::main')]
            ]
            
            await query.edit_message_text("🛠 تعديل إعدادات التقارير:", 
                reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            session.close()
    
    async def check_opportunities(self):
        session = Session()
        try:
            symbols = self.get_updated_symbols()
            for symbol in symbols:
                data = yf.download(f"{symbol}.SR", period='1d', interval='5m')
                if self.detect_golden_opportunity(data):
                    await self.create_opportunity(symbol, 'golden', data)
                if self.detect_earthquake(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
        except Exception as e:
            logging.error(f"Opportunity error: {str(e)}")
        finally:
            session.close()
    
    def detect_golden_opportunity(self, data):
        ma50 = talib.SMA(data['Close'], timeperiod=50)
        ma200 = talib.SMA(data['Close'], timeperiod=200)
        return ma50.iloc[-1] > ma200.iloc[-1] and ma50.iloc[-2] <= ma200.iloc[-2]
    
    def detect_earthquake(self, data):
        high = talib.MAX(data['High'], timeperiod=14)
        low = talib.MIN(data['Low'], timeperiod=14)
        return data['Close'].iloc[-1] > high.iloc[-2] or data['Close'].iloc[-1] < low.iloc[-2]
    
    async def create_opportunity(self, symbol, strategy, data):
        session = Session()
        try:
            entry = data['Close'].iloc[-1]
            targets = self.calculate_targets(strategy, entry)
            stop_loss = self.calculate_stop_loss(strategy, data)
            
            opp = Opportunity(
                symbol=symbol,
                strategy=strategy,
                entry_price=entry,
                targets=targets,
                stop_loss=stop_loss,
                created_at=datetime.now(SAUDI_TIMEZONE)
            )
            
            session.add(opp)
            session.commit()
            
            groups = session.query(GroupSettings).filter(
                GroupSettings.settings['strategies'][strategy].as_boolean()
            ).all()
            
            for group in groups:
                await self.send_opportunity_alert(group.chat_id, opp)
                
        finally:
            session.close()
    
    async def send_opportunity_alert(self, chat_id, opportunity):
        text = f"🚨 فرصة {opportunity.strategy} جديدة!\n"
        text += f"📈 السهم: {opportunity.symbol}\n"
        text += f"💰 السعر: {opportunity.entry_price:.2f}\n"
        text += f"🎯 الأهداف: {' → '.join(map(str, opportunity.targets))}\n"
        text += f"🛑 وقف الخسارة: {opportunity.stop_loss:.2f}"
        
        buttons = [
            [InlineKeyboardButton("متابعة الأهداف", callback_data=f"target::{opportunity.id}")],
            [InlineKeyboardButton("إغلاق الفرصة", callback_data=f"close::{opportunity.id}")]
        ]
        
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    async def track_targets(self):
        session = Session()
        try:
            opportunities = session.query(Opportunity).filter_by(status='active').all()
            for opp in opportunities:
                current_price = yf.Ticker(f"{opp.symbol}.SR").history(period='1d').iloc[-1].Close
                if current_price >= opp.targets[opp.current_target]:
                    await self.update_target(opp, current_price)
                elif current_price <= opp.stop_loss:
                    await self.close_opportunity(opp, "وقف خسارة")
        finally:
            session.close()
    
    async def update_target(self, opportunity, price):
        session = Session()
        try:
            opportunity.current_target += 1
            if opportunity.current_target >= len(opportunity.targets):
                await self.close_opportunity(opportunity, "تحقيق كافة الأهداف")
            else:
                new_target = opportunity.targets[opportunity.current_target]
                text = f"✅ تم تحقيق الهدف {opportunity.current_target}\n"
                text += f"🎯 الهدف التالي: {new_target:.2f}"
                await self.app.bot.edit_message_text(
                    text,
                    chat_id=opportunity.group.chat_id,
                    message_id=opportunity.message_id
                )
            session.commit()
        finally:
            session.close()
    
    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            top_gainers = self.get_top_movers(ascending=False)
            top_losers = self.get_top_movers(ascending=True)
            
            report += "🏆 أعلى 5 شركات:\n" + "\n".join(
                [f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers)]
            )
            
            report += "\n\n🔻 أقل 5 شركات:\n" + "\n".join(
                [f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_losers)]
            )
            
            groups = session.query(GroupSettings).filter(
                GroupSettings.settings['reports']['daily'].as_boolean()
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report
                )
        finally:
            session.close()
    
    def get_top_movers(self, ascending=False):
        movers = []
        for symbol in STOCK_SYMBOLS:
            data = yf.Ticker(f"{symbol}.SR").history(period='1d')
            change = (data.Close.iloc[-1] - data.Open.iloc[-1]) / data.Open.iloc[-1] * 100
            movers.append((symbol, round(change, 2)))
        return sorted(movers, key=lambda x: x[1], reverse=not ascending)[:5]
    
    async def handle_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        action, approval_id = query.data.split('::')
        
        session = Session()
        try:
            approval = session.query(PendingApproval).get(int(approval_id))
            if approval and not approval.handled:
                approval.handled = True
                if action == 'approve':
                    group = GroupSettings(
                        chat_id=approval.chat_id,
                        is_approved=True,
                        subscription_end=datetime.now(SAUDI_TIMEZONE) + timedelta(days=30)
                    )
                    session.add(group)
                    await query.message.reply_text(f"✅ تم تفعيل المجموعة {approval.chat_id}")
                session.commit()
        finally:
            session.close()
        await query.answer()
    
    def calculate_targets(self, strategy, entry):
        strategies = {
            'golden': [entry * 1.05, entry * 1.10, entry * 1.15, entry * 1.20],
            'earthquake': [entry * 1.10, entry * 1.25, entry * 1.40],
            'volcano': [entry * 1.15, entry * 1.30, entry * 1.45]
        }
        return strategies.get(strategy, [entry * 1.10])
    
    def calculate_stop_loss(self, strategy, data):
        if strategy == 'golden':
            return data['Low'].iloc[-2]
        elif strategy == 'earthquake':
            return data['Close'].iloc[-1] * 0.98
        else:
            return data['Close'].iloc[-1] * 0.95
    
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
    logging.basicConfig(level=logging.INFO)
    bot = SaudiStockBot()
    asyncio.run(bot.run())