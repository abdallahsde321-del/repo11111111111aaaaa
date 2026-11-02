import os
import subprocess
import re
import time
import sys
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

TOKEN = os.environ.get('BOT_TOKEN')
bot = telebot.TeleBot(TOKEN)

# إنشاء ملف الكوكيز إن وجد
cookies_data = os.environ.get('COOKIES_CONTENT')
if cookies_data and len(cookies_data.strip()) > 0:
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(cookies_data)

user_data = {}
START_TIME = time.time()
MAX_LIFETIME = (5 * 3600) + (50 * 60) # 5 ساعات و50 دقيقة

def time_to_seconds(t_str):
    try:
        parts = list(map(int, t_str.split(':')))
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return 0

def get_main_menu_keyboard(lang='ar'):
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    if lang == 'ar':
        btn1 = KeyboardButton("🎬 قص فيديو جديد")
        btn2 = KeyboardButton("🌐 تغيير اللغة")
        btn3 = KeyboardButton("ℹ️ مساعدة")
    else:
        btn1 = KeyboardButton("🎬 Clip New Video")
        btn2 = KeyboardButton("🌐 Change Language")
        btn3 = KeyboardButton("ℹ️ Help")
    markup.add(btn1, btn2, btn3)
    return markup

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

def get_quality_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("⚡ 1080p (FHD سريح)", callback_data="quality_1080"),
        InlineKeyboardButton("🎬 720p (HD)", callback_data="quality_720")
    )
    markup.row(
        InlineKeyboardButton("📱 480p", callback_data="quality_480"),
        InlineKeyboardButton("⚡ 360p", callback_data="quality_360")
    )
    markup.row(
        InlineKeyboardButton("📉 144p", callback_data="quality_144"),
        InlineKeyboardButton("🎵 صوت فقط (MP3)", callback_data="quality_audio")
    )
    return markup

def get_confirm_keyboard(filename):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("نعم ✅", callback_data=f"confirm_delete_{filename}"),
        InlineKeyboardButton("لا ❌", callback_data=f"cancel_delete_{filename}")
    )
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id in user_data and 'lang' in user_data[chat_id]:
        lang = user_data[chat_id]['lang']
        msg = "مرحباً بك مجدداً! أرسل رابط فيديو أو بث اليوتيوب الآن لقص مقطع جديد:" if lang == 'ar' else "Welcome back! Send a YouTube link now to clip a new video:"
        bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))
        user_data[chat_id]['step'] = 'waiting_for_url'
    else:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
            InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")
        )
        bot.send_message(chat_id, "مرحباً بك! يرجى اختيار اللغة:\nWelcome! Please choose your language:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def set_language(call):
    chat_id = call.message.chat.id
    lang = call.data.split('_')[1]
    if chat_id not in user_data:
        user_data[chat_id] = {}
    user_data[chat_id].update({'lang': lang, 'h': 0, 'm': 0, 's': 0, 'step': 'waiting_for_url'})
    bot.answer_callback_query(call.id)
    
    msg = "✅ تم اختيار اللغة العربية.\nالرجاء إرسال رابط فيديو أو بث اليوتيوب الآن:" if lang == 'ar' else "✅ English selected.\nPlease send the YouTube video or live link now:"
    bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))

@bot.message_handler(func=lambda message: True)
def handle_steps(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    if chat_id not in user_data or 'lang' not in user_data[chat_id]:
        send_welcome(message)
        return

    lang = user_data[chat_id]['lang']

    if text in ["🎬 قص فيديو جديد", "🎬 Clip New Video"]:
        user_data[chat_id]['step'] = 'waiting_for_url'
        msg = "أرسل رابط فيديو أو بث اليوتيوب الآن:" if lang == 'ar' else "Send YouTube link now:"
        bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))
        return
    elif text in ["🌐 تغيير اللغة", "🌐 Change Language"]:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
            InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")
        )
        bot.send_message(chat_id, "اختر اللغة الجديدة / Choose language:", reply_markup=markup)
        return
    elif text in ["ℹ️ مساعدة", "ℹ️ Help"]:
        help_text = "💡 البوت يقوم بقص أي مقطع بالسرعة الفائقة وبأعلى جودة." if lang == 'ar' else "💡 Fast video clipping in high quality."
        bot.send_message(chat_id, help_text)
        return

    step = user_data[chat_id].get('step', 'waiting_for_url')
    
    if step == 'waiting_for_url' or re.search(r'https?://', text):
        url_match = re.search(r'(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+)', text)
        if url_match:
            user_data[chat_id]['url'] = url_match.group(1)
            user_data[chat_id]['step'] = 'setting_start_time'
            user_data[chat_id]['h'], user_data[chat_id]['m'], user_data[chat_id]['s'] = 0, 0, 0
            
            msg_text = "⏱️ حدد **وقت البداية** بالعداد:" if lang == 'ar' else "⏱️ Set **start time**:"
            bot.send_message(chat_id, msg_text, reply_markup=get_time_keyboard("start", 0, 0, 0), parse_mode="Markdown")
        else:
            err = "❌ رابط يوتيوب غير صحيح. حاول مرة أخرى:" if lang == 'ar' else "❌ Invalid YouTube URL. Try again:"
            bot.send_message(chat_id, err)

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_') or call.data.startswith('cancel_delete_'))
def handle_delete_confirmation(call):
    chat_id = call.message.chat.id
    lang = user_data.get(chat_id, {}).get('lang', 'ar')
    
    if call.data.startswith('confirm_delete_'):
        filename = call.data.replace('confirm_delete_', '')
        if os.path.exists(filename):
            try:
                os.remove(filename)
                msg = "🗑️ تم تأكيد الاستلام وحذف الملف بنجاح!" if lang == 'ar' else "🗑️ Confirmed! File deleted."
            except Exception as e:
                msg = f"❌ خطأ أثناء الحذف: {str(e)}"
        else:
            msg = "✅ الملف محذوف بالفعل." if lang == 'ar' else "✅ File already deleted."
        
        bot.answer_callback_query(call.id, "تم الحذف")
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=msg)
    
    elif call.data.startswith('cancel_delete_'):
        msg = "ℹ️ تم الإبقاء على الملف مؤقتاً." if lang == 'ar' else "ℹ️ File retained temporarily."
        bot.answer_callback_query(call.id, "تم الإلغاء")
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=msg)

@bot.callback_query_handler(func=lambda call: call.data.startswith('quality_'))
def handle_quality_selection(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data or 'start_time' not in user_data[chat_id] or 'end_time' not in user_data[chat_id]:
        bot.answer_callback_query(call.id, "انتهاء الجلسة، أرسل الرابط مجدداً")
        return

    quality_choice = call.data.replace('quality_', '')
    lang = user_data[chat_id].get('lang', 'ar')
    start_time = user_data[chat_id]['start_time']
    end_time = user_data[chat_id]['end_time']
    url = user_data[chat_id]['url']

    bot.answer_callback_query(call.id, f"جاري السحب الفوري: {quality_choice}")
    
    is_audio_only = (quality_choice == 'audio')
    
    wait_msg = f"⚡ [قص سريع] جاري سحب مقطع `{quality_choice}`..." if lang == 'ar' else f"⚡ Fast clipping `{quality_choice}`..."
    bot.send_message(chat_id, wait_msg, parse_mode="Markdown")

    try:
        ext = "mp3" if is_audio_only else "mp4"
        out = f"clip_{chat_id}.{ext}"
        if os.path.exists(out): os.remove(out)

        # أوامر السرعة الفائقة (منع إعادة الترميز والنسخ المباشر)
        command = [
            "yt-dlp",
            "--remote-components", "ejs:github",
            "--no-force-keyframes-at-cuts",  # السر في السرعة المباشرة بدون Re-encode
            "--download-sections", f"*{start_time}-{end_time}"
        ]

        if os.path.exists("cookies.txt") and os.path.getsize("cookies.txt") > 0:
            command.extend(["--cookies", "cookies.txt"])

        if is_audio_only:
            command.extend([
                "-x", "--audio-format", "mp3",
                "--audio-quality", "0",
                "-o", out,
                url
            ])
        else:
            height = quality_choice
            fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
            command.extend([
                "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", out,
                url
            ])

        subprocess.run(command, capture_output=True, text=True, check=True)

        if os.path.exists(out):
            caption = "✅ تفضل مقطعك جاهز بسرعة فائقة!" if lang == 'ar' else "✅ Fast clip ready!"
            with open(out, "rb") as f:
                if is_audio_only:
                    bot.send_audio(chat_id, f, caption=caption, reply_markup=get_main_menu_keyboard(lang))
                else:
                    bot.send_video(chat_id, f, caption=caption, reply_markup=get_main_menu_keyboard(lang))
            
            confirm_msg = "هل تم تأكيد واستلام الملف بنجاح لحذفه من الجهاز؟" if lang == 'ar' else "Confirm file receipt to delete from server?"
            bot.send_message(chat_id, confirm_msg, reply_markup=get_confirm_keyboard(out))
        else:
            bot.send_message(chat_id, "❌ حدث خطأ أثناء القص، تأكد من الوقت والرابط.")
    except subprocess.CalledProcessError as e:
        err_log = e.stderr[-350:] if e.stderr else str(e)
        bot.send_message(chat_id, f"❌ خطأ تقني أثناء القص:\n`{err_log}`", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ خطأ غير متوقع: {str(e)}")

    user_data[chat_id]['step'] = 'waiting_for_url'

@bot.callback_query_handler(func=lambda call: '_' in call.data and not call.data.startswith('lang_') and not 'delete_' in call.data and not call.data.startswith('quality_'))
def handle_counter(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data: return
    
    data = call.data
    prefix, action = data.split('_', 1)
    if 'h' not in user_data[chat_id]: user_data[chat_id].update({'h': 0, 'm': 0, 's': 0})
        
    h, m, s = user_data[chat_id]['h'], user_data[chat_id]['m'], user_data[chat_id]['s']
    lang = user_data[chat_id].get('lang', 'ar')
    
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
            
            msg_text = f"✅ وقت البداية: `{time_str}`\n\n⏱️ حدد **وقت النهاية** بالعداد:" if lang == 'ar' else f"✅ Start time: `{time_str}`\n\n⏱️ Set **end time**:"
            bot.send_message(chat_id, msg_text, reply_markup=get_time_keyboard("end", 0, 0, 0), parse_mode="Markdown")
        elif prefix == 'end':
            end_time = time_str
            start_time = user_data[chat_id]['start_time']
            
            start_sec = time_to_seconds(start_time)
            end_sec = time_to_seconds(end_time)
            
            if end_sec <= start_sec:
                err_time = "❌ خطأ: وقت النهاية يجب أن يكون أكبر من وقت البداية!" if lang == 'ar' else "❌ Error: End time must be greater than start time!"
                bot.send_message(chat_id, err_time)
                return
            
            user_data[chat_id]['end_time'] = end_time
            
            msg_qual = "🎬 اختر الجودة أو الصيغة المطلوبة للمقطع:" if lang == 'ar' else "🎬 Choose desired quality/format for the clip:"
            bot.send_message(chat_id, msg_qual, reply_markup=get_quality_keyboard())
        return

    try:
        bot.answer_callback_query(call.id, f"الوقت: {time_str}")
        msg_text = "⏱️ حدد وقت البداية:" if prefix == 'start' else "⏱️ حدد وقت النهاية:"
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=msg_text, reply_markup=get_time_keyboard(prefix, h, m, s))
    except Exception: pass

if __name__ == "__main__":
    print("🤖 Fast Worker Alpha running...")
    while True:
        if time.time() - START_TIME > MAX_LIFETIME:
            print("🔄 انتهت الجلسة، جاري إعادة التشغيل أوتوماتيكياً...")
            sys.exit(0)
        try:
            bot.polling(none_stop=True, interval=1, timeout=30, skip_pending=True)
        except Exception:
            time.sleep(3)
