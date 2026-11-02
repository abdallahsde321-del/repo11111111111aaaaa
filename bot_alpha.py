#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram video downloader / clipper (yt-dlp), built to run on GitHub Actions.

- No cookies and no login: nothing here reads or writes cookie files.
- yt-dlp supports many sites, not literally all of them.
- YouTube-specific fallbacks are only used for YouTube links.
- Downloads run in a worker pool, never inside Telegram's polling threads.
"""

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ---------------------------- Configuration ----------------------------


def _env_int(name, default, low, high):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to GitHub Secrets.")

# GitHub-hosted jobs are limited to 6 hours. Setup takes a few minutes, the
# worker stops polling after MAX_LIFETIME, then waits up to DRAIN_GRACE for
# running downloads before killing them.
MAX_LIFETIME_SECONDS = 5 * 3600 + 30 * 60
DRAIN_GRACE_SECONDS = 8 * 60
ATTEMPT_TIMEOUT_SECONDS = 30 * 60

MAX_UPLOAD_MB = 49                 # Bot API limit is 50 MB
MAX_DOWNLOAD_WORKERS = 3           # concurrent downloads (disk + CPU safe)
MIN_FREE_BYTES = 1_500_000_000     # refuse new jobs below ~1.5 GB free disk
FRAGMENTS = _env_int("CONCURRENT_FRAGMENTS", 16, 1, 32)
PRECISE_CUTS = os.environ.get("PRECISE_CUTS", "0") == "1"  # slower, frame-exact cuts

MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".mp3", ".m4a",
    ".aac", ".wav", ".flac", ".opus", ".ogg",
}
VIDEO_SEND_EXTENSIONS = {".mp4", ".m4v", ".mov"}

# Public Piped API instances. Piped returns proxy URLs for media streams, so
# GitHub Actions downloads from the instance proxy rather than asking YouTube
# directly from the GitHub runner IP. Instances can disappear or rate-limit;
# the bot rotates through the list automatically.
PIPED_API_SERVERS = (
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi-libre.kavin.rocks",
    "https://piped-api.privacy.com.de",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
    "https://pipedapi.drgns.space",
    "https://pipedapi.owo.si",
    "https://pipedapi.ducks.party",
    "https://piped-api.codespace.cz",
    "https://pipedapi.reallyaweso.me",
    "https://api.piped.private.coffee",
    "https://pipedapi.darkness.services",
    "https://pipedapi.orangenet.cc",
)
PIPED_API_TIMEOUT_SECONDS = 10
PIPED_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

QUALITY_HEIGHTS = {"1080", "720", "480", "360", "144"}

STOP_EVENT = threading.Event()      # hard stop: kill everything
DRAINING = threading.Event()        # soft stop: no new work, let running jobs finish
STATE_LOCK = threading.RLock()
PROCESS_LOCK = threading.RLock()
IS_POSIX = os.name == "posix"

user_data = {}
active_chats = set()
active_processes = set()

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
executor = ThreadPoolExecutor(
    max_workers=MAX_DOWNLOAD_WORKERS,
    thread_name_prefix="video-download",
)


def _detect_impersonation():
    """True only if this yt-dlp can really impersonate Chrome (needs curl_cffi)."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--ignore-config", "--list-impersonate-targets"],
            capture_output=True, text=True, timeout=30,
        )
        for line in (result.stdout or "").splitlines():
            low = line.strip().lower()
            if low.startswith("chrome") and "unavailable" not in low:
                return True
    except Exception:
        pass
    return False


HAS_IMPERSONATION = _detect_impersonation()


# ---------------------------- Telegram UI ----------------------------

def main_menu(lang="ar"):
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    if lang == "ar":
        markup.add(
            KeyboardButton("🎬 قص فيديو جديد"),
            KeyboardButton("🌐 تغيير اللغة"),
            KeyboardButton("ℹ️ مساعدة"),
        )
    else:
        markup.add(
            KeyboardButton("🎬 Clip New Video"),
            KeyboardButton("🌐 Change Language"),
            KeyboardButton("ℹ️ Help"),
        )
    return markup


def language_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
        InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
    )
    return markup


def time_keyboard(prefix, h, m, s):
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


def quality_keyboard():
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


def safe_answer(call_id, text=None):
    try:
        bot.answer_callback_query(call_id, text)
    except Exception:
        pass


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        print(f"[send_message] chat={chat_id} failed: {exc}")
        return None


# ---------------------------- Validation / helpers ----------------------------

def seconds_to_time(value):
    value = max(0, int(value))
    h, rem = divmod(value, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def time_to_seconds(value):
    """'HH:MM:SS' -> seconds, or None if malformed."""
    try:
        parts = [int(x) for x in value.split(":")]
    except (AttributeError, ValueError):
        return None
    if len(parts) != 3 or parts[0] < 0 or not (0 <= parts[1] < 60) or not (0 <= parts[2] < 60):
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_time_text(text):
    """Typed time: 'SS', 'MM:SS' or 'HH:MM:SS' -> seconds, or None."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d{1,5}(:\d{1,2}){0,2}", text):
        return None
    parts = [int(x) for x in text.split(":")]
    if len(parts) == 1:
        total = parts[0]
    elif len(parts) == 2:
        if parts[1] >= 60:
            return None
        total = parts[0] * 60 + parts[1]
    else:
        if parts[1] >= 60 or parts[2] >= 60:
            return None
        total = parts[0] * 3600 + parts[1] * 60 + parts[2]
    return total if total < 100 * 3600 else None


def _is_global_address(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return True


def extract_and_validate_url(text):
    """Return a safe http(s) URL from text, or None. Blocks local/private targets."""
    match = re.search(r"https?://[^\s<>]+", text or "", flags=re.IGNORECASE)
    if not match:
        return None

    url = match.group(0).rstrip(".,;:!?)]}>'\"")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        if len(url) > 4096:
            return None

        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return None

        try:
            ipaddress.ip_address(host)
            if not _is_global_address(host):
                return None
        except ValueError:
            # Hostname: block it if it resolves to a private/loopback/link-local address.
            try:
                infos = socket.getaddrinfo(host, None)
                for info in infos:
                    if not _is_global_address(info[4][0]):
                        return None
            except OSError:
                pass  # can't resolve here; let yt-dlp report a clear error
        return url
    except Exception:
        return None


def is_youtube_url(url):
    host = (urlsplit(url).hostname or "").lower()
    return (
        host == "youtu.be"
        or host == "youtube.com" or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")
    )


def extract_youtube_video_id(url):
    """Extract an 11-character video ID from common YouTube URL forms."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
        else:
            query = parse_qs(parsed.query)
            if query.get("v"):
                video_id = query["v"][0]
            else:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                    video_id = parts[1]
        return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or "") else None
    except Exception:
        return None


def _valid_piped_media_url(value):
    """Accept only absolute HTTP(S) stream URLs returned by a Piped API."""
    try:
        parsed = urlsplit(str(value or ""))
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def _piped_iter_streams(video_id):
    """Yield usable stream metadata from public Piped instances one by one."""
    for base_url in PIPED_API_SERVERS:
        if STOP_EVENT.is_set():
            return
        endpoint = f"{base_url}/streams/{video_id}"
        try:
            request = Request(
                endpoint,
                headers={
                    "User-Agent": PIPED_USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=PIPED_API_TIMEOUT_SECONDS) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                raw = response.read(6 * 1024 * 1024 + 1)
            if len(raw) > 6 * 1024 * 1024:
                raise RuntimeError("API response too large")
            data = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(data, dict) or data.get("error"):
                reason = str(data.get("error", "invalid API response")) if isinstance(data, dict) else "invalid JSON"
                print(f"[piped] {urlsplit(base_url).netloc}: {reason[:180]}")
                continue

            videos = [
                item for item in (data.get("videoStreams") or [])
                if isinstance(item, dict)
                and _valid_piped_media_url(item.get("url"))
                and "googlevideo.com" not in (urlsplit(item.get("url", "")).hostname or "").lower()
            ]
            audios = [
                item for item in (data.get("audioStreams") or [])
                if isinstance(item, dict)
                and _valid_piped_media_url(item.get("url"))
                and "googlevideo.com" not in (urlsplit(item.get("url", "")).hostname or "").lower()
            ]
            if videos or audios:
                print(
                    f"[piped] API ready: {urlsplit(base_url).netloc} | "
                    f"video streams={len(videos)} audio streams={len(audios)}"
                )
                yield {"base_url": base_url, "data": data, "videos": videos, "audios": audios}
            else:
                print(f"[piped] {urlsplit(base_url).netloc}: no proxied streams (direct YouTube links are skipped)")
        except Exception as exc:
            print(f"[piped] {urlsplit(base_url).netloc}: API failed ({str(exc)[:180]})")


def _stream_height(stream):
    try:
        return int(stream.get("height") or 0)
    except (TypeError, ValueError, AttributeError):
        match = re.search(r"(\d{3,4})p", str((stream or {}).get("quality", "")))
        return int(match.group(1)) if match else 0


def _choose_piped_stream(streams, target_height=None, prefer_combined=False):
    candidates = list(streams or [])
    if prefer_combined:
        combined = [item for item in candidates if not item.get("videoOnly", False)]
        if not combined:
            return None
        candidates = combined
    if not candidates:
        return None

    if target_height:
        under = [item for item in candidates if 0 < _stream_height(item) <= target_height]
        if under:
            candidates = under

    def score(item):
        mime = str(item.get("mimeType", "")).lower()
        fmt = str(item.get("format", "")).lower()
        is_mp4 = int("mp4" in mime or "mpeg_4" in fmt)
        return (is_mp4, _stream_height(item), int(item.get("bitrate") or 0))

    return max(candidates, key=score)


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _piped_ffmpeg_command(video_stream, audio_stream, output_path, quality, start_time, end_time):
    """Build an FFmpeg command for a Piped-proxied muxed or separate stream."""
    start_seconds = time_to_seconds(start_time) or 0
    end_seconds = time_to_seconds(end_time) or 0
    duration = max(0, end_seconds - start_seconds) if end_seconds else 0
    precise = PRECISE_CUTS

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-user_agent", PIPED_USER_AGENT,
    ]

    stream_urls = []
    if quality == "audio":
        stream_urls = [audio_stream["url"]]
    elif audio_stream is not None:
        stream_urls = [video_stream["url"], audio_stream["url"]]
    else:
        stream_urls = [video_stream["url"]]

    # Fast seeking uses HTTP range requests before opening each input.
    # Precise seeking is applied after input decoding and re-encodes the clip.
    if start_seconds and not precise:
        seek = _format_duration(start_seconds)
        for stream_url in stream_urls:
            command += ["-ss", seek, "-i", stream_url]
    else:
        for stream_url in stream_urls:
            command += ["-i", stream_url]

    if precise and start_seconds:
        command += ["-ss", _format_duration(start_seconds)]
    if duration:
        command += ["-t", _format_duration(duration)]

    if quality == "audio":
        command += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "0"]
    elif audio_stream is not None:
        command += ["-map", "0:v:0", "-map", "1:a:0"]
        if precise:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", "0:v:0", "-map", "0:a:0?"]
        if precise:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c", "copy"]

    if output_path.lower().endswith(".mp4"):
        command += ["-movflags", "+faststart"]
    command.append(output_path)
    return command


def download_youtube_via_piped(url, work_dir, quality, start_time, end_time):
    """Fallback for YouTube IP blocks: use public Piped API/proxy instances."""
    video_id = extract_youtube_video_id(url)
    if not video_id:
        print("[piped] Could not extract a YouTube video ID from this URL.")
        return None

    print(f"[piped] yt-dlp clients were blocked; trying public Piped proxies for {video_id}.")
    for result in _piped_iter_streams(video_id):
        if STOP_EVENT.is_set():
            return None

        videos = result["videos"]
        audios = result["audios"]
        video_stream = None
        audio_stream = None

        if quality == "audio":
            audio_stream = max(audios, key=lambda item: int(item.get("bitrate") or 0), default=None)
            if not audio_stream:
                print(f"[piped] {urlsplit(result['base_url']).netloc}: no audio stream")
                continue
            extension = ".mp3"
        else:
            try:
                target_height = int(quality)
            except (TypeError, ValueError):
                target_height = 720
            video_stream = _choose_piped_stream(videos, target_height, prefer_combined=True)
            if video_stream is None:
                # Some instances expose only separate DASH video/audio streams.
                video_stream = _choose_piped_stream(
                    [item for item in videos if item.get("videoOnly", False)],
                    target_height,
                )
                audio_stream = max(audios, key=lambda item: int(item.get("bitrate") or 0), default=None)
                if video_stream is None or audio_stream is None:
                    print(f"[piped] {urlsplit(result['base_url']).netloc}: no usable A/V pair")
                    continue

            mime = str(video_stream.get("mimeType", "")).lower()
            fmt = str(video_stream.get("format", "")).lower()
            codec = str(video_stream.get("codec", "")).lower()
            if audio_stream is not None:
                extension = ".mp4" if "avc1" in codec or "mp4" in mime else ".mkv"
            elif "mp4" in mime or "mpeg_4" in fmt:
                extension = ".mp4"
            else:
                extension = ".mkv"

        output_path = os.path.join(work_dir, "piped_media" + extension)
        command = _piped_ffmpeg_command(
            video_stream, audio_stream, output_path, quality, start_time, end_time
        )
        chosen_height = _stream_height(video_stream) if video_stream else "audio"
        instance = urlsplit(result["base_url"]).netloc
        print(
            f"[piped] trying media proxy | instance={instance} "
            f"| selected_height={chosen_height} | precise_cuts={PRECISE_CUTS}"
        )
        code, _out, error = execute_command(command)
        if code == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            print(f"[piped] download succeeded through {instance}")
            return output_path

        print(f"[piped] proxy download failed via {instance} (exit={code}): {(error or 'no output')[-1200:]}")
        try:
            os.remove(output_path)
        except OSError:
            pass

    print("[piped] All public Piped APIs/proxies failed or were unavailable.")
    return None


def cleanup_directory(path):
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def clear_directory(path):
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass
    except OSError:
        pass


def find_media_file(directory):
    """Largest finished media file in directory (ignores partial/temporary files)."""
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if name.endswith((".part", ".ytdl", ".temp", ".json")) or ".part-" in name:
            continue
        if os.path.splitext(name)[1].lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            if os.path.getsize(path) > 0:
                candidates.append(path)
        except OSError:
            pass
    return max(candidates, key=os.path.getsize) if candidates else None


# Errors that retrying with another mode cannot fix.
PERMANENT_ERRORS = (
    "unsupported url",
    "is not a valid url",
    "private video",
    "this video is private",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "members-only",
    "join this channel",
)


def is_permanent_error(stderr):
    low = (stderr or "").lower()
    return any(marker in low for marker in PERMANENT_ERRORS)


def summarize_error(stderr, url, lang):
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    text = "\n".join(errors[-3:]) if errors else "\n".join(lines[-4:])
    text = (text or "yt-dlp did not return a clear reason.")[-1200:]

    hint = ""
    low = (stderr or "").lower()
    if is_youtube_url(url) and ("confirm you" in low and "bot" in low or "sign in" in low):
        hint = (
            "\n\n💡 يوتيوب يحجب عناوين السيرفرات أحياناً. جرّب رابطاً آخر أو أعد المحاولة لاحقاً."
            if lang == "ar"
            else "\n\n💡 YouTube sometimes blocks datacenter IPs. Try another link or retry later."
        )
    elif "unsupported url" in low:
        hint = (
            "\n\n💡 هذا الموقع/الرابط غير مدعوم من yt-dlp."
            if lang == "ar"
            else "\n\n💡 This site/link is not supported by yt-dlp."
        )
    head = "❌ لم ينجح التحميل بدون تسجيل دخول.\n\n" if lang == "ar" else "❌ Download failed (no login used).\n\n"
    return (head + text + hint)[-3500:]


# ---------------------------- yt-dlp engine ----------------------------

def get_attempts(url):
    """Return ordered strategies; YouTube-specific clients stay YouTube-only."""
    attempts = [
        {"label": "standard", "args": [], "fragments": FRAGMENTS}
    ]

    if not is_youtube_url(url):
        if HAS_IMPERSONATION:
            attempts.append({
                "label": "impersonate-chrome",
                "args": ["--impersonate", "chrome"],
                "fragments": min(FRAGMENTS, 8),
            })
        return attempts

    youtube_fragments = min(FRAGMENTS, 8)

    # Keep each YouTube client in its own attempt. A missing-format result
    # from one client must not prevent trying the next client.
    attempts.extend([
        {
            "label": "youtube-mweb-po-token",
            "args": [
                "--extractor-args",
                "youtube:player_client=mweb",
            ],
            "fragments": youtube_fragments,
        },
        {
            "label": "youtube-web-safari",
            "args": [
                "--extractor-args",
                "youtube:player_client=web_safari",
            ],
            "fragments": youtube_fragments,
        },
        {
            "label": "youtube-web-embedded",
            "args": [
                "--extractor-args",
                "youtube:player_client=web_embedded",
            ],
            "fragments": youtube_fragments,
        },
        {
            "label": "youtube-android-vr",
            "args": [
                "--extractor-args",
                "youtube:player_client=android_vr",
            ],
            "fragments": youtube_fragments,
        },
        {
            "label": "youtube-tv",
            "args": [
                "--extractor-args",
                "youtube:player_client=tv",
            ],
            "fragments": youtube_fragments,
        },
    ])
    return attempts



YOUTUBE_DIAGNOSTIC_MARKERS = (
    "po token providers",
    "generating a gvs po token",
    "po token",
    "sign in to confirm",
    "not a bot",
    "no video formats",
    "unsupported url",
    "error:",
)


def check_po_token_provider():
    """Check whether the local bgutil provider HTTP server responds."""
    ping_url = "http://127.0.0.1:4416/ping"
    try:
        with urlopen(ping_url, timeout=3) as response:
            body = response.read(256).decode("utf-8", errors="replace").strip()
            print(
                f"[po-token] provider reachable: HTTP {response.status}"
                + (f" | {body[:200]}" if body else "")
            )
            return 200 <= response.status < 300
    except Exception as exc:
        print(
            f"[po-token] provider unavailable at {ping_url}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def log_attempt_diagnostics(attempt, return_code, stderr):
    """Print useful yt-dlp/PO-token lines without dumping the entire verbose log."""
    print(
        f"[download] attempt result: mode={attempt['label']} "
        f"exit_code={return_code}"
    )

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    selected = []
    for line in lines:
        low = line.lower()
        if any(marker in low for marker in YOUTUBE_DIAGNOSTIC_MARKERS):
            selected.append(line)

    # If yt-dlp failed for another reason, retain the last useful stderr lines.
    if return_code != 0 and not selected:
        selected = lines[-8:]

    # Keep Actions logs readable even if a verbose response is unusually long.
    for line in selected[-30:]:
        print(f"[yt-dlp diagnostic] {line[:1200]}")


def build_command(url, output_template, quality, start_time, end_time, attempt):
    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "4",
        "--fragment-retries", "8",
        "--extractor-retries", "3",
        "--file-access-retries", "3",
        "--socket-timeout", "25",
        "--concurrent-fragments", str(attempt["fragments"]),
        "--throttled-rate", "100K",
        "--no-mtime",
        "--output", output_template,
    ]
    cmd += attempt["args"]

    # yt-dlp only reports provider discovery/token generation at debug level.
    # Enable verbose output for YouTube, then filter it in log_attempt_diagnostics().
    if is_youtube_url(url):
        cmd.append("--verbose")

    # Clipping: ffmpeg pulls only the requested range (no full download).
    # End 00:00:00 means "until the end of the video".
    if start_time != "00:00:00" or end_time != "00:00:00":
        end_arg = end_time if end_time != "00:00:00" else "inf"
        cmd += ["--download-sections", f"*{start_time}-{end_arg}"]
        if PRECISE_CUTS:
            cmd.append("--force-keyframes-at-cuts")

    if quality == "audio":
        cmd += ["--format", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        height = int(quality)
        fmt = (
            f"bv*[height<=?{height}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<=?{height}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<=?{height}]+ba/"
            f"b[height<=?{height}]/b"
        )
        cmd += ["--format", fmt, "--merge-output-format", "mp4"]

    cmd += ["--", url]
    return cmd


def _signal_process(process, sig):
    try:
        if IS_POSIX:
            os.killpg(os.getpgid(process.pid), sig)  # yt-dlp + its ffmpeg children
        else:
            process.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def execute_command(command):
    if STOP_EVENT.is_set():
        raise RuntimeError("انتهت جلسة التشغيل.")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=IS_POSIX,
    )

    with PROCESS_LOCK:
        active_processes.add(process)
        if STOP_EVENT.is_set():
            _signal_process(process, signal.SIGTERM)

    try:
        stdout, stderr = process.communicate(timeout=ATTEMPT_TIMEOUT_SECONDS)
        return process.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        _signal_process(process, signal.SIGKILL if IS_POSIX else signal.SIGTERM)
        stdout, stderr = process.communicate()
        return 124, stdout or "", (stderr or "") + "\nTimeout"
    finally:
        with PROCESS_LOCK:
            active_processes.discard(process)


def terminate_active_processes():
    with PROCESS_LOCK:
        processes = list(active_processes)
    for process in processes:
        if process.poll() is None:
            _signal_process(process, signal.SIGTERM)
    deadline = time.time() + 5
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            _signal_process(process, signal.SIGKILL if IS_POSIX else signal.SIGTERM)


def send_media(chat_id, path, is_audio, lang):
    caption = "✅ تفضل المقطع جاهز!" if lang == "ar" else "✅ Clip ready!"
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as media:
        if is_audio:
            bot.send_audio(chat_id, media, caption=caption, timeout=600)
            return
        if ext in VIDEO_SEND_EXTENSIONS:
            try:
                bot.send_video(chat_id, media, caption=caption, supports_streaming=True, timeout=600)
                return
            except Exception as exc:
                print(f"[send_video] failed, falling back to document: {exc}")
                media.seek(0)
        bot.send_document(chat_id, media, caption=caption, timeout=600)


def run_download(chat_id, snapshot, quality):
    lang = snapshot.get("lang", "ar")
    url = snapshot["url"]
    start_time = snapshot["start_time"]
    end_time = snapshot["end_time"]
    is_audio = quality == "audio"
    work_dir = None
    last_error = ""

    try:
        if STOP_EVENT.is_set():
            return

        free = shutil.disk_usage(tempfile.gettempdir()).free
        if free < MIN_FREE_BYTES:
            safe_send(chat_id, "⚠️ مساحة السيرفر ممتلئة مؤقتاً، حاول بعد قليل."
                      if lang == "ar" else "⚠️ Server disk is nearly full, try again shortly.")
            return

        work_dir = tempfile.mkdtemp(prefix="tgclip_")
        output_template = os.path.join(work_dir, "media.%(ext)s")
        attempts = get_attempts(url)
        media_path = None

        for index, attempt in enumerate(attempts, start=1):
            if STOP_EVENT.is_set():
                return
            clear_directory(work_dir)
            command = build_command(url, output_template, quality, start_time, end_time, attempt)
            print(f"[download] chat={chat_id} attempt={index}/{len(attempts)} mode={attempt['label']}")

            code, _out, err = execute_command(command)
            if is_youtube_url(url):
                log_attempt_diagnostics(attempt, code, err)
            elif code != 0:
                log_attempt_diagnostics(attempt, code, err)

            media_path = find_media_file(work_dir)
            if code == 0 and media_path:
                break

            media_path = None
            last_error = err or "yt-dlp finished without producing a file."
            if code == 124 or is_permanent_error(err):
                break  # retrying another mode will not help

        if not media_path and is_youtube_url(url) and not STOP_EVENT.is_set():
            print("[piped] Starting proxy fallback after yt-dlp failures.")
            try:
                media_path = download_youtube_via_piped(
                    url, work_dir, quality, start_time, end_time
                )
            except Exception as exc:
                print(f"[piped] fallback exception: {str(exc)[:500]}")
                media_path = None

        if not media_path:
            if is_youtube_url(url):
                last_error = (last_error or "") + "\nPublic Piped proxy fallback was also unavailable."
            safe_send(chat_id, summarize_error(last_error, url, lang))
            return

        size_mb = os.path.getsize(media_path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            safe_send(
                chat_id,
                f"⚠️ حجم الملف {size_mb:.1f}MB وتيليجرام (Bot API) يسمح بـ {MAX_UPLOAD_MB}MB فقط.\n"
                "اختر جودة أقل أو قص جزءاً أقصر."
                if lang == "ar"
                else f"⚠️ File is {size_mb:.1f}MB; Telegram Bot API allows {MAX_UPLOAD_MB}MB. "
                     "Choose lower quality or a shorter clip.",
            )
            return

        send_media(chat_id, media_path, is_audio, lang)

    except Exception as exc:
        if not STOP_EVENT.is_set():
            message = str(exc).strip() or exc.__class__.__name__
            safe_send(chat_id, f"❌ حدث خطأ: {message[:1500]}" if lang == "ar"
                      else f"❌ Error: {message[:1500]}")
    finally:
        cleanup_directory(work_dir)  # always delete temp files
        with STATE_LOCK:
            active_chats.discard(chat_id)


# ---------------------------- Time-step logic ----------------------------

def finish_time_step(chat_id, prefix, total_seconds):
    """Commit a start/end time. Returns (status, lang, time_text)."""
    time_text = seconds_to_time(total_seconds)
    with STATE_LOCK:
        state = user_data.get(chat_id)
        if not state or "url" not in state:
            return "expired", "ar", time_text
        lang = state.get("lang", "ar")
        expected = "setting_start_time" if prefix == "start" else "setting_end_time"
        if state.get("step") != expected:
            return "expired", lang, time_text

        if prefix == "start":
            state.update({"start_time": time_text, "step": "setting_end_time", "h": 0, "m": 0, "s": 0})
            return "start_ok", lang, time_text

        start_sec = time_to_seconds(state.get("start_time", "00:00:00")) or 0
        if total_seconds != 0 and total_seconds <= start_sec:
            return "bad_end", lang, time_text
        state.update({"end_time": time_text, "step": "choosing_quality"})
        return "end_ok", lang, time_text


def send_time_step_result(chat_id, status, lang, time_text):
    if status == "start_ok":
        safe_send(
            chat_id,
            f"✅ البداية: {time_text}\n\n⏱️ حدد وقت النهاية (اتركه 00:00:00 للتحميل حتى النهاية).\n"
            "يمكنك أيضاً كتابة الوقت مثل 1:30 أو 01:02:03."
            if lang == "ar"
            else f"✅ Start: {time_text}\n\n⏱️ Set the end time (leave 00:00:00 to download to the end).\n"
                 "You can also type a time like 1:30 or 01:02:03.",
            reply_markup=time_keyboard("end", 0, 0, 0),
        )
    elif status == "bad_end":
        safe_send(chat_id, "❌ يجب أن يكون وقت النهاية أكبر من وقت البداية."
                  if lang == "ar" else "❌ End time must be later than start time.")
    elif status == "end_ok":
        safe_send(chat_id, "🎬 اختر الجودة أو الصيغة المطلوبة للمقطع:"
                  if lang == "ar" else "🎬 Choose desired quality/format:",
                  reply_markup=quality_keyboard())


# ---------------------------- Telegram handlers ----------------------------

@bot.message_handler(commands=["start"])
def send_welcome(message):
    chat_id = message.chat.id
    with STATE_LOCK:
        state = user_data.setdefault(chat_id, {})
        lang = state.get("lang")
        if lang:
            state["step"] = "waiting_for_url"

    if lang:
        safe_send(
            chat_id,
            "مرحباً بك مجدداً! أرسل رابط أي فيديو الآن لقص المقطع:"
            if lang == "ar" else "Welcome back! Send any video link now to clip:",
            reply_markup=main_menu(lang),
        )
    else:
        safe_send(
            chat_id,
            "مرحباً بك! يرجى اختيار اللغة:\nWelcome! Please choose your language:",
            reply_markup=language_keyboard(),
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def set_language(call):
    chat_id = call.message.chat.id
    lang = call.data.split("_", 1)[1]
    if lang not in ("ar", "en"):
        safe_answer(call.id, "Invalid language")
        return

    with STATE_LOCK:
        user_data.setdefault(chat_id, {}).update(
            {"lang": lang, "step": "waiting_for_url", "h": 0, "m": 0, "s": 0}
        )

    safe_answer(call.id)
    safe_send(
        chat_id,
        "✅ تم اختيار اللغة العربية.\nالرجاء إرسال رابط الفيديو الآن (من أي موقع):"
        if lang == "ar" else "✅ English selected.\nPlease send the video link now (from any site):",
        reply_markup=main_menu(lang),
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    with STATE_LOCK:
        state = user_data.get(chat_id)
        lang = state.get("lang") if state else None
        step = state.get("step", "waiting_for_url") if state else None

    if not lang:
        send_welcome(message)
        return

    if text in ("🎬 قص فيديو جديد", "🎬 Clip New Video"):
        with STATE_LOCK:
            user_data[chat_id].update({"step": "waiting_for_url", "h": 0, "m": 0, "s": 0})
        safe_send(
            chat_id,
            "أرسل رابط أي فيديو الآن (يوتيوب، تيك توك، فيسبوك، إنستجرام...):"
            if lang == "ar" else "Send any video link now:",
            reply_markup=main_menu(lang),
        )
        return

    if text in ("🌐 تغيير اللغة", "🌐 Change Language"):
        safe_send(chat_id, "اختر اللغة الجديدة / Choose language:", reply_markup=language_keyboard())
        return

    if text in ("ℹ️ مساعدة", "ℹ️ Help"):
        safe_send(
            chat_id,
            "أرسل رابط فيديو عام، ثم حدد البداية والنهاية (بالأزرار أو بكتابة الوقت مثل 1:30) ثم الجودة.\n"
            "البوت لا يستخدم كوكيز ولا تسجيل دخول، لذلك الفيديوهات الخاصة أو المحمية قد لا تعمل، "
            "ولا يمكن ضمان دعم كل موقع. الحد الأقصى للملف 49MB."
            if lang == "ar"
            else "Send a public video link, set start/end (buttons or type a time like 1:30), then pick a quality.\n"
                 "No cookies or login are used, so private/protected videos may fail and not every site is "
                 "guaranteed. Max file size is 49MB.",
        )
        return

    url = extract_and_validate_url(text)
    if url:
        with STATE_LOCK:
            user_data[chat_id].update(
                {"url": url, "step": "setting_start_time", "h": 0, "m": 0, "s": 0}
            )
        safe_send(
            chat_id,
            "⏱️ حدد وقت البداية بالعداد (أو اكتب الوقت مثل 1:30):"
            if lang == "ar" else "⏱️ Set the start time (or type a time like 1:30):",
            reply_markup=time_keyboard("start", 0, 0, 0),
        )
        return

    if step in ("setting_start_time", "setting_end_time"):
        total = parse_time_text(text)
        if total is None:
            safe_send(chat_id, "❌ اكتب الوقت مثل 90 أو 1:30 أو 01:02:03، أو استخدم الأزرار."
                      if lang == "ar" else "❌ Type a time like 90, 1:30 or 01:02:03, or use the buttons.")
            return
        prefix = "start" if step == "setting_start_time" else "end"
        status, lang2, time_text = finish_time_step(chat_id, prefix, total)
        if status == "expired":
            safe_send(chat_id, "انتهت الخطوة؛ أرسل الرابط مجدداً." if lang2 == "ar"
                      else "Step expired; send the link again.")
        else:
            send_time_step_result(chat_id, status, lang2, time_text)
        return

    safe_send(chat_id, "❌ رابط غير صحيح. أرسل رابطاً يبدأ بـ http:// أو https://"
              if lang == "ar" else "❌ Invalid URL. Send a link starting with http:// or https://")


@bot.callback_query_handler(func=lambda call: call.data == "ignore")
def handle_ignore(call):
    safe_answer(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith(("start_", "end_")))
def handle_counter(call):
    chat_id = call.message.chat.id
    prefix, action = call.data.split("_", 1)

    with STATE_LOCK:
        state = user_data.get(chat_id)
        valid = bool(state) and "url" in state and state.get("step") == (
            "setting_start_time" if prefix == "start" else "setting_end_time"
        )
        if valid:
            h, m, s = state.get("h", 0), state.get("m", 0), state.get("s", 0)
            lang = state.get("lang", "ar")

    if not valid:
        safe_answer(call.id, "انتهت الخطوة؛ أرسل الرابط مجدداً.")
        return

    if action == "done":
        status, lang, time_text = finish_time_step(chat_id, prefix, h * 3600 + m * 60 + s)
        if status == "expired":
            safe_answer(call.id, "انتهت الخطوة؛ أرسل الرابط مجدداً.")
            return
        safe_answer(call.id, f"تم اعتماد الوقت: {time_text}")
        send_time_step_result(chat_id, status, lang, time_text)
        return

    if action == "h_plus":
        h = (h + 1) % 24
    elif action == "h_minus":
        h = (h - 1) % 24
    elif action == "m_plus":
        m = (m + 1) % 60
    elif action == "m_minus":
        m = (m - 1) % 60
    elif action == "s_plus":
        s = (s + 1) % 60
    elif action == "s_minus":
        s = (s - 1) % 60
    else:
        safe_answer(call.id)
        return

    with STATE_LOCK:
        user_data[chat_id].update({"h": h, "m": m, "s": s})

    safe_answer(call.id, f"⏱️ {h:02d}:{m:02d}:{s:02d}")
    if lang == "ar":
        label = "⏱️ حدد وقت البداية:" if prefix == "start" else "⏱️ حدد وقت النهاية:"
    else:
        label = "⏱️ Set start time:" if prefix == "start" else "⏱️ Set end time:"
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=label,
            reply_markup=time_keyboard(prefix, h, m, s),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("quality_"))
def handle_quality(call):
    chat_id = call.message.chat.id
    quality = call.data.split("_", 1)[1]

    if quality not in QUALITY_HEIGHTS and quality != "audio":
        safe_answer(call.id, "اختيار غير صحيح")
        return
    if DRAINING.is_set() or STOP_EVENT.is_set():
        safe_answer(call.id, "البوت يعيد التشغيل الآن، حاول بعد دقائق.")
        return

    with STATE_LOCK:
        state = user_data.get(chat_id, {})
        ready = all(k in state for k in ("url", "start_time", "end_time")) and \
            state.get("step") == "choosing_quality"
        if not ready:
            snapshot = None
        elif chat_id in active_chats:
            snapshot = "busy"
        else:
            snapshot = dict(state)
            active_chats.add(chat_id)
            state["step"] = "waiting_for_url"
        queued = len(active_chats) > MAX_DOWNLOAD_WORKERS

    if snapshot is None:
        safe_answer(call.id, "انتهت الجلسة؛ أرسل الرابط مجدداً.")
        return
    if snapshot == "busy":
        safe_answer(call.id, "لديك عملية تحميل تعمل بالفعل.")
        return

    lang = snapshot.get("lang", "ar")
    safe_answer(call.id, f"جاري التحميل: {quality}")
    wait_note = (" (في قائمة الانتظار)" if lang == "ar" else " (queued)") if queued else ""
    safe_send(
        chat_id,
        f"⚡ جاري معالجة الفيديو ({quality}) من {snapshot['start_time']} إلى "
        f"{snapshot['end_time']}{wait_note}..."
        if lang == "ar"
        else f"⚡ Processing video ({quality}) {snapshot['start_time']} → "
             f"{snapshot['end_time']}{wait_note}...",
    )

    try:
        executor.submit(run_download, chat_id, snapshot, quality)
    except Exception as exc:
        with STATE_LOCK:
            active_chats.discard(chat_id)
        safe_send(chat_id, f"تعذر بدء التحميل: {str(exc)[:500]}")


# ---------------------------- Lifecycle ----------------------------

def safe_stop_polling():
    try:
        bot.stop_polling()
    except Exception:
        pass


def lifecycle():
    """After MAX_LIFETIME: stop taking work, let running downloads finish, then stop."""
    if STOP_EVENT.wait(MAX_LIFETIME_SECONDS):
        return
    print("Worker lifetime reached: draining.")
    DRAINING.set()
    safe_stop_polling()

    deadline = time.time() + DRAIN_GRACE_SECONDS
    while time.time() < deadline and not STOP_EVENT.is_set():
        with STATE_LOCK:
            busy = bool(active_chats)
        if not busy:
            break
        time.sleep(2)

    STOP_EVENT.set()
    terminate_active_processes()


def _handle_signal(signum, _frame):
    STOP_EVENT.set()
    DRAINING.set()
    safe_stop_polling()
    raise KeyboardInterrupt


def flush_pending_updates():
    """Skip messages sent while no worker was running (their sessions are gone anyway)."""
    try:
        updates = bot.get_updates(offset=-1, timeout=0)
        if updates:
            bot.get_updates(offset=updates[-1].update_id + 1, timeout=0)
    except Exception as exc:
        print(f"[startup] could not flush pending updates: {exc}")


def main():
    print("Telegram video bot started (no cookies).")
    print(f"concurrent fragments: {FRAGMENTS} | impersonation: {HAS_IMPERSONATION} | precise cuts: {PRECISE_CUTS}")
    print("YouTube fallback: yt-dlp clients, then public Piped proxy instances (no local-network download).")
    print("Checking local YouTube PO Token provider...")
    check_po_token_provider()

    try:
        bot.remove_webhook()
    except Exception as exc:
        print(f"[startup] remove_webhook failed: {exc}")
    flush_pending_updates()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    life = threading.Thread(target=lifecycle, name="lifecycle", daemon=True)
    life.start()

    try:
        while not (DRAINING.is_set() or STOP_EVENT.is_set()):
            try:
                bot.polling(
                    non_stop=True,
                    timeout=30,
                    long_polling_timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[polling] error: {exc}")
            if not (DRAINING.is_set() or STOP_EVENT.is_set()):
                time.sleep(3)
        life.join()  # wait for running downloads to finish (bounded by DRAIN_GRACE)
    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        STOP_EVENT.set()
        DRAINING.set()
        terminate_active_processes()
        executor.shutdown(wait=True, cancel_futures=True)
        print("Bot stopped cleanly.")


if __name__ == "__main__":
    main()