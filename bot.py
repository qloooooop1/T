import os
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram import Update
import re
import random

# إعدادات بيئية
TOKEN = os.getenv('TOKEN', 'YOUR_BOT_TOKEN_HERE')
MUTE_OR_BAN = os.getenv('MUTE_OR_BAN', 'mute').lower() == 'mute'
BANNED_WORDS = ['سبام', 'إعلان', 'جولة', 'واتساب', 'تليجرام', 'فيسبوك', 'تويتر']
MUTE_MESSAGE = "تم كتم @{user} بسبب: {reason} 😴🙊"
BAN_MESSAGE = "تم طرد @{user} بسبب: {reason} 🚀👋"

# قائمة بالسخريات الممكنة
SARCASTIC_REMARKS = [
    "آه، {user}، لقد كانت رسالة سبام مبهرة، شكرًا للتسلية! 🎉",
    "أوه، {user}، لقد فاتنا عرض رائع... في قمامة السبام! 🗑️",
    "رسالة ذكية جدًا، {user}، ستكون مفقودة بشدة... في سلة المهملات! 🧹",
    "أعتقد أننا قد نحتاج إلى درس في كيفية السبام الفعال، {user}! 📚",
    "لقد كانت هذه رسالة سبام مثالية، {user}، لو كان هناك جائزة للسبام! 🏆",
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

def start(update: Update, context: CallbackContext):
    update.message.reply_text('مرحبًا! أنا بوت لمكافحة السبام بطريقة ساخرة. 📢🙃')

def handle_message(update: Update, context: CallbackContext):
    message = update.message.text
    chat_id = update.message.chat_id
    user = update.message.from_user

    if is_spam(message):
        if update.message.chat.get_member(context.bot.id).can_restrict_members:
            reason = 'إرسال إعلانات أو أرقام جوالات أو روابط غير مسموح بها'
            if MUTE_OR_BAN:
                context.bot.restrict_chat_member(chat_id, user.id, can_send_messages=False)
                message_template = MUTE_MESSAGE
            else:
                context.bot.kick_chat_member(chat_id, user.id)
                message_template = BAN_MESSAGE

            # إرسال رسالة الكتم أو الطرد مع سخرية
            sarcastic_remark = random.choice(SARCASTIC_REMARKS).format(user=f"@{user.username}", reason=reason)
            context.bot.send_message(
                chat_id, 
                f"{message_template.format(user=user.username, reason=reason)}\n\n{sarcastic_remark}"
            )
            
            # سخرية إضافية
            context.bot.send_message(
                chat_id, 
                f"حسنًا، {user.username}، لنرى إذا كنت تستطيع إرسال شيء أكثر إبداعًا في المرة القادمة! 🎨"
            )
        else:
            context.bot.send_message(chat_id, "ليس لدي صلاحيات للكتم أو الطرد في هذه المجموعة! 🔒")
        
        try:
            context.bot.delete_message(chat_id, update.message.message_id)
        except:
            pass

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()