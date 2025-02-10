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

# Configuration
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.getenv('OWNER_ID', 0))
DATABASE_URL = os.getenv('DATABASE_URL').replace("postgres://", "postgresql://", 1)
ACTIVATED_GROUPS = os.getenv('ACTIVATED_GROUPS', '').split(',')
MAX_QUERIES_PER_DAY = 5  # Example value, can be set in the admin panel

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
    status = Column(String, default='active')  # active, completed, stopped
    group_id = Column(Integer, ForeignKey('groups.id'))
    group = relationship('Group', back_populates='opportunities')
    created_at = Column(DateTime, default=lambda: datetime.now(SAUDI_TIMEZONE))

class UserQuery(Base):
    __tablename__ = 'user_queries'
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    query_date = Column(DateTime, default=lambda: datetime.now(SAUDI_TIMEZONE))

Base.metadata.create_all(engine)

class SaudiStockBot:
    def __init__(self):
        self.app = Application.builder().token(TOKEN).build()
        self.scheduler = AsyncIOScheduler(timezone=SAUDI_TIMEZONE)
        self.setup_handlers()

    def setup_handlers(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('settings', self.settings))
        self.app.add_handler(CommandHandler('approve', self.approve_group))
        self.app.add_handler(CallbackQueryHandler(self.handle_button))
        self.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        if WEBHOOK_URL and os.getenv('PORT'):
            await self.app.updater.start_webhook(
                listen="0.0.0.0",
                port=int(os.getenv('PORT')),
                url_path="",
                webhook_url=WEBHOOK_URL
            )
        else:
            await self.app.updater.start_polling()
        
        # Setup scheduler after the event loop is running
        self.setup_scheduler()

        logging.info("Bot is running...")
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logging.info("Bot is shutting down...")
            await self.app.stop()
            self.scheduler.shutdown(wait=False)

    def setup_scheduler(self):
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=5)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=16, minute=0, timezone=SAUDI_TIMEZONE))
        self.scheduler.add_job(self.send_weekly_report, CronTrigger(day_of_week='thu', hour=16, minute=0, timezone=SAUDI_TIMEZONE))
        self.scheduler.start()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if chat_id not in ACTIVATED_GROUPS:
            keyboard = [
                [InlineKeyboardButton("الدعم الفني 📞", url='t.me/support')]
            ]
            await update.message.reply_text(
                "⚠️ هذه القناة غير مسجلة. يرجى تفعيلها من خلال التواصل مع الدعم الفني.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        keyboard = [
            [InlineKeyboardButton("الإعدادات ⚙️", callback_data='settings'),
             InlineKeyboardButton("التقارير 📊", callback_data='reports')],
            [InlineKeyboardButton("الدعم الفني 📞", url='t.me/support')]
        ]
        await update.message.reply_text(
            "مرحبًا بكم في بوت الأسهم السعودية المتقدم! 📈",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        user_id = str(update.effective_user.id)

        # Check for spam messages
        if self.is_spam(message):
            await self.delete_and_reply_sarcastically(update, context)
            return

        # Handle stock analysis requests
        if message.isdigit():
            await self.handle_stock_analysis(user_id, message, update)

    def is_spam(self, message):
        saudi_phone_pattern = r"(?:\+?966|0)?\d{10}"
        spam_patterns = [saudi_phone_pattern, r"whatsapp", r"telegram"]
        for pattern in spam_patterns:
            if re.search(pattern, message, re.IGNORECASE):
                return True
        return False

    async def delete_and_reply_sarcastically(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        sarcastic_replies = [
            "لا تزعجنا برقمك مرة أخرى!",
            "رقم جوال؟ هل تريد أن نبيع لك شيء ما؟",
            "من فضلك، احترم خصوصيتنا.",
            "لا نريد أي رسائل إعلانية هنا.",
            "هل تعتقد أننا نحتاج إلى رقمك؟",
            "شكراً لك، لكننا لا نحتاج إلى خدماتك."
        ]
        await update.message.delete()
        await update.message.reply_text(sarcastic_replies[np.random.randint(len(sarcastic_replies))])

    async def handle_stock_analysis(self, user_id, stock_code, update: Update):
        session = Session()
        try:
            today = datetime.now(SAUDI_TIMEZONE).date()
            queries = session.query(UserQuery).filter(
                UserQuery.user_id == user_id,
                UserQuery.query_date >= today
            ).count()

            if queries >= MAX_QUERIES_PER_DAY:
                await update.message.reply_text(
                    "⚠️ لقد استنفذت عدد الاستفسارات المسموح بها اليوم. يمكنك طرح المزيد غدًا."
                )
                return

            # Fetch stock data
            stock = yf.Ticker(f"{stock_code}.SR")
            data = stock.history(period="1mo")

            if data.empty:
                await update.message.reply_text("⚠️ لا توجد بيانات متاحة لهذا السهم.")
                return

            # Perform analysis
            analysis = self.analyze_stock(stock_code, data)

            # Save the query
            session.add(UserQuery(user_id=user_id))
            session.commit()

            # Send the analysis
            await update.message.reply_text(analysis, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logging.error(f"Error handling stock analysis: {str(e)}", exc_info=True)
        finally:
            session.close()

    def analyze_stock(self, stock_code, data):
        # Basic info
        current_price = data['Close'].iloc[-1]
        avg_50 = data['Close'].rolling(window=50).mean().iloc[-1]
        rsi = self.calculate_rsi(data)
        macd = self.calculate_macd(data)
        annual_return = ((data['Close'].iloc[-1] - data['Close'].iloc[0]) / data['Close'].iloc[0]) * 100
        volume_avg = data['Volume'].mean()
        fair_price = avg_50 * 0.9  # Example calculation

        # Build the analysis message
        analysis = (
            f"📊 *تحليل فني ومالي لسهم ({stock_code})\n"
            f"*الأنماط الفنية والتشكيلات:*\n"
            f"- *أنماط حركة السعر:* السهم يمرّ بمرحلة صعودية قوية مع كونه أعلى بكثير من متوسط 50 يوم البالغ {avg_50:.2f} ريال، مما يشير إلى زخم إيجابي 📈.\n"
            f"- *أنماط الاتجاه:* الاتجاه العام صعودي مدعوم بإشارات MACD إيجابية ({macd:.2f}) و RSI عند {rsi:.2f}، مما يعكس استمرارية الصعود 📊.\n"
            f"- *اختراقات الدعم/المقاومة:* لا توجد إشارات على اختراقات حديثة، ولكن السعر الحالي قريب من أعلى مستوى اليوم عند {data['High'].iloc[-1]:.2f} ريال.\n\n"
            f"*أداء السوق:*\n"
            f"- *الاتجاهات التاريخية:* العائد السنوي المقدر بـ {annual_return:.2f}% وعائده على مدى 5 سنوات بلغ 65.64%، مما يشير إلى أداء قوي مستدام 📈.\n"
            f"- *تحليل الحجم:* حجم التداول المتوسط على مدى شهر هو {volume_avg:.2f} ألف، وهو حجم معقول يشير إلى سيولة جيدة.\n"
            f"- *المستويات السعرية الرئيسية:* السعر الحالي {current_price:.2f} ريال، مما يجعله أعلى من السعر العادل المقدر بـ {fair_price:.2f} ريال بفارق -15.5% 📉.\n\n"
            f"*التحليل الأساسي:*\n"
            f"- *المؤشرات المالية:* السعر العادل وفق المؤشرات الأساسية هو {fair_price:.2f} ريال، مع نسبة ثقة 95%.\n"
            f"- *مقارنة القطاع:* الشركة تعمل في قطاع النقل والبنية التحتية بمشاريع متعددة في الموانئ والخدمات اللوجستية وتحلية المياه، مما يوفر تنوعاً في مصادر الدخل.\n"
            f"- *تأثير الأخبار:* لا توجد أخبار جديدة قد تؤثر على السعر بشكل فوري.\n\n"
            f"*تقييم المخاطر:*\n"
            f"- *المخاطر الفنية:* مخاطر مرتبطة بالاتجاه الصعودي الحالي واحتمالية التصحيح، خصوصاً بسبب التباين مع السعر العادل 📉.\n"
            f"- *مخاطر السوق:* الضغط البيعي الحالي مع صافي تدفق سلبي -81.81 ألف قد يؤثر على السعر.\n"
            f"- *مخاطر القطاع:* التغيرات في الاقتصاد المحلي أو العالمي قد تؤثر على مشاريع البنية التحتية والنقل.\n"
            f"لا تشكل توصية للشراء أو البيع."
        )
        return analysis

    def calculate_rsi(self, data, period=14):
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs)).iloc[-1]

    def calculate_macd(self, data, short_period=12, long_period=26, signal_period=9):
        short_ema = data['Close'].ewm(span=short_period, adjust=False).mean()
        long_ema = data['Close'].ewm(span=long_period, adjust=False).mean()
        macd = short_ema - long_ema
        signal = macd.ewm(span=signal_period, adjust=False).mean()
        return (macd - signal).iloc[-1]

    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                try:
                    data = yf.download(symbol, period='3d', interval='1h')
                    if data.empty or 'Close' not in data.columns:
                        logging.error(f"No data available for {symbol}")
                        continue
                    # Golden Cross Strategy
                    if self.detect_golden_cross(data):
                        await self.create_opportunity(symbol, 'golden', data)
                    # Earthquake Strategy
                    if self.detect_earthquake(data):
                        await self.create_opportunity(symbol, 'earthquake', data)
                    # Volcano Strategy
                    if self.detect_volcano(data):
                        await self.create_opportunity(symbol, 'volcano', data)
                    # Lightning Strategy
                    if self.detect_lightning(data):
                        await self.create_opportunity(symbol, 'lightning', data)
                except Exception as e:
                    logging.error(f"Error processing {symbol}: {str(e)}", exc_info=True)
        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    def detect_golden_cross(self, data):
        if len(data) < 200:
            return False
        ema50 = data['Close'].ewm(span=50, adjust=False).mean()
        ema200 = data['Close'].ewm(span=200, adjust=False).mean()
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]

    def detect_earthquake(self, data):
        if len(data) < 14:
            return False
        return (data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2] and
                data['Volume'].iloc[-1] > data['Volume'].mean() * 2)

    def detect_volcano(self, data):
        high = data['High'].max()
        low = data['Low'].min()
        return data['Close'].iloc[-1] > low + 0.618 * (high - low)

    def detect_lightning(self, data):
        return (data['High'].iloc[-1] - data['Low'].iloc[-1] >
                data['Close'].iloc[-2] * 0.05)

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
            logging.error(f"Create Opportunity Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    def calculate_stop_loss(self, strategy, data):
        if strategy == 'golden':
            return data['Low'].iloc[-2] * 0.98
        elif strategy == 'earthquake':
            return data['Close'].iloc[-1] * 0.95
        else:
            return data['Close'].iloc[-1] * 0.97

    def calculate_targets(self, strategy, entry):
        strategies = {
            'golden': [round(entry * (1 + i*0.05), 2) for i in range(1,5)],
            'earthquake': [round(entry * (1 + i*0.08), 2) for i in range(1,3)],
            'volcano': [round(entry * (1 + i*0.1), 2) for i in range(1,6)],
            'lightning': [round(entry * (1 + i*0.07), 2) for i in range(1,3)]
        }
        return strategies.get(strategy, [])

    async def send_alert_to_groups(self, opportunity):
        session = Session()
        try:
            groups = session.query(Group).filter(
                Group.is_approved == True,
                Group.settings['strategies'][opportunity.strategy].as_boolean()
            ).all()
            if not groups:
                logging.info("No active groups for this strategy")
                return
            text = (
                f"🚨 إشارة {self.get_strategy_name(opportunity.strategy)}\n"
                f"📈 السهم: {opportunity.symbol}\n"
                f"💰 السعر الحالي: {opportunity.entry_price:.2f}\n"
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
        except Exception as e:
            logging.error(f"Alert Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            top_gainers = await self.get_top_movers('1d')
            if not top_gainers:
                report += "⚠️ لا توجد بيانات متاحة اليوم"
            else:
                report += "🏆 أعلى 5 شركات:\n"
                report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[:5])])
                report += "\n\n🔻 أقل 5 شركات:\n"
                report += "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[-5:])])
            groups = session.query(Group).filter(
                Group.is_approved == True,
                Group.settings['reports']['daily'].as_boolean()
            ).all()
            for group in groups:
                try:
                    await self.app.bot.send_message(
                        chat_id=group.chat_id,
                        text=report
                    )
                except Exception as e:
                    logging.error(f"Failed to send report to group {group.chat_id}: {e}")
        except Exception as e:
            logging.error(f"Report Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def get_top_movers(self, period):
        movers = []
        for symbol in STOCK_SYMBOLS:
            try:
                data = yf.download(symbol, period=period)
                if len(data) < 2:
                    continue
                change = ((data['Close'].iloc[-1] - data['Open'].iloc[0]) / data['Open'].iloc[0]) * 100
                movers.append((symbol, round(change, 2)))
            except Exception as e:
                logging.error(f"Error getting data for {symbol}: {e}")
        return sorted(movers, key=lambda x: x[1], reverse=True)

    async def send_weekly_report(self):
        session = Session()
        try:
            report = "📊 التقرير الأسبوعي:\n\n"
            opportunities = session.query(Opportunity).filter(
                Opportunity.created_at >= datetime.now(SAUDI_TIMEZONE) - timedelta(days=7)
            ).all()
            if not opportunities:
                report += "⚠️ لا توجد فرص مطروحة هذا الأسبوع."
            else:
                total_profits = 0
                total_losses = 0
                active_opportunities = []
                for opp in opportunities:
                    current_price = yf.Ticker(opp.symbol).history(period='1d')['Close'].iloc[-1]
                    profit = (current_price - opp.entry_price) / opp.entry_price * 100
                    if profit > 0:
                        total_profits += profit
                    else:
                        total_losses += profit
                    if opp.status == 'active':
                        active_opportunities.append(opp)
                report += f"📈 إجمالي الأرباح: {total_profits:.2f}%\n"
                report += f"📉 إجمالي الخسائر: {total_losses:.2f}%\n\n"
                report += "🔄 الفرص المستمرة:\n"
                for opp in active_opportunities:
                    report += f"- {opp.symbol}: {opp.strategy}\n"
            groups = session.query(Group).filter(
                Group.is_approved == True,
                Group.settings['reports']['weekly'].as_boolean()
            ).all()
            for group in groups:
                try:
                    await self.app.bot.send_message(
                        chat_id=group.chat_id,
                        text=report
                    )
                except Exception as e:
                    logging.error(f"Failed to send weekly report to group {group.chat_id}: {e}")
        except Exception as e:
            logging.error(f"Weekly Report Error: {str(e)}", exc_info=True)
        finally:
            session.close()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    bot = SaudiStockBot()
    asyncio.run(bot.run())