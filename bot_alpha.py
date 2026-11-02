import os
import re
import sys
import glob
import time
import threading
import subprocess

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

TOKEN = os.environ.get('BOT_TOKEN')
bot = telebot.TeleBot(TOKEN)

try:
    import curl_cffi  # noqa: F401
    HAS_IMPERSONATE = True
except ImportError:
    HAS_IMPERSONATE = False

user_data = {}
START_TIME = time.time()
MAX_LIFETIME = (5 * 3600) + (50 * 60)  # 5 ساعات و50 دقيقة
MAX_UPLOAD_MB = 49                      # حد Bot API هو 50MB

# محاولات متتالية بدون كوكيز لو الأولى فشلت
YT_ATTEMPTS = [
    None,
    "youtube:player_client=tv,web_safari",
    "youtube:player_client=android_vr,web_embedded",
]


# ------------------------------------------------------------------
# أدوات مساعدة
# ------------------------------------------------------------------
def time_to_seconds(t_str):
    try:
        parts = list(map(int, t_str.split(':')))
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return 0


def get_main_menu_keyboard(lang='ar'):
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    if lang == 'ar':
        markup.add(KeyboardButton("🎬 قص فيديو جديد"),
                   KeyboardButton("🌐 تغيير اللغة"),
                   KeyboardButton("ℹ️ مساعدة"))
    else:
        markup.add(KeyboardButton("🎬 Clip New Video"),
                   KeyboardButton("🌐 Change Language"),
                   KeyboardButton("ℹ️ Help"))
    return markup


def get_time_keyboard(prefix, h, m, s):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_h_plus"),
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_m_plus"),
        InlineKeyboardButton("🔼", callback_data=f"{prefix}_s_plus"),
    )
    markup.row(
        InlineKeyboardButton(f"⏰ {h:02d} س", callback_data="ignore"),
        InlineKeyboardButton(f"⏱️ {m:02d} د", callback_data="ignore"),
        InlineKeyboardButton(f"⏱️ {s:02d} ث", callback_data="ignore"),
    )
    markup.row(
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_h_minus"),
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_m_minus"),
        InlineKeyboardButton("🔽", callback_data=f"{prefix}_s_minus"),
    )
    markup.row(InlineKeyboardButton("✅ تم وتأكيد الوقت", callback_data=f"{prefix}_done"))
    return markup


def get_quality_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("⚡ 1080p (أعلى جودة)", callback_data="quality_1080"),
        InlineKeyboardButton("🎬 720p (HD)", callback_data="quality_720"),
    )
    markup.row(
        InlineKeyboardButton("📱 480p", callback_data="quality_480"),
        InlineKeyboardButton("⚡ 360p", callback_data="quality_360"),
    )
    markup.row(
        InlineKeyboardButton("📉 144p", callback_data="quality_144"),
        InlineKeyboardButton("🎵 صوت فقط (MP3)", callback_data="quality_audio"),
    )
    return markup


def language_keyboard():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
        InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
    )
    return markup


# ------------------------------------------------------------------
# أوامر البداية واللغة
# ------------------------------------------------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id in user_data and 'lang' in user_data[chat_id]:
        lang = user_data[chat_id]['lang']
        msg = ("مرحباً بك مجدداً! أرسل رابط أي فيديو الآن لقص المقطع:"
               if lang == 'ar' else "Welcome back! Send any video link now to clip:")
        bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))
        user_data[chat_id]['step'] = 'waiting_for_url'
    else:
        bot.send_message(chat_id,
                         "مرحباً بك! يرجى اختيار اللغة:\nWelcome! Please choose your language:",
                         reply_markup=language_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def set_language(call):
    chat_id = call.message.chat.id
    lang = call.data.split('_')[1]
    user_data.setdefault(chat_id, {})
    user_data[chat_id].update({'lang': lang, 'h': 0, 'm': 0, 's': 0, 'step': 'waiting_for_url'})
    bot.answer_callback_query(call.id)

    msg = ("✅ تم اختيار اللغة العربية.\nالرجاء إرسال رابط الفيديو الآن (من أي موقع):"
           if lang == 'ar' else "✅ English selected.\nPlease send the video link now (from any site):")
    bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))


# ------------------------------------------------------------------
# الرسائل النصية
# ------------------------------------------------------------------
@bot.message_handler(func=lambda message: True)
def handle_steps(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if chat_id not in user_data or 'lang' not in user_data[chat_id]:
        send_welcome(message)
        return

    lang = user_data[chat_id]['lang']

    if text in ["🎬 قص فيديو جديد", "🎬 Clip New Video"]:
        user_data[chat_id]['step'] = 'waiting_for_url'
        msg = ("أرسل رابط أي فيديو الآن (يوتيوب، تيك توك، فيسبوك، إنستجرام...):"
               if lang == 'ar' else "Send any video link now:")
        bot.send_message(chat_id, msg, reply_markup=get_main_menu_keyboard(lang))
        return
    elif text in ["🌐 تغيير اللغة", "🌐 Change Language"]:
        bot.send_message(chat_id, "اختر اللغة الجديدة / Choose language:",
                         reply_markup=language_keyboard())
        return
    elif text in ["ℹ️ مساعدة", "ℹ️ Help"]:
        help_text = ("💡 البوت يدعم القص والتحميل السريع من كل المواقع بأعلى جودة."
                     if lang == 'ar' else "💡 Fast downloading and clipping from all video sites.")
        bot.send_message(chat_id, help_text)
        return

    step = user_data[chat_id].get('step', 'waiting_for_url')

    if step == 'waiting_for_url' or re.search(r'https?://', text):
        url_match = re.search(r'(https?://[^\s]+)', text)
        if url_match:
            user_data[chat_id]['url'] = url_match.group(1)
            user_data[chat_id]['step'] = 'setting_start_time'
            user_data[chat_id].update({'h': 0, 'm': 0, 's': 0})

            msg_text = ("⏱️ حدد **وقت البداية** بالعداد:"
                        if lang == 'ar' else "⏱️ Set **start time**:")
            bot.send_message(chat_id, msg_text,
                             reply_markup=get_time_keyboard("start", 0, 0, 0),
                             parse_mode="Markdown")
        else:
            err = "❌ رابط غير صحيح. حاول مرة أخرى:" if lang == 'ar' else "❌ Invalid URL. Try again:"
            bot.send_message(chat_id, err)


# ------------------------------------------------------------------
# التحميل (سريع، بدون كوكيز)
# ------------------------------------------------------------------
def build_command(url, base, quality, start, end, is_audio, attempt):
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-N", "16",                      # 16 اتصال متوازي للأجزاء
        "--http-chunk-size", "10M",      # يتجنب خنق السرعة في يوتيوب
        "--retries", "5", "--fragment-retries", "5",
        "--socket-timeout", "15",
        "--no-part", "--no-mtime",
        "-o", f"{base}.%(ext)s",
    ]
    if HAS_IMPERSONATE:
        cmd += ["--impersonate", "chrome"]
    if attempt:
        cmd += ["--extractor-args", attempt]

    clipping = (start != "00:00:00" or end != "00:00:00")
    if clipping:
        sec_end = end if end != "00:00:00" else "inf"
        cmd += ["--download-sections", f"*{start}-{sec_end}",
                "--downloader-args", "ffmpeg_i:-threads 4"]

    if is_audio:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        h = quality
        fmt = (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
               f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
               f"bv*[height<={h}]+ba/b[height<={h}]/b")
        cmd += ["-f", fmt, "--merge-output-format", "mp4"]

    cmd.append(url)
    return cmd


def cleanup(base):
    for f in glob.glob(f"{base}.*"):
        try:
            os.remove(f)
        except OSError:
            pass


def run_download(chat_id, quality_choice):
    d = user_data[chat_id]
    lang = d.get('lang', 'ar')
    start, end, url = d['start_time'], d['end_time'], d['url']
    is_audio = (quality_choice == 'audio')
    base = f"clip_{chat_id}_{int(time.time())}"
    last_err = ""
    files = []

    try:
        ok = False
        for attempt in YT_ATTEMPTS:
            cmd = build_command(url, base, quality_choice, start, end, is_audio, attempt)
            r = subprocess.run(cmd, capture_output=True, text=True)
            files = [f for f in glob.glob(f"{base}.*") if not f.endswith(".part")]
            if r.returncode == 0 and files:
                ok = True
                break
            last_err = (r.stderr or "")[-350:]
            cleanup(base)

        if not ok:
            bot.send_message(chat_id, f"❌ فشل التحميل:\n{last_err}")
            return

        out = files[0]
        size_mb = os.path.getsize(out) / 1024 / 1024
        if size_mb > MAX_UPLOAD_MB:
            bot.send_message(
                chat_id,
                f"⚠️ حجم الملف {size_mb:.0f}MB وتيليجرام (Bot API) يسمح بـ 50MB فقط. اختر جودة أقل أو قص أقصر."
                if lang == 'ar' else
                f"⚠️ File is {size_mb:.0f}MB, Telegram Bot API allows 50MB only. Choose lower quality or a shorter clip."
            )
            return

        caption = "✅ تفضل المقطع جاهز!" if lang == 'ar' else "✅ Clip ready!"
        with open(out, "rb") as f:
            if is_audio:
                bot.send_audio(chat_id, f, caption=caption, timeout=300)
            else:
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True, timeout=300)

    except Exception as e:
        bot.send_message(chat_id, f"❌ خطأ غير متوقع: {e}")
    finally:
        cleanup(base)  # حذف تلقائي دائماً
        user_data[chat_id]['step'] = 'waiting_for_url'


@bot.callback_query_handler(func=lambda call: call.data.startswith('quality_'))
def handle_quality_selection(call):
    chat_id = call.message.chat.id
    d = user_data.get(chat_id, {})
    if 'start_time' not in d or 'end_time' not in d or 'url' not in d:
        bot.answer_callback_query(call.id, "انتهاء الجلسة، أرسل الرابط مجدداً")
        return
    q = call.data.replace('quality_', '')
    bot.answer_callback_query(call.id, f"جاري التحميل: {q}")
    bot.send_message(chat_id, f"⚡ جاري المعالجة ({q})...")
    threading.Thread(target=run_download, args=(chat_id, q), daemon=True).start()


# ------------------------------------------------------------------
# عدادات الوقت
# ------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data == 'ignore')
def handle_ignore(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith(('start_', 'end_')))
def handle_counter(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data:
        bot.answer_callback_query(call.id, "انتهاء الجلسة، أرسل الرابط مجدداً")
        return

    prefix, action = call.data.split('_', 1)
    d = user_data[chat_id]
    d.setdefault('h', 0); d.setdefault('m', 0); d.setdefault('s', 0)

    h, m, s = d['h'], d['m'], d['s']
    lang = d.get('lang', 'ar')

    if action == 'h_plus': h = (h + 1) % 24
    elif action == 'h_minus': h = (h - 1) % 24
    elif action == 'm_plus': m = (m + 1) % 60
    elif action == 'm_minus': m = (m - 1) % 60
    elif action == 's_plus': s = (s + 1) % 60
    elif action == 's_minus': s = (s - 1) % 60

    d.update({'h': h, 'm': m, 's': s})
    time_str = f"{h:02d}:{m:02d}:{s:02d}"

    if action == 'done':
        bot.answer_callback_query(call.id, f"تم اعتماد الوقت: {time_str}")
        if prefix == 'start':
            d['start_time'] = time_str
            d['step'] = 'setting_end_time'
            d.update({'h': 0, 'm': 0, 's': 0})

            msg_text = (f"✅ وقت البداية: `{time_str}`\n\n⏱️ حدد **وقت النهاية** بالعداد:\n"
                        f"(اتركه 00:00:00 لتحميل حتى نهاية الفيديو)"
                        if lang == 'ar' else
                        f"✅ Start time: `{time_str}`\n\n⏱️ Set **end time**:\n"
                        f"(leave 00:00:00 to download until the end)")
            bot.send_message(chat_id, msg_text,
                             reply_markup=get_time_keyboard("end", 0, 0, 0),
                             parse_mode="Markdown")
        elif prefix == 'end':
            end_time = time_str
            start_time = d.get('start_time', "00:00:00")
            start_sec = time_to_seconds(start_time)
            end_sec = time_to_seconds(end_time)

            # وقت النهاية صفر = حتى نهاية الفيديو
            if end_sec != 0 and end_sec <= start_sec:
                err_time = ("❌ خطأ: وقت النهاية يجب أن يكون أكبر من وقت البداية!"
                            if lang == 'ar' else "❌ Error: End time must be greater than start time!")
                bot.send_message(chat_id, err_time)
                return

            d['end_time'] = end_time
            msg_qual = ("🎬 اختر الجودة أو الصيغة المطلوبة للمقطع:"
                        if lang == 'ar' else "🎬 Choose desired quality/format:")
            bot.send_message(chat_id, msg_qual, reply_markup=get_quality_keyboard())
        return

    try:
        bot.answer_callback_query(call.id, f"الوقت: {time_str}")
        msg_text = "⏱️ حدد وقت البداية:" if prefix == 'start' else "⏱️ حدد وقت النهاية:"
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id,
                              text=msg_text,
                              reply_markup=get_time_keyboard(prefix, h, m, s))
    except Exception:
        pass


# ------------------------------------------------------------------
# التشغيل
# ------------------------------------------------------------------
def stop_when_expired():
    time.sleep(MAX_LIFETIME)
    print("🔄 انتهت الجلسة، جاري الإيقاف لبدء تشغيل جديد...")
    bot.stop_polling()


if __name__ == "__main__":
    print("🤖 Universal Fast Worker Alpha running...")
    threading.Thread(target=stop_when_expired, daemon=True).start()
    while True:
        if time.time() - START_TIME > MAX_LIFETIME:
            sys.exit(0)
        try:
            bot.polling(none_stop=True, interval=1, timeout=30, skip_pending=True)
        except Exception:
            time.sleep(3)
