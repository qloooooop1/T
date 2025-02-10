import os
import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Float, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
import re

# ------------------ Configuration ------------------
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
SAUDI_TIMEZONE = pytz.timezone('Asia/Riyadh')
STOCK_SYMBOLS = ['1211.SR', '2222.SR', '3030.SR', '4200.SR']
OWNER_ID = int(os.getenv('OWNER_ID', 0))
DATABASE_URL = os.getenv('DATABASE_URL').replace("postgres://", "postgresql://", 1)
ACTIVATED_GROUPS = os.getenv('ACTIVATED_GROUPS', '').split(',')

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
    settings = Column(JSON, default={
        'reports': {'hourly': True, 'daily': True, 'weekly': True},
        'strategies': {
            'golden': True, 'earthquake': True,
            'volcano': True, 'lightning': True
        },
        'security': {
            'max_queries': 5,
            'penalty': {'type': 'mute', 'duration': 24}
        }
    })
    opportunities = relationship('Opportunity', back_populates='group')
    users = relationship("User", back_populates="group")

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    daily_queries = Column(Integer, default=0)
    last_query = Column(DateTime)
    group_id = Column(Integer, ForeignKey('groups.id'))
    group = relationship("Group", back_populates="users")
    penalties = relationship("Penalty", back_populates="user")

class Penalty(Base):
    __tablename__ = 'penalties'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    penalty_type = Column(String)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    user = relationship("User", back_populates="penalties")

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
        self.sarcastic_messages = [
            "لا تزعجنا برقمك مرة أخرى!",
            "رقم جوال؟ هل تريد أن نبيع لك شيء ما؟",
            "من فضلك، احترم خصوصيتنا.",
            "لا نريد أي رسائل إعلانية هنا.",
            "هل تعتقد أننا نحتاج إلى رقمك؟",
            "شكراً لك، لكننا لا نحتاج إلى خدماتك."
        ]
        self.setup_handlers()

    def setup_handlers(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('settings', self.settings))
        self.app.add_handler(CommandHandler('approve', self.approve_group))
        self.app.add_handler(CallbackQueryHandler(self.handle_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def run(self):
        await self.app.initialize()
        self.scheduler.start()
        
        # Setup scheduled jobs
        self.scheduler.add_job(self.check_opportunities, 'interval', minutes=5)
        self.scheduler.add_job(self.send_daily_report, CronTrigger(hour=16, minute=0, timezone=SAUDI_TIMEZONE))
        self.scheduler.add_job(self.send_weekly_report, CronTrigger(day_of_week='thu', hour=16, minute=0, timezone=SAUDI_TIMEZONE))
        self.scheduler.add_job(self.reset_daily_queries, CronTrigger(hour=0, timezone=SAUDI_TIMEZONE))
        self.scheduler.add_job(self.check_penalties, 'interval', minutes=30)
        
        if WEBHOOK_URL and os.getenv('PORT'):
            await self.app.updater.start_webhook(
                listen="0.0.0.0",
                port=int(os.getenv('PORT')),
                url_path="",
                webhook_url=WEBHOOK_URL
            )
        else:
            await self.app.updater.start_polling()
        
        logging.info("Bot is running...")
        await asyncio.Event().wait()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if chat_id not in ACTIVATED_GROUPS:
            keyboard = [[InlineKeyboardButton("الدعم الفني 📞", url='t.me/support')]]
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

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        try:
            chat_id = str(update.effective_chat.id)
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group or not group.is_approved:
                await update.message.reply_text("⚠️ الإعدادات متاحة للمجموعات المفعلة فقط")
                return

            settings_text = (
                "⚙️ إعدادات المجموعة:\n\n"
                f"📊 الحد الأقصى للاستفسارات اليومية: {group.settings['security']['max_queries']}\n"
                f"🔨 نوع العقوبة: {group.settings['penalty']['type'].capitalize()}\n"
                f"⏳ مدة العقوبة: {group.settings['penalty']['duration']} ساعة\n"
                f"📈 الاستراتيجيات المفعلة:\n"
                f"- ذهبية: {'✅' if group.settings['strategies']['golden'] else '❌'}\n"
                f"- زلزالية: {'✅' if group.settings['strategies']['earthquake'] else '❌'}\n"
                f"- بركانية: {'✅' if group.settings['strategies']['volcano'] else '❌'}\n"
                f"- برقية: {'✅' if group.settings['strategies']['lightning'] else '❌'}"
            )
            
            buttons = [
                [InlineKeyboardButton("تعديل الإعدادات", callback_data='edit_settings')],
                [InlineKeyboardButton("رجوع ↩️", callback_data='main_menu')]
            ]
            await update.message.reply_text(
                settings_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            
        except Exception as e:
            logging.error(f"Settings Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == 'settings':
            await self.settings(update, context)
        elif query.data == 'edit_settings':
            await self.edit_settings(update)
        elif query.data == 'main_menu':
            await query.message.delete()
            await self.start(update, context)

    async def edit_settings(self, update: Update):
        session = Session()
        try:
            chat_id = str(update.callback_query.message.chat.id)
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            keyboard = [
                [InlineKeyboardButton("تعديل عدد الاستفسارات", callback_data='edit_queries')],
                [InlineKeyboardButton("تعديل نوع العقوبة", callback_data='edit_penalty')],
                [InlineKeyboardButton("تفعيل/تعطيل الاستراتيجيات", callback_data='toggle_strategies')],
                [InlineKeyboardButton("رجوع ↩️", callback_data='settings')]
            ]
            await update.callback_query.message.edit_text(
                "🛠 اختر الإعداد الذي تريد تعديله:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logging.error(f"Edit Settings Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        user_id = str(update.effective_user.id)
        
        if self.is_spam(message):
            await self.handle_spam(update)
            return
        
        if re.match(r'^\d{4}$', message):
            await self.handle_stock_analysis(user_id, message, update)

    def is_spam(self, message):
        patterns = [
            r'(?:\+?966|0)?\d{10}',
            r'whatsapp|telegram|t\.me|http|www|\.com|إعلان|اتصل بنا'
        ]
        return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)

    async def handle_spam(self, update: Update):
        await update.message.delete()
        session = Session()
        try:
            user_id = str(update.message.from_user.id)
            chat_id = str(update.message.chat.id)
            
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            user = session.query(User).filter_by(user_id=user_id, group_id=group.id).first()
            
            if not user:
                user = User(user_id=user_id, group_id=group.id)
                session.add(user)
                session.commit()

            penalty = Penalty(
                user_id=user.id,
                penalty_type=group.settings['penalty']['type'],
                start_time=datetime.now(SAUDI_TIMEZONE),
                end_time=datetime.now(SAUDI_TIMEZONE) + timedelta(hours=group.settings['penalty']['duration'])
            )
            session.add(penalty)
            session.commit()

            if penalty.penalty_type == 'mute':
                await update.message.chat.restrict_member(
                    user_id=user_id,
                    until_date=penalty.end_time,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            elif penalty.penalty_type == 'ban':
                await update.message.chat.ban_member(user_id=user_id)

            await update.message.reply_text(
                f"{update.message.from_user.mention_markdown()} {random.choice(self.sarcastic_messages)}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logging.error(f"Spam Handling Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def handle_stock_analysis(self, user_id, stock_code, update: Update):
        session = Session()
        try:
            today = datetime.now(SAUDI_TIMEZONE).date()
            group = session.query(Group).filter_by(chat_id=str(update.message.chat.id)).first()
            user = session.query(User).filter_by(user_id=user_id, group_id=group.id).first()
            
            if not user:
                user = User(user_id=user_id, group_id=group.id)
                session.add(user)
                session.commit()

            if user.daily_queries >= group.settings['security']['max_queries']:
                await update.message.reply_text(random.choice(self.sarcastic_messages))
                return

            # Perform analysis
            analysis = self.analyze_stock(stock_code)
            sent_message = await update.message.reply_text(analysis, parse_mode=ParseMode.MARKDOWN)
            
            # Update user
            user.daily_queries += 1
            user.last_query = datetime.now(SAUDI_TIMEZONE)
            session.commit()

            # Delete after 2 minutes
            await asyncio.sleep(120)
            await sent_message.delete()

        except Exception as e:
            logging.error(f"Stock Analysis Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    def analyze_stock(self, stock_code):
        try:
            stock = yf.Ticker(f"{stock_code}.SR")
            hist = stock.history(period="1mo")
            
            analysis = f"""
📊 *تحليل فني ومالي لسهم {stock_code}*

*المؤشرات الفنية:*
- السعر الحالي: {hist['Close'].iloc[-1]:.2f} ريال
- المتوسط المتحرك 50 يوم: {hist['Close'].rolling(50).mean().iloc[-1]:.2f}
- مؤشر RSI: {self.calculate_rsi(hist):.2f}
- مؤشر MACD: {self.calculate_macd(hist):.2f}

*التوصية:* {'🟢 شراء' if hist['Close'].iloc[-1] > hist['Close'].rolling(200).mean().iloc[-1] else '🔴 بيع'}
            """
            return analysis
        except Exception as e:
            logging.error(f"Analysis Error: {str(e)}")
            return "⚠️ حدث خطأ في تحليل السهم، يرجى المحاولة لاحقًا"

    def calculate_rsi(self, data, period=14):
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs)).iloc[-1]

    def calculate_macd(self, data):
        exp12 = data['Close'].ewm(span=12, adjust=False).mean()
        exp26 = data['Close'].ewm(span=26, adjust=False).mean()
        return (exp12 - exp26).iloc[-1]

    async def check_opportunities(self):
        session = Session()
        try:
            for symbol in STOCK_SYMBOLS:
                data = yf.download(symbol, period='3d', interval='1h')
                if data.empty:
                    continue

                if self.detect_golden_cross(data):
                    await self.create_opportunity(symbol, 'golden', data)
                if self.detect_earthquake(data):
                    await self.create_opportunity(symbol, 'earthquake', data)
                if self.detect_volcano(data):
                    await self.create_opportunity(symbol, 'volcano', data)
                if self.detect_lightning(data):
                    await self.create_opportunity(symbol, 'lightning', data)
        except Exception as e:
            logging.error(f"Opportunity Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    def detect_golden_cross(self, data):
        ema50 = data['Close'].ewm(span=50, adjust=False).mean()
        ema200 = data['Close'].ewm(span=200, adjust=False).mean()
        return ema50.iloc[-1] > ema200.iloc[-1] and ema50.iloc[-2] <= ema200.iloc[-2]

    def detect_earthquake(self, data):
        return (data['Close'].iloc[-1] > data['High'].rolling(14).max().iloc[-2] 
                and data['Volume'].iloc[-1] > data['Volume'].mean() * 2)

    def detect_volcano(self, data):
        high = data['High'].max()
        low = data['Low'].min()
        return data['Close'].iloc[-1] > low + 0.618 * (high - low)

    def detect_lightning(self, data):
        return (data['High'].iloc[-1] - data['Low'].iloc[-1] 
                > data['Close'].iloc[-2] * 0.05)

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
                stop_loss=stop_loss
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
        except Exception as e:
            logging.error(f"Alert Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    def get_strategy_name(self, strategy):
        names = {
            'golden': 'ذهبية 💰',
            'earthquake': 'زلزالية 🌋',
            'volcano': 'بركانية 🌋',
            'lightning': 'برقية ⚡'
        }
        return names.get(strategy, 'غير معروفة')

    async def reset_daily_queries(self):
        session = Session()
        try:
            session.query(User).update({User.daily_queries: 0})
            session.commit()
        except Exception as e:
            logging.error(f"Reset Queries Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def check_penalties(self):
        session = Session()
        try:
            penalties = session.query(Penalty).filter(Penalty.end_time <= datetime.now(SAUDI_TIMEZONE)).all()
            for penalty in penalties:
                if penalty.penalty_type == 'mute':
                    await self.app.bot.restrict_chat_member(
                        chat_id=penalty.user.group.chat_id,
                        user_id=penalty.user.user_id,
                        permissions=ChatPermissions.all_permissions()
                    )
                session.delete(penalty)
            session.commit()
        except Exception as e:
            logging.error(f"Penalty Check Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def send_daily_report(self):
        session = Session()
        try:
            report = "📊 التقرير اليومي:\n\n"
            top_gainers = await self.get_top_movers('1d')
            
            if top_gainers:
                report += "🏆 أعلى 5 شركات:\n" + "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[:5])])
                report += "\n\n🔻 أقل 5 شركات:\n" + "\n".join([f"{i+1}. {sym}: {chg}%" for i, (sym, chg) in enumerate(top_gainers[-5:])])
            else:
                report += "⚠️ لا توجد بيانات متاحة اليوم"
            
            groups = session.query(Group).filter(
                Group.is_approved == True,
                Group.settings['reports']['daily'].as_boolean()
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report
                )
        except Exception as e:
            logging.error(f"Daily Report Error: {str(e)}", exc_info=True)
        finally:
            session.close()

    async def send_weekly_report(self):
        session = Session()
        try:
            report = "📊 التقرير الأسبوعي:\n\n"
            opportunities = session.query(Opportunity).filter(
                Opportunity.created_at >= datetime.now(SAUDI_TIMEZONE) - timedelta(days=7)
            ).all()
            
            if opportunities:
                total_profit = sum(
                    (yf.Ticker(opp.symbol).history(period='1d')['Close'].iloc[-1] - opp.entry_price) 
                    / opp.entry_price * 100 
                    for opp in opportunities
                )
                report += f"📈 إجمالي الأرباح: {total_profit:.2f}%\n"
                report += "🔄 الفرص النشطة:\n" + "\n".join([f"- {opp.symbol} ({opp.strategy})" for opp in opportunities if opp.status == 'active'])
            else:
                report += "⚠️ لا توجد فرص هذا الأسبوع"
            
            groups = session.query(Group).filter(
                Group.is_approved == True,
                Group.settings['reports']['weekly'].as_boolean()
            ).all()
            
            for group in groups:
                await self.app.bot.send_message(
                    chat_id=group.chat_id,
                    text=report
                )
        except Exception as e:
            logging.error(f"Weekly Report Error: {str(e)}", exc_info=True)
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

    async def approve_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ صلاحية مطلوبة!")
            return

        try:
            _, chat_id = update.message.text.split()
            session = Session()
            group = session.query(Group).filter_by(chat_id=chat_id).first()
            
            if not group:
                group = Group(chat_id=chat_id)
                session.add(group)
            
            group.is_approved = True
            session.commit()
            
            if chat_id not in ACTIVATED_GROUPS:
                ACTIVATED_GROUPS.append(chat_id)
                os.environ['ACTIVATED_GROUPS'] = ','.join(ACTIVATED_GROUPS)
            
            await update.message.reply_text(f"✅ تم تفعيل المجموعة {chat_id}")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="🎉 تمت الموافقة على مجموعتك!"
            )
        except Exception as e:
            logging.error(f"Approval Error: {str(e)}", exc_info=True)
        finally:
            session.close()

if __name__ == '__main__':
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    bot = SaudiStockBot()
    asyncio.run(bot.run())