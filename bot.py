import os
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes
from telegram import Update, ChatPermissions
import re
import random
import asyncio

# إعدادات بيئية
TOKEN = '7812533121:AAFyxg2EeeB4WqFpHecR1gdGUdg9Or7Evlk'
MUTE_OR_BAN = os.getenv('MUTE_OR_BAN', 'mute').lower() == 'mute'
BANNED_WORDS = ['سبام', 'إعلان', 'جولة', 'واتساب', 'تليجرام', 'فيسبوك', 'تويتر']
MUTE_MESSAGE = "تم كتم @{user} بسبب: {reason} 😴🙊"
BAN_MESSAGE = "تم طرد @{user} بسبب: {reason} 🚀👋"

# قائمة بالسخريات الممكنة مع إضافة السخريات الجديدة
SARCASTIC_REMARKS = [
    "آه، {user}، لقد كانت رسالة سبام مبهرة، شكرًا للتسلية! 🎉",
    "أوه، {user}، لقد فاتنا عرض رائع... في قمامة السبام! 🗑️",
    "رسالة ذكية جدًا، {user}، ستكون مفقودة بشدة... في سلة المهملات! 🧹",
    "أعتقد أننا قد نحتاج إلى درس في كيفية السبام الفعال، {user}! 📚",
    "لقد كانت هذه رسالة سبام مثالية، {user}، لو كان هناك جائزة للسبام! 🏆",
    "{user}، هل تعتقد أن السبام هو مهنة جانبية؟ لقد فقدت وظيفتك الآن! 💼🤣",
    "تم اصتياد ابو سكيلف {user}، كما تصتاد الفقمه البطريق! 🎣",
    "انا تعلت ياترند {user}، انت وعدتني انك سوف تعود، انا انتظرك! ⏳",
    "تذكر عزيزي المتداول البسيط {user}، ان ترند افضل شخص في جنوب شرق افريقيا! 🌍",
    "ترند وبس والباقي خس، {user}! 💨",
    "تم اصتياد ابو سبام {user}! 🎉"
]

def is_spam(message):
    # تحقق من وجود كلمات ممنوعة
    if any(word in message.lower() for word in BANNED_WORDS):
        return True
    
    # كشف عن الأرقام السعودية والأجنبية
    # أرقام سعودية (05xxxxxxx, 05x-xxxxxxx, 05xxxxxxx, 5xxxxxxx, 5x-xxxxxxx)
    saudi_numbers = r'(05|5)\d{8}'
    # أرقام أجنبية (تشمل الرمز الدولي مع أو بدون +، مع أو بدون فاصلة أو نقطة أو وصلة)
    international_numbers = r'(\+\d{1,3}\s?[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}'
    
    if re.search(f'{saudi_numbers}|{international_numbers}', message):
        return True
    
    # كشف عن الروابط
    urls = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    if re.search(urls, message):
        return True
    
    # كشف عن قنوات ومجموعات تليجرام
    telegram_channels = r'@|t\.me'
    if re.search(telegram_channels, message):
        return True
    
    # كشف عن WhatsApp (بما في ذلك wa.me)
    whatsapp = r'wa\.me|\bwhatsapp\.com\b'
    if re.search(whatsapp, message):
        return True
    
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('مرحبًا! أنا بوت لمكافحة السبام بطريقة ساخرة. 📢🙃')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    chat_id = update.message.chat_id
    user = update.message.from_user

    if is_spam(message):
        if (await context.bot.get_chat_member(chat_id, context.bot.id)).can_delete_messages:
            reason = 'إرسال إعلانات أو أرقام جوالات أو روابط غير مسموح بها'
            if MUTE_OR_BAN:
                # استخدام ChatPermissions لتحديد صلاحيات المستخدم
                await context.bot.restrict_chat_member(chat_id, user.id, 
                                                       permissions=ChatPermissions(can_send_messages=False))
                message_template = MUTE_MESSAGE
                action = 'كتم'
            else:
                await context.bot.ban_chat_member(chat_id, user.id)
                message_template = BAN_MESSAGE
                action = 'طرد'

            # إرسال رسالة الكتم أو الطرد مع سخرية
            sarcastic_remark = random.choice(SARCASTIC_REMARKS).format(user=f"@{user.username}")
            bot_message = await context.bot.send_message(
                chat_id, 
                f"{message_template.format(user=user.username, reason=reason)}\n\n{sarcastic_remark}\n\nوهكذا، {user.username}، تم {action}ك بسبب {reason}، لكن لا يهم، لقد أضفت قليلاً من الضحك على حسابك هنا! 😂"
            )
            
            try:
                await context.bot.delete_message(chat_id, update.message.message_id)
            except Exception as e:
                print(f"فشل في حذف الرسالة: {e}")

            # حذف رسالة البوت بعد 2 دقيقة
            await asyncio.sleep(120)  # 2 دقيقة = 120 ثانية
            try:
                await context.bot.delete_message(chat_id, bot_message.message_id)
            except Exception as e:
                print(f"فشل في حذف رسالة البوت: {e}")
        else:
            await context.bot.send_message(chat_id, "ليس لدي صلاحيات لحذف الرسائل أو للكتم أو الطرد في هذه المجموعة! 🔒")

def main():
    application = Application.builder().token(TOKEN).build()
    
    from telegram.ext import filters
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(~filters.COMMAND & filters.TEXT, handle_message))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()