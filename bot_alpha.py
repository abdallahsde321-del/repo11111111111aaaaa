import os
import subprocess
import re
import time
import sys
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.environ.get('BOT_TOKEN')
bot = telebot.TeleBot(TOKEN)

cookies_data = os.environ.get('COOKIES_CONTENT')
if cookies_data:
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(cookies_data)

user_data = {}
START_TIME = time.time()
MAX_LIFETIME = 6 * 3600  # 6 ساعات

def get_time_keyboard(prefix, h, m, s):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_h_plus"),
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_m_plus"),
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_s_plus")
    )
    markup.row(
        InlineKeyboardButton(f"⏰ {h:02d} س", callback_data="ignore"),
        InlineKeyboardButton(f"⏱️ {m:02d} د", callback_data="ignore"),
        InlineKeyboardButton(f"⏱️ {s:02d} ث", callback_data="ignore")
    )
    markup.row(
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_h_minus"),
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_m_minus"),
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_s_minus")
    )
    markup.row(InlineKeyboardButton("✅ تم وتأكيد الوقت", callback_data=f"{prefix}_done"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
        InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")
    )
    bot.send_message(chat_id, "🤖 [Worker-Alpha (6H)] مرحباً بك! يرجى اختيار اللغة:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def set_language(call):
    chat_id = call.message.chat.id
    lang = call.data.split('_')[1]
    user_data[chat_id] = {'lang': lang, 'h': 0, 'm': 0, 's': 0}
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "✅ تم اختيار اللغة (Alpha). أرسل رابط يوتيوب الآن:")
    user_data[chat_id]['step'] = 'waiting_for_url'

@bot.message_handler(func=lambda message: True)
def handle_steps(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if chat_id not in user_data:
        bot.reply_to(message, "الرجاء البدء بالضغط على /start")
        return
    
    if user_data[chat_id].get('step') == 'waiting_for_url':
        url_match = re.search(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)\S+)', text)
        if url_match:
            user_data[chat_id]['url'] = url_match.group(1)
            user_data[chat_id]['step'] = 'setting_start_time'
            user_data[chat_id]['h'], user_data[chat_id]['m'], user_data[chat_id]['s'] = 0, 0, 0
            bot.send_message(chat_id, "⏱️ حدد وقت البداية بالعداد:", reply_markup=get_time_keyboard("start", 0, 0, 0), parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ رابط غير صحيح. حاول مجدداً:")

@bot.callback_query_handler(func=lambda call: '_' in call.data and not call.data.startswith('lang_'))
def handle_counter(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data: return
    
    data = call.data
    prefix, action = data.split('_', 1)
    if 'h' not in user_data[chat_id]: user_data[chat_id].update({'h': 0, 'm': 0, 's': 0})
        
    h, m, s = user_data[chat_id]['h'], user_data[chat_id]['m'], user_data[chat_id]['s']
    if action == 'h_plus': h = (h + 1) % 24
    elif action == 'h_minus': h = (h - 1) % 24
    elif action == 'm_plus': m = (m + 1) % 60
    elif action == 'm_minus': m = (m - 1) % 60
    elif action == 's_plus': s = (s + 1) % 60
    elif action == 's_minus': s = (s - 1) % 60
    
    user_data[chat_id].update({'h': h, 'm': m, 's': s})
    time_str = f"{h:02d}:{m:02d}:{s:02d}"
    
    if action == 'done':
        bot.answer_callback_query(call.id, f"تم اعتماد الوقت: {time_str}")
        if prefix == 'start':
            user_data[chat_id]['start_time'] = time_str
            user_data[chat_id]['step'] = 'setting_end_time'
            user_data[chat_id]['h'], user_data[chat_id]['m'], user_data[chat_id]['s'] = 0, 0, 0
            bot.send_message(chat_id, f"✅ وقت البداية: `{time_str}`\n⏱️ حدد وقت النهاية:", reply_markup=get_time_keyboard("end", 0, 0, 0), parse_mode="Markdown")
        elif prefix == 'end':
            end_time, start_time, url = time_str, user_data[chat_id]['start_time'], user_data[chat_id]['url']
            bot.send_message(chat_id, f"⏳ [Alpha] جاري قص الفيديو...")
            try:
                out = "alpha_clip.mp4"
                if os.path.exists(out): os.remove(out)
                subprocess.run([
                    "yt-dlp", "--remote-components", "ejs:github", "--cookies", "cookies.txt",
                    "--download-sections", f"*{start_time}-{end_time}", "-f", "bestvideo+bestaudio/best",
                    "--merge-output-format", "mp4", "-o", out, url
                ], check=True)
                if os.path.exists(out):
                    with open(out, "rb") as f: bot.send_video(chat_id, f, caption="✅ [Alpha] جاهز!")
                    os.remove(out)
            except Exception as e:
                bot.send_message(chat_id, f"❌ خطأ: {str(e)}")
            user_data[chat_id]['step'] = 'waiting_for_url'
        return

    try:
        bot.answer_callback_query(call.id, f"الوقت: {time_str}")
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text="⏱️ حدد الوقت بالعداد:", reply_markup=get_time_keyboard(prefix, h, m, s), parse_mode="Markdown")
    except Exception: pass

if __name__ == "__main__":
    print("🤖 Alpha Worker (6H) started...")
    while True:
        if time.time() - START_TIME > MAX_LIFETIME:
            print("🔄 انتهت الـ 6 ساعات، إيقاف للجلسة القادمة...")
            sys.exit(0)
        try: bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e: time.sleep(5)
