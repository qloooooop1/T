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
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
import requests
from bs4 import BeautifulSoup

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') + "/" + TOKEN
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = [s+'.SR' for s in ['1211', '2222', '3030', '4200']]
OWNER_ID = int(os.environ.get('OWNER_ID'))
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)
NEWS_API_KEY = os.environ.get('NEWS_API_KEY')

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
    message_id = Column(Integer)
    group_id = Column(Integer, ForeignKey('groups.id'))
    group = relationship('Group', back_populates='opportunities')
    created_at = Column(DateTime, default=datetime.now(SAUDI_TIMEZONE))

class ApprovalRequest(Base):
    __tablename__ = 'approval_requests'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    requester_id = Column(String)
    requested_at = Column(DateTime, default=datetime.now(SAUDI_TIMEZONE))
    handled = Column(Boolean, default=False)

class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.id'))
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    is_active = Column(Boolean, default=True)

Base.metadata.create_all(engine)

class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.setup_handlers()
        self.setup_scheduler()

    # ------------------ Handlers Setup ------------------
    def setup_handlers(self):
        handlers = [
            CommandHandler('start', self.start),
            CommandHandler('settings', self.settings),
            CommandHandler('approve', self.approve_group),
            CommandHandler('report', self.generate_report),
            CallbackQueryHandler(self.handle_button, pattern=r'^settings_|^opportunity_|^approve_|^close_|^renew_|^target_')
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    # ------------------ Scheduler Setup ------------------
    def setup_scheduler(self):
        jobs = [
            {'func': self.check_opportunities, 'trigger': 'interval', 'minutes': 5},
            {'func': self.send_hourly_report, 'trigger': CronTrigger(minute=0)},
            {'func': self.send_daily_report, 'trigger': CronTrigger(hour=15, minute=30)},
            {'func': self.send_weekly_report, 'trigger': CronTrigger(day_of_week='sun', hour=16)},
            {'func': self.check_subscriptions, 'trigger': 'interval', 'hours': 1},
            {'func': self.price_alerts, 'trigger': 'interval', 'minutes': 3}
        ]
        for job in jobs:
            self.scheduler.add_job(job['func'], job['trigger'])

    # ------------------ Core Handlers ------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        keyboard = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings_main'),
             InlineKeyboardButton("التقارير 📊", callback_data='reports_menu')],
            [InlineKeyboardButton("الدعم الفني 🛠", url='t.me/support')]
        ]
        await update.message.reply_html(
            f"مرحبًا {user.mention_html()}! 👑\n"
            "بوت الأسهم السعودية المتقدم مع التحليل الفني والتنبيهات الذكية",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        try:
            chat_id = str(update.effective_chat.id)
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group or not group.is_approved:
                keyboard = [[InlineKeyboardButton("طلب الموافقة", callback_data='request_approval')]]
                return await update.message.reply_text(
                    "⛔ المجموعة غير مفعلة! يلزم موافقة المالك",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            settings_text = (
                "⚙️ <b>إعدادات المجموعة:</b>\n\n"
                f"📊 <i>التقارير:</i>\n"
                f"- ساعية: {'✅' if group.settings['reports']['hourly'] else '❌'}\n"
                f"- يومية: {'✅' if group.settings['reports']['daily'] else '❌'}\n"
                f"- أسبوعية: {'✅' if group.settings['reports']['weekly'] else '❌'}\n\n"
                f"🔍 <i>الاستراتيجيات:</i>\n"
                f"- ذهبية: {'✅' if group.settings['strategies']['golden'] else '❌'}\n"
                f"- زلزالية: {'✅' if group.settings['strategies']['earthquake'] else '❌'}\n"
                f"- بركانية: {'✅' if group.settings['strategies']['volcano'] else '❌'}\n"
                f"- برقية: {'✅' if group.settings['strategies']['lightning'] else '❌'}\n"
            )

            buttons = [
                [InlineKeyboardButton("تعديل التقارير", callback_data='edit_reports'),
                 InlineKeyboardButton("تعديل الاستراتيجيات", callback_data='edit_strategies')],
                [InlineKeyboardButton("إغلاق", callback_data='close_settings')]
            ]
            
            await update.message.reply_text(
                settings_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logging.error(f"Settings Error: {str(e)}")
        finally:
            session.close()

    # ------------------ Opportunity System ------------------
    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period='1d', interval='15m')
                if len(data) < 50: continue

                # Golden Cross Strategy
                if self.detect_golden_cross(data):
                    await self.create_opportunity(symbol, 'golden', data)
                
                # Earthquake Strategy (Breakout)
                if self.detect_breakout(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
                
                # Volcano Strategy (Fibonacci)
                if self.detect_fibonacci(data):
                    await self.create_opportunity(symbol, 'volcano', data)
                
                # Lightning Strategy (Pattern)
                if self.detect_pattern(data):
                    await self.create_opportunity(symbol, 'lightning', data)

        except Exception as e:
            logging.error(f"Opportunity Check Error: {str(e)}")
        finally:
            session.close()

    def detect_golden_cross(self, data):
        ema50 = ta.ema(data['Close'], length=50)
        ema200 = ta.ema(data['Close'], length=200)
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]

    def detect_breakout(self, data):
        return (data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2] and
                data['Volume'].iloc[-1] > data['Volume'].rolling(20).mean().iloc[-1] * 2)

    def detect_fibonacci(self, data):
        fib_levels = self.calculate_fibonacci(data)
        return data['Close'].iloc[-1] > fib_levels['61.8%']

    def detect_pattern(self, data):
        pattern = ta.cdl_pattern(data['Open'], data['High'], data['Low'], data['Close'])
        return any(pattern.iloc[-1] != 0)

    # ------------------ Reporting System ------------------
    async def send_hourly_report(self):
        session = Session()
        try:
            report = "📈 <b>تقرير الساعة:</b>\n\n"
            top_gainers = await self.get_top_movers('1h')
            
            report += "🏆 <i>أعلى 5 شركات:</i>\n"
            report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[:5])])
            
            report += "\n\n🔻 <i>أقل 5 شركات:</i>\n"
            report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[-5:])])
            
            groups = session.query(Group).filter(
                Group.settings['reports']['hourly'].as_boolean(),
                Group.is_approved == True
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report,
                    parse_mode=ParseMode.HTML
                )
        finally:
            session.close()

    async def send_weekly_report(self):
        session = Session()
        try:
            report = "📅 <b>تقرير أسبوعي:</b>\n\n"
            opportunities = session.query(Opportunity).filter(
                Opportunity.created_at >= datetime.now(SAUDI_TIMEZONE) - timedelta(days=7)
            ).all()
            
            report += f"🔍 عدد الفرص: {len(opportunities)}\n"
            report += f"✅ الفرص الناجحة: {len([o for o in opportunities if o.status == 'completed'])}\n"
            report += f"📉 الفرص المغلقة: {len([o for o in opportunities if o.status == 'closed'])}\n\n"
            
            report += "🎯 <i>أفضل 5 فرص:</i>\n"
            top_opps = sorted(opportunities, key=lambda x: x.targets[-1] - x.entry_price, reverse=True)[:5]
            report += "\n".join([f"{o.symbol}: {o.strategy} (+{(o.targets[-1]-o.entry_price)/o.entry_price*100:.2f}%)" for o in top_opps])

            groups = session.query(Group).filter(
                Group.settings['reports']['weekly'].as_boolean(),
                Group.is_approved == True
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report,
                    parse_mode=ParseMode.HTML
                )
        finally:
            session.close()

    # ------------------ Subscription Management ------------------
    async def check_subscriptions(self):
        session = Session()
        try:
            groups = session.query(Group).filter(
                Group.subscription_end <= datetime.now(SAUDI_TIMEZONE) + timedelta(days=3),
                Group.is_approved == True
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=f"⚠️ اشتراكك ينتهي في {group.subscription_end.strftime('%Y-%m-%d')}\n"
                         "يرجى التواصل مع الدعم لتجديد الاشتراك",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("تجديد الاشتراك", callback_data=f"renew_{group.id}")]
                    ])
                )
        finally:
            session.close()

    # ------------------ Approval System ------------------
    async def approve_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return await update.message.reply_text("⛔ فقط المالك يمكنه تنفيذ هذا الأمر!")
        
        try:
            _, chat_id = update.message.text.split()
            session = Session()
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group:
                group = Group(chat_id=chat_id, is_approved=True)
                session.add(group)
            
            group.is_approved = True
            group.subscription_end = datetime.now(SAUDI_TIMEZONE) + timedelta(days=30)
            session.commit()
            
            await update.message.reply_text(f"✅ تم تفعيل المجموعة {chat_id}")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="🎉 تمت الموافقة على مجموعتك! يمكنك الآن استخدام البوت بالكامل."
            )
        except Exception as e:
            logging.error(f"Approval Error: {str(e)}")
            await update.message.reply_text("❌ حدث خطأ أثناء التفعيل")
        finally:
            session.close()

    # ------------------ Button Handlers ------------------
    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        
        try:
            if data == 'edit_reports':
                await self.edit_report_settings(query)
            elif data == 'edit_strategies':
                await self.edit_strategy_settings(query)
            elif data.startswith('approve_'):
                await self.handle_approval(query)
            elif data.startswith('renew_'):
                await self.renew_subscription(query)
            elif data.startswith('target_'):
                await self.update_target(query)
            elif data == 'close_settings':
                await query.message.delete()
        except Exception as e:
            logging.error(f"Button Handler Error: {str(e)}")

    async def edit_report_settings(self, query):
        session = Session()
        try:
            chat_id = query.message.chat.id
            group = session.query(Group).filter_by(chat_id=str(chat_id)).first()
            
            current_settings = group.settings['reports']
            keyboard = [
                [
                    InlineKeyboardButton(f"الساعية {'✅' if current_settings['hourly'] else '❌'}", 
                     callback_data='toggle_hourly'),
                    InlineKeyboardButton(f"اليومية {'✅' if current_settings['daily'] else '❌'}",
                     callback_data='toggle_daily')
                ],
                [
                    InlineKeyboardButton(f"الأسبوعية {'✅' if current_settings['weekly'] else '❌'}",
                     callback_data='toggle_weekly'),
                    InlineKeyboardButton("رجوع", callback_data='settings_main')
                ]
            ]
            
            await query.edit_message_text(
                "🛠 تعديل إعدادات التقارير:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        finally:
            session.close()

    # ------------------ Utility Functions ------------------
    def calculate_fibonacci(self, data):
        high = data['High'].max()
        low = data['Low'].min()
        diff = high - low
        return {
            '23.6%': high - diff * 0.236,
            '38.2%': high - diff * 0.382,
            '61.8%': high - diff * 0.618
        }

    async def get_top_movers(self, period):
        movers = []
        for symbol in STOCK_SYMBOLS:
            data = yf.download(symbol, period=period)
            if len(data) < 2: continue
            change = ((data['Close'].iloc[-1] - data['Open'].iloc[0]) / data['Open'].iloc[0]) * 100
            movers.append((symbol, round(change, 2)))
        return sorted(movers, key=lambda x: x[1], reverse=True)

    # ------------------ Deployment Setup ------------------
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