import os
import logging
import asyncio
import re
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
from typing import Dict, Any

# ------------------ Advanced Configuration ------------------
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = f"{os.environ.get('WEBHOOK_URL')}/{TOKEN}"
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.environ.get('OWNER_ID', 0))
DATABASE_URL = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql+psycopg2://", 1)

# ------------------ Database Initialization ------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

class GroupSettings(Base):
    __tablename__ = 'group_settings'
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), unique=True)
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {
            'golden_cross': True,
            'rsi_divergence': True,
            'macd_crossover': True,
            'bollinger_breakout': False
        },
        'protection': {
            'delete_phones': True,
            'delete_links': True,
            'anti_spam': True,
            'mute_duration': 24
        },
        'notifications': {
            'price_alerts': True,
            'volume_spike': True,
            'news_alerts': True
        }
    })
    approvals = relationship("PendingApproval", back_populates="group")

class PendingApproval(Base):
    __tablename__ = 'pending_approvals'
    id = Column(Integer, primary_key=True)
    user_id = Column(String(50))
    chat_id = Column(String(50), ForeignKey('group_settings.chat_id'))
    command = Column(String(100))
    created_at = Column(DateTime)
    handled = Column(Boolean, default=False)
    group = relationship("GroupSettings", back_populates="approvals")

class StockData(Base):
    __tablename__ = 'stock_data'
    symbol = Column(String(10), primary_key=True)
    data = Column(JSON)
    indicators = Column(JSON)
    last_updated = Column(DateTime)

class TradingOpportunity(Base):
    __tablename__ = 'trading_opportunities'
    id = Column(Integer, primary_key=True)
    symbol = Column(String(10))
    strategy = Column(String(50))
    entry_price = Column(Float)
    targets = Column(JSON)
    stop_loss = Column(Float)
    risk_reward = Column(Float)
    status = Column(String(20), default='active')
    message_id = Column(Integer)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

Base.metadata.create_all(engine)

# ------------------ Advanced Technical Analysis Engine ------------------
class AdvancedTA:
    @staticmethod
    def calculate_rsi(data: pd.DataFrame, period=14) -> pd.Series:
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_macd(data: pd.DataFrame, fast=12, slow=26, signal=9) -> Dict[str, pd.Series]:
        ema_fast = data['Close'].ewm(span=fast, adjust=False).mean()
        ema_slow = data['Close'].ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        return {'macd': macd, 'signal': signal_line, 'histogram': macd - signal_line}

    @staticmethod
    def bollinger_bands(data: pd.DataFrame, window=20, num_std=2) -> Dict[str, pd.Series]:
        sma = data['Close'].rolling(window=window).mean()
        std = data['Close'].rolling(window=window).std()
        return {
            'upper': sma + (std * num_std),
            'middle': sma,
            'lower': sma - (std * num_std)
        }

# ------------------ Core Bot Functionality ------------------
class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.ta = AdvancedTA()
        self.setup_handlers()
        self.setup_scheduler()

    # ------------------ Database Operations ------------------
    @staticmethod
    def get_session():
        return Session()

    # ------------------ Handler Setup ------------------
    def setup_handlers(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('settings', self.settings_menu))
        self.app.add_handler(CommandHandler('report', self.generate_report))
        self.app.add_handler(CallbackQueryHandler(self.handle_button, pattern=r'^settings::|^approval::'))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    # ------------------ Scheduler Setup ------------------
    def setup_scheduler(self):
        self.scheduler.add_job(self.update_market_data, 'interval', minutes=15)
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=10)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=15, minute=30, timezone=SAUDI_TIMEZONE))

    # ------------------ Main Menu System ------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        menu = [
            [InlineKeyboardButton("⚙️ الإعدادات المتقدمة", callback_data='settings::main')],
            [InlineKeyboardButton("📈 تقرير فوري", callback_data='report::instant')],
            [InlineKeyboardButton("📢 الدعم الفني", url='t.me/support')]
        ]
        await update.message.reply_text(
            "مرحبا بكم في البوت المتقدم لتحليل الأسهم السعودية",
            reply_markup=InlineKeyboardMarkup(menu),
            parse_mode=ParseMode.MARKDOWN
        )

    # ------------------ Advanced Settings System ------------------
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = self.get_session()
        try:
            user_id = str(update.effective_user.id)
            
            if not await self.verify_ownership(user_id):
                await self.handle_approval_request(update, session, user_id)
                return

            # Dynamic Settings Menu Generation
            group = session.query(GroupSettings).filter_by(chat_id=str(update.effective_chat.id)).first()
            settings = group.settings if group else {}

            menu = self.generate_settings_menu(settings)
            await update.message.reply_text(
                "الإعدادات المتقدمة:",
                reply_markup=InlineKeyboardMarkup(menu),
                parse_mode=ParseMode.MARKDOWN
            )

        except Exception as e:
            logging.error(f"Settings Error: {str(e)}")
            await update.message.reply_text("حدث خطأ في تحميل الإعدادات")
        finally:
            session.close()

    def generate_settings_menu(self, settings: Dict[str, Any]) -> list:
        menu = []
        # Reports Section
        menu.append([InlineKeyboardButton("📊 تقارير التداول", callback_data='settings::reports')])
        # Strategies Section
        menu.append([InlineKeyboardButton("🛠 استراتيجيات التداول", callback_data='settings::strategies')])
        # Protection Section
        menu.append([InlineKeyboardButton("🛡 إعدادات الحماية", callback_data='settings::protection')])
        # Back Button
        menu.append([InlineKeyboardButton("🔙 رجوع", callback_data='settings::main')])
        return menu

    # ------------------ Approval System ------------------
    async def handle_approval_request(self, update: Update, session: Session, user_id: str):
        try:
            new_request = PendingApproval(
                user_id=user_id,
                chat_id=str(update.effective_chat.id),
                command='settings_access',
                created_at=datetime.now(SAUDI_TIMEZONE)
            )
            session.add(new_request)
            session.commit()

            # Send approval request to owner
            approve_btn = InlineKeyboardButton("✅ الموافقة", callback_data=f"approval::approve::{new_request.id}")
            deny_btn = InlineKeyboardButton("❌ الرفض", callback_data=f"approval::deny::{new_request.id}")
            
            await self.app.bot.send_message(
                chat_id=OWNER_ID,
                text=f"طلب وصول جديد من المستخدم: {user_id}",
                reply_markup=InlineKeyboardMarkup([[approve_btn, deny_btn]])
            )
            
            await update.message.reply_text("📬 تم إرسال طلبك للإدارة للموافقة")

        except SQLAlchemyError as e:
            session.rollback()
            logging.error(f"Database Error: {str(e)}")
            await update.message.reply_text("حدث خطأ في نظام الموافقة")

    # ------------------ Market Data Management ------------------
    async def update_market_data(self):
        session = self.get_session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period="1y", interval="1d")
                if data.empty:
                    continue

                # Calculate advanced indicators
                indicators = {
                    'rsi': self.ta.calculate_rsi(data).iloc[-1],
                    'macd': self.ta.calculate_macd(data),
                    'bollinger': self.ta.bollinger_bands(data)
                }

                # Update database
                stock = session.query(StockData).filter_by(symbol=symbol).first()
                if not stock:
                    stock = StockData(symbol=symbol)
                
                stock.data = data.to_json()
                stock.indicators = indicators
                stock.last_updated = datetime.now(SAUDI_TIMEZONE)
                session.add(stock)
            
            session.commit()
        except Exception as e:
            logging.error(f"Market Data Error: {str(e)}")
            session.rollback()
        finally:
            session.close()

    # ------------------ Trading Opportunity Engine ------------------
    async def check_opportunities(self):
        session = self.get_session()
        try:
            for symbol in STOCK_SYMBOLS:
                stock = session.query(StockData).filter_by(symbol=symbol).first()
                if not stock:
                    continue

                data = pd.read_json(stock.data)
                indicators = stock.indicators

                # Golden Cross Detection
                if self.detect_golden_cross(data):
                    await self.create_opportunity(session, symbol, 'golden_cross', data)

                # RSI Divergence Detection
                if self.detect_rsi_divergence(indicators['rsi']):
                    await self.create_opportunity(session, symbol, 'rsi_divergence', data)

        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}")
        finally:
            session.close()

    def detect_golden_cross(self, data: pd.DataFrame) -> bool:
        ma50 = data['Close'].rolling(50).mean()
        ma200 = data['Close'].rolling(200).mean()
        return (ma50.iloc[-2] < ma200.iloc[-2]) and (ma50.iloc[-1] > ma200.iloc[-1])

    def detect_rsi_divergence(self, rsi_values: pd.Series) -> bool:
        # Implement sophisticated RSI divergence detection
        return (rsi_values.iloc[-1] < 30) and (rsi_values.iloc[-3] > rsi_values.iloc[-1])

    # ------------------ Report Generation System ------------------
    async def generate_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = self.get_session()
        try:
            # Generate comprehensive report
            report = "📊 تقرير السوق الشامل:\n\n"
            
            # Add market summary
            report += await self.generate_market_summary(session)
            
            # Add top opportunities
            report += "\n\n🔥 أفضل الفرص الآن:\n"
            report += await self.generate_opportunities_list(session)
            
            await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
            
        except Exception as e:
            logging.error(f"Report Error: {str(e)}")
            await update.message.reply_text("حدث خطأ في إنشاء التقرير")
        finally:
            session.close()

    # ------------------ Protection System ------------------
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = self.get_session()
        try:
            message = update.message
            group = session.query(GroupSettings).filter_by(chat_id=str(message.chat.id)).first()
            
            if group and group.settings['protection']['active']:
                if self.detect_invalid_content(message.text):
                    await self.apply_protection_measures(message, group)
                    
        except Exception as e:
            logging.error(f"Protection Error: {str(e)}")
        finally:
            session.close()

    def detect_invalid_content(self, text: str) -> bool:
        phone_pattern = r'\+\d{10,}'
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        return re.search(phone_pattern, text) or re.search(url_pattern, text)

    async def apply_protection_measures(self, message, group):
        try:
            await message.delete()
            
            if group.settings['protection']['punishment'] == 'mute':
                mute_duration = group.settings['protection']['mute_duration']
                until_date = datetime.now() + timedelta(hours=mute_duration)
                await self.app.bot.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    until_date=until_date,
                    permissions=ChatPermissions()
                )
                
        except Exception as e:
            logging.error(f"Protection Action Failed: {str(e)}")

    # ------------------ System Core ------------------
    async def run(self):
        await self.app.initialize()
        await self.app.start()
        self.scheduler.start()
        await self.app.updater.start_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get('PORT', 5000)),
            url_path=TOKEN,
            webhook_url=WEBHOOK_URL,
            secret_token='WEBHOOK_SECRET'
        )
        logging.info("Bot is running in production mode")
        await asyncio.Event().wait()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    bot = SaudiStockBot()
    asyncio.run(bot.run())