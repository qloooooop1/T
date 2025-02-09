import os
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
import re
import random

# ------------------ Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')  # Use environment variables for security
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')  # Heroku URL
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))  # Owner's actual ID
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)

# Environment variables for group activation
ACTIVATED_GROUPS = os.environ.get('ACTIVATED_GROUPS', '').split(',')

# Initialize database
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# ------------------ Database Models ------------------
class Group(Base):
    __tablename__ = 'groups'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True)
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {
            'golden': True, 'earthquake': True,
            'volcano': True, 'lightning': True
        },
        'protection': {
            'delete_phone_numbers': True,
            'delete_whatsapp_links': True,
            'delete_telegram_links': True,
            'mute_duration': 3  # Default mute duration in days
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
        self.app.add_handler(CommandHandler('analyze', self.analyze))
        self.app.add_handler(CallbackQueryHandler(self.handle_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    # ------------------ Scheduler Setup ------------------
    def setup_scheduler(self):
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=5)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=16, minute=0))

    # ------------------ Core Handlers ------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings'),
             InlineKeyboardButton("التقارير 📊", callback_data='reports')],
            [InlineKeyboardButton("الدعم الفني 📞", url='https://t.me/support')]
        ]
        chat_id = str(update.effective_chat.id)
        if chat_id not in ACTIVATED_GROUPS:
            await update.message.reply_text("⚠️ يجب تفعيل المجموعة لاستخدام البوت. الرجاء التواصل مع الدعم الفني.")
            return

        await update.message.reply_text(
            "مرحبًا بكم في بوت الأسهم السعودية المتقدم! 📈",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        message = update.message or update.callback_query.message  # Handle both cases

        if chat_id not in ACTIVATED_GROUPS:
            await message.reply_text("⚠️ يجب تفعيل المجموعة لاستخدام البوت. الرجاء التواصل مع الدعم الفني.")
            return

        session = Session()
        try:
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group:
                group = Group(chat_id=chat_id)
                session.add(group)
                session.commit()
            
            settings_text = self.format_settings_text(group)
            buttons = self.create_settings_buttons()

            await message.reply_text(
                settings_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logging.error(f"Settings Error: {str(e)}")
        finally:
            session.close()

    def format_settings_text(self, group):
        settings = group.settings
        return (
            "⚙️ إعدادات المجموعة:\n\n"
            f"📊 التقارير:\n"
            f"- ساعية: {'✅' if settings['reports']['hourly'] else '❌'}\n"
            f"- يومية: {'✅' if settings['reports']['daily'] else '❌'}\n"
            f"- أسبوعية: {'✅' if settings['reports']['weekly'] else '❌'}\n\n"
            f"🛡️ الحماية:\n"
            f"- حذف أرقام الجوالات: {'✅' if settings['protection']['delete_phone_numbers'] else '❌'}\n"
            f"- حذف روابط الواتساب: {'✅' if settings['protection']['delete_whatsapp_links'] else '❌'}\n"
            f"- حذف روابط التليجرام: {'✅' if settings['protection']['delete_telegram_links'] else '❌'}\n"
            f"- مدة الكتم: {settings['protection']['mute_duration']} أيام"
        )

    def create_settings_buttons(self):
        return [
            [InlineKeyboardButton("تعديل التقارير", callback_data='edit_reports'),
             InlineKeyboardButton("تعديل الحماية", callback_data='edit_protection')],
            [InlineKeyboardButton("إغلاق", callback_data='close')]
        ]

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
                
                # Volcano Strategy (Fibonacci - simplified here)
                if self.detect_volcano(data):
                    await self.create_opportunity(symbol, 'volcano', data)
                
                # Lightning Strategy (Candlestick Pattern - simplified here)
                if self.detect_lightning(data):
                    await self.create_opportunity(symbol, 'lightning', data)

        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}")
        finally:
            session.close()

    @staticmethod
    def ema(close, window):
        # Simple EMA calculation
        alpha = 2 / (window + 1.0)
        ema = np.zeros_like(close)
        ema[0] = close[0]
        for t in range(1, len(close)):
            ema[t] = alpha * close[t] + (1 - alpha) * ema[t-1]
        return pd.Series(ema, index=close.index)

    def detect_golden_cross(self, data):
        if 'Close' not in data.columns:
            return False

        ema50 = self.ema(data['Close'], 50)
        ema200 = self.ema(data['Close'], 200)
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]

    def detect_earthquake(self, data):
        if 'Close' not in data.columns or 'High' not in data.columns or 'Volume' not in data.columns:
            return False

        return (data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2] and
                data['Volume'].iloc[-1] > data['Volume'].mean() * 2)

    def detect_volcano(self, data):
        if 'High' not in data.columns or 'Low' not in data.columns or 'Close' not in data.columns:
            return False

        high = data['High'].max()
        low = data['Low'].min()
        return data['Close'].iloc[-1] > low + 0.618 * (high - low)  # 61.8% retracement

    def detect_lightning(self, data):
        if 'High' not in data.columns or 'Low' not in data.columns or 'Close' not in data.columns:
            return False

        return data['High'].iloc[-1] - data['Low'].iloc[-1] > data['Close'].iloc[-2] * 0.05  # Example: Large range candle

    # ------------------ Opportunity Management ------------------
    async def create_opportunity(self, symbol, strategy, data):
        session = Session()
        try:
            if data.empty or 'Close' not in data.columns:
                logging.error(f"No data available for symbol {symbol}")
                return

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
        if not entry:
            return []

        strategies = {
            'golden': [round(entry * (1 + i*0.05), 2) for i in range(1,5)],
            'earthquake': [round(entry * (1 + i*0.08), 2) for i in range(1,3)],
            'volcano': [round(entry * (1 + i*0.1), 2) for i in range(1,6)],
            'lightning': [round(entry * (1 + i*0.07), 2) for i in range(1,3)]
        }
        return strategies.get(strategy, [])

    def calculate_stop_loss(self, strategy, data):
        if 'Close' not in data.columns or 'Low' not in data.columns:
            return 0.0

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
                Group.chat_id.in_(ACTIVATED_GROUPS)
            ).all()
            
            if not groups:
                logging.info("No groups found for the alert")
                return
            
            text = (
                f"🚨 إشارة {self.get_strategy_name(opportunity.strategy)}\n"
                f"📈 السهم: {opportunity.symbol}\n"
                f"💰 السعر: {opportunity.entry_price:.2f}\n"
                f"🎯 الأهداف: {', '.join(map(str, opportunity.targets))}\n"
                f"🛑 وقف الخسارة: {opportunity.stop_loss:.2f}"
            )
            
            for group in groups:
                try:
                    await self.app.bot.send_message(
                        chat_id=group.chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logging.error(f"Failed to send alert to group {group.chat_id}: {e}")

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
            
            if not top_gainers:
                report += "⚠️ لا توجد بيانات متاحة اليوم.\n"
            else:
                report += "🏆 أعلى 5 شركات:\n"
                report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[:5])])
                
                report += "\n\n🔻 أقل 5 شركات:\n"
                report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[-5:])])
            
            groups = session.query(Group).filter(
                Group.settings['reports']['daily'].as_boolean(),
                Group.chat_id.in_(ACTIVATED_GROUPS)
            ).all()
            
            for group in groups:
                try:
                    await self.app.bot.send_message(
                        chat_id=group.chat_id,
                        text=report
                    )
                except Exception as e:
                    logging.error(f"Failed to send daily report to group {group.chat_id}: {e}")

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

    # ------------------ Button Handlers ------------------
    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == 'settings':
            await self.settings(update, context)
        elif query.data == 'edit_reports':
            await self.edit_reports(query)
        elif query.data == 'edit_protection':
            await self.edit_protection(query)
        elif query.data == 'close':
            await query.message.delete()

    async def edit_reports(self, query):
        session = Session()
        try:
            chat_id = query.message.chat.id
            group = session.query(Group).filter_by(chat_id=str(chat_id)).first()
            
            if not group:
                await query.message.reply_text("⚠️ المجموعة غير موجودة في قاعدة البيانات")
                return
            
            keyboard = self.create_report_edit_buttons(group.settings)
            
            await query.edit_message_text(
                "🛠 تعديل إعدادات التقارير:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logging.error(f"Edit Reports Error: {str(e)}")
        finally:
            session.close()

    def create_report_edit_buttons(self, settings):
        if not settings or 'reports' not in settings:
            return []

        return [
            [
                InlineKeyboardButton(f"الساعية {'✅' if settings['reports']['hourly'] else '❌'}", 
                 callback_data='toggle_hourly'),
                InlineKeyboardButton(f"اليومية {'✅' if settings['reports']['daily'] else '❌'}",
                 callback_data='toggle_daily')
            ],
            [
                InlineKeyboardButton(f"الأسبوعية {'✅' if settings['reports']['weekly'] else '❌'}",
                 callback_data='toggle_weekly'),
                InlineKeyboardButton("رجوع", callback_data='settings')
            ]
        ]

    async def edit_protection(self, query):
        session = Session()
        try:
            chat_id = query.message.chat.id
            group = session.query(Group).filter_by(chat_id=str(chat_id)).first()
            
            if not group:
                await query.message.reply_text("⚠️ المجموعة غير موجودة في قاعدة البيانات")
                return
            
            keyboard = self.create_protection_edit_buttons(group.settings)
            
            await query.edit_message_text(
                "🛠 تعديل إعدادات الحماية:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logging.error(f"Edit Protection Error: {str(e)}")
        finally:
            session.close()

    def create_protection_edit_buttons(self, settings):
        if not settings or 'protection' not in settings:
            return []

        return [
            [
                InlineKeyboardButton(f"حذف أرقام الجوالات {'✅' if settings['protection']['delete_phone_numbers'] else '❌'}", 
                 callback_data='toggle_delete_phone_numbers'),
                InlineKeyboardButton(f"حذف روابط الواتساب {'✅' if settings['protection']['delete_whatsapp_links'] else '❌'}",
                 callback_data='toggle_delete_whatsapp_links')
            ],
            [
                InlineKeyboardButton(f"حذف روابط التليجرام {'✅' if settings['protection']['delete_telegram_links'] else '❌'}",
                 callback_data='toggle_delete_telegram_links'),
                InlineKeyboardButton(f"مدة الكتم: {settings['protection']['mute_duration']} أيام",
                 callback_data='edit_mute_duration')
            ],
            [
                InlineKeyboardButton("رجوع", callback_data='settings')
            ]
        ]

    # ------------------ Message Handler ------------------
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        message = update.message

        if chat_id not in ACTIVATED_GROUPS:
            return

        session = Session()
        try:
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group:
                return

            # Check for phone numbers
            if group.settings['protection']['delete_phone_numbers']:
                phone_numbers = re.findall(r'\+9665\d{8}|\+9665\d{8}|\+9665\d{8}', message.text)
                if phone_numbers:
                    await self.delete_message_and_mute_user(message, group)
                    return

            # Check for WhatsApp links
            if group.settings['protection']['delete_whatsapp_links']:
                whatsapp_links = re.findall(r'https?://wa\.me/\d+|https?://chat\.whatsapp\.com/\w+', message.text)
                if whatsapp_links:
                    await self.delete_message_and_mute_user(message, group)
                    return

            # Check for Telegram links
            if group.settings['protection']['delete_telegram_links']:
                telegram_links = re.findall(r'https?://t\.me/\w+|https?://telegram\.me/\w+', message.text)
                if telegram_links:
                    await self.delete_message_and_mute_user(message, group)
                    return

        except Exception as e:
            logging.error(f"Message Handling Error: {str(e)}")
        finally:
            session.close()

    async def delete_message_and_mute_user(self, message, group):
        try:
            await message.delete()
            mute_duration = timedelta(days=group.settings['protection']['mute_duration'])
            await message.chat.restrict_member(
                message.from_user.id,
                until_date=datetime.now(SAUDI_TIMEZONE) + mute_duration,
                permissions=None
            )
            mock_messages = [
                f"@{message.from_user.username} يا حبيبي، ما نسمح بالإعلانات هنا! 😅",
                f"@{message.from_user.username} شكلك جديد هنا، الإعلانات ممنوعة! 🚫",
                f"@{message.from_user.username} الإعلانات ممنوعة، خلك في حدود الأدب! 😉",
                f"@{message.from_user.username} يا عمري، الإعلانات ممنوعة! 🤣",
                f"@{message.from_user.username} شكلك تحب الإعلانات، بس هنا ممنوع! 😂"
            ]
            await message.reply_text(random.choice(mock_messages))
        except Exception as e:
            logging.error(f"Failed to delete message and mute user: {e}")

    # ------------------ Analyze Command ------------------
    async def analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        message = update.message

        if chat_id not in ACTIVATED_GROUPS:
            await message.reply_text("⚠️ يجب تفعيل المجموعة لاستخدام البوت. الرجاء التواصل مع الدعم الفني.")
            return

        symbol = message.text.split()[1] if len(message.text.split()) > 1 else None
        if not symbol or len(symbol) != 4 or not symbol.isdigit():
            await message.reply_text("⚠️ الرجاء إدخال رمز شركة صحيح مكون من 4 أرقام.")
            return

        symbol += '.SR'
        if symbol not in STOCK_SYMBOLS:
            await message.reply_text("⚠️ الرمز غير موجود في قائمة الشركات المدعومة.")
            return

        try:
            data = yf.download(symbol, period='1d', interval='1h')
            if data.empty:
                await message.reply_text("⚠️ لا توجد بيانات متاحة للشركة.")
                return

            analysis = self.perform_technical_analysis(data)
            await message.reply_text(analysis, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Analyze Error: {str(e)}")
            await message.reply_text("⚠️ حدث خطأ أثناء تحليل الشركة.")

    def perform_technical_analysis(self, data):
        close = data['Close']
        high = data['High']
        low = data['Low']
        volume = data['Volume']

        ema50 = self.ema(close, 50)
        ema200 = self.ema(close, 200)

        analysis = (
            f"📈 <b>التحليل الفني للشركة:</b>\n\n"
            f"💰 <b>السعر الحالي:</b> {close.iloc[-1]:.2f}\n"
            f"📊 <b>أعلى سعر اليوم:</b> {high.iloc[-1]:.2f}\n"
            f"📉 <b>أقل سعر اليوم:</b> {low.iloc[-1]:.2f}\n"
            f"📈 <b>متوسط 50 يوم:</b> {ema50.iloc[-1]:.2f}\n"
            f"📊 <b>متوسط 200 يوم:</b> {ema200.iloc[-1]:.2f}\n\n"
            f"📈 <b>الاتجاه العام:</b> {'صاعد' if close.iloc[-1] > ema200.iloc[-1] else 'هابط'}\n"
            f"📊 <b>الدعوم والمقاومات:</b>\n"
            f"- الدعم القريب: {low.iloc[-1]:.2f}\n"
            f"- المقاومة القريبة: {high.iloc[-1]:.2f}\n\n"
            f"📈 <b>الأهداف:</b>\n"
            f"- الهدف القريب: {close.iloc[-1] * 1.05:.2f}\n"
            f"- الهدف المتوسط: {close.iloc[-1] * 1.1:.2f}\n"
            f"- الهدف البعيد: {close.iloc[-1] * 1.2:.2f}\n\n"
            f"📉 <b>وقف الخسارة:</b> {close.iloc[-1] * 0.95:.2f}"
        )
        return analysis

    # ------------------ Run Bot ------------------
    async def run(self):
        await self.app.initialize()
        await self.app.start()
        self.scheduler.start()
        
        if WEBHOOK_URL and os.environ.get('PORT'):
            await self.app.updater.start_webhook(
                listen="0.0.0.0",
                port=int(os.environ.get('PORT', 5000)),
                url_path="",
                webhook_url=WEBHOOK_URL
            )
        else:
            await self.app.updater.start_polling()
        
        logging.info("Bot is running...")
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logging.info("Bot is shutting down...")
            await self.app.stop()
            self.scheduler.shutdown(wait=False)

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    bot = SaudiStockBot()
    asyncio.run(bot.run()) 