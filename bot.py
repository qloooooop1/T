import os
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes
from telegram import Update
import re
import random

# إعدادات بيئية
TOKEN = os.getenv('TOKEN', '7812533121:AAFyxg2EeeB4WqFpHecR1gdGUdg9Or7Evlk')
MUTE_OR_BAN = os.getenv('MUTE_OR_BAN', 'mute').lower() == 'mute'
BANNED_WORDS = ['سبام', 'إعلان', 'جولة', 'واتساب', 'تليجرام', 'فيسبوك', 'تويتر']
MUTE_MESSAGE = "تم كتم @{user} بسبب: {reason} 😴🙊"
BAN_MESSAGE = "تم طرد @{user} بسبب: {reason} 🚀👋"

# قائمة بالسخريات الممكنة مع إضافة سخرية جديدة
SARCASTIC_REMARKS = [
    "آه، {user}، لقد كانت رسالة سبام مبهرة، شكرًا للتسلية! 🎉",
    "أوه، {user}، لقد فاتنا عرض رائع... في قمامة السبام! 🗑️",
    "رسالة ذكية جدًا، {user}، ستكون مفقودة بشدة... في سلة المهملات! 🧹",
    "أعتقد أننا قد نحتاج إلى درس في كيفية السبام الفعال، {user}! 📚",
    "لقد كانت هذه رسالة سبام مثالية، {user}، لو كان هناك جائزة للسبام! 🏆",
    "{user}، هل تعتقد أن السبام هو مهنة جانبية؟ لقد فقدت وظيفتك الآن! 💼🤣",
]

def is_spam(message):
    # تحقق من وجود كلمات ممنوعة أو أرقام جوالات أو روابط
    if any(word in message.lower() for word in BANNED_WORDS):
        return True
    if re.search(r'\b\d{3,}[-,.\s]?\d{3,}[-,.\s]?\d{4,}\b', message):  # أرقام جوالات
        return True
    if re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', message):  # روابط
        return True
    if re.search(r'@|t\.me', message):  # قنوات ومجموعات تليجرام
        return True
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('مرحبًا! أنا بوت لمكافحة السبام بطريقة ساخرة. 📢🙃')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    chat_id = update.message.chat_id
    user = update.message.from_user

    if is_spam(message):
        if (await context.bot.get_chat_member(chat_id, context.bot.id)).can_restrict_members:
            reason = 'إرسال إعلانات أو أرقام جوالات أو روابط غير مسموح بها'
            if MUTE_OR_BAN:
                await context.bot.restrict_chat_member(chat_id, user.id, can_send_messages=False)
                message_template = MUTE_MESSAGE
                action = 'كتم'
            else:
                await context.bot.ban_chat_member(chat_id, user.id)
                message_template = BAN_MESSAGE
                action = 'طرد'

            # إرسال رسالة الكتم أو الطرد مع سخرية
            sarcastic_remark = random.choice(SARCASTIC_REMARKS).format(user=f"@{user.username}", reason=reason)
            await context.bot.send_message(
                chat_id, 
                f"{message_template.format(user=user.username, reason=reason)}\n\n{sarcastic_remark}\n\nوهكذا، {user.username}، تم {action}ك بسبب {reason}، لكن لا يهم، لقد أضفت قليلاً من الضحك على حسابك هنا! 😂"
            )
        else:
            await context.bot.send_message(chat_id, "ليس لدي صلاحيات للكتم أو الطرد في هذه المجموعة! 🔒")
        
        try:
            await context.bot.delete_message(chat_id, update.message.message_id)
        except:
            pass

def main():
    application = Application.builder().token(TOKEN).build()
    
    # تصحيح الاستيراد لـ filters
    from telegram.ext import filters
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(~filters.COMMAND & filters.TEXT, handle_message))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()