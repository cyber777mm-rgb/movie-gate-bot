# Movie Myanmar - single-file bot + embedded Mini App
import os
import asyncio
import hashlib
import json
import threading
import re
import traceback
import html
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from google import genai

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    from telegram import WebAppInfo, MenuButtonWebApp
except ImportError:
    WebAppInfo = MenuButtonWebApp = None


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MAIN_CHANNEL = "@moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"

RENDER_URL = "https://movie-gate-bot.onrender.com"
WEBHOOK_PATH = "/telegram-webhook"

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8072749262").strip()

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode()
).hexdigest()[:32]

FREE_AI_LIMIT = 20
GEMINI_MODEL = "gemini-3.8-flash"

WELCOME_IMAGE = (
    "https://raw.githubusercontent.com/cyber777mm-rgb/"
    "movie-gate-bot/main/"
    "ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"
)


# ============================================================
# PERSISTENT DATA DIRECTORY
# ============================================================

# Render Persistent Disk should be mounted at /data.
#
# You can also override this with:
# MOVIE_DATA_DIR=/data
#
# If /data is not available, we fall back to ./data.
# NOTE:
# The fallback directory is NOT persistent on Render.

REQUESTED_DATA_DIR = os.getenv("MOVIE_DATA_DIR", "/data").strip()

try:
    os.makedirs(REQUESTED_DATA_DIR, exist_ok=True)
    DATA_DIR = REQUESTED_DATA_DIR
except Exception:
    DATA_DIR = os.path.abspath("./data")
    os.makedirs(DATA_DIR, exist_ok=True)

print("Movie data directory:", DATA_DIR)

MOVIE_DB_FILE = os.path.join(DATA_DIR, "movies.json")
AI_USAGE_FILE = os.path.join(DATA_DIR, "ai_usage.json")


# ============================================================
# GLOBAL STATE
# ============================================================

movie_database = []
ai_usage = {}

user_search_mode = {}
user_ai_mode = {}
user_last_results = {}

webhook_application = None
webhook_loop = None

poster_cache = {}


# ============================================================
# GENRES
# ============================================================

GENRE_KEYWORDS = {
    "Action": ["action", "အက်ရှင်"],
    "Comedy": ["comedy", "ဟာသ"],
    "Romance": ["romance", "romantic", "အချစ်"],
    "Horror": ["horror", "ကြောက်", "သရဲ"],
    "Thriller": ["thriller"],
    "Drama": ["drama", "ဒရာမာ"],
    "Fantasy": ["fantasy"],
}


CHANNEL_REQUEST_WORDS = [
    "channel link",
    "channel",
    "ချန်နယ်",
    "ချန်နယ်လင့်",
    "movie channel",
    "channel ပေး",
    "channel ပး",
]

MOVIE_INTENT_WORDS = [
    "movie",
    "film",
    "ကား",
    "ဇာတ်ကား",
    "ရုပ်ရှင်",
    "ရှာပေး",
    "ရှာပေးပါ",
    "ကားပေး",
]


# ============================================================
# CAROUSEL / CACHE
# ============================================================

CAROUSEL_LIMIT = 5
CAROUSEL_VISIBLE = 3

carousel_message_ids = {}
carousel_gif_cache = {
    "signature": None,
    "bytes": None,
}


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print("load_json error:", path, e)
        return default


def save_json(path, data):
    """
    Atomic JSON write.
    Writes to a temporary file first, then replaces the original.
    """

    temp_path = path + ".tmp"

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)

    except Exception as e:
        print("save error:", path, e)

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


# ============================================================
# OLD DATA MIGRATION
# ============================================================

def migrate_old_data():
    """
    If an old movies.json / ai_usage.json exists in the application
    directory and the persistent /data file doesn't exist yet,
    copy the old data into the persistent directory.

    This helps protect existing movies when moving to Persistent Disk.
    """

    old_movie_file = os.path.abspath("movies.json")
    old_ai_file = os.path.abspath("ai_usage.json")

    if (
        os.path.abspath(old_movie_file) != os.path.abspath(MOVIE_DB_FILE)
        and os.path.exists(old_movie_file)
        and not os.path.exists(MOVIE_DB_FILE)
    ):
        try:
            old_movies = load_json(old_movie_file, [])

            if isinstance(old_movies, list) and old_movies:
                save_json(MOVIE_DB_FILE, old_movies)
                print(
                    "Migrated old movies.json ->",
                    MOVIE_DB_FILE,
                    "count=",
                    len(old_movies),
                )
        except Exception:
            print("Movie migration error:")
            print(traceback.format_exc())

    if (
        os.path.abspath(old_ai_file) != os.path.abspath(AI_USAGE_FILE)
        and os.path.exists(old_ai_file)
        and not os.path.exists(AI_USAGE_FILE)
    ):
        try:
            old_usage = load_json(old_ai_file, {})

            if isinstance(old_usage, dict):
                save_json(AI_USAGE_FILE, old_usage)
                print(
                    "Migrated old ai_usage.json ->",
                    AI_USAGE_FILE,
                )
        except Exception:
            print("AI usage migration error:")
            print(traceback.format_exc())


migrate_old_data()


# ============================================================
# MOVIE ID
# ============================================================

def generate_movie_id(chat_id, message_id):
    """
    Stable ID based on Telegram channel + message ID.

    Same movie post = same ID.
    """

    raw = f"{chat_id}:{message_id}"

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def ensure_movie_id(movie):
    """
    Make sure every old movie record has a stable ID.
    """

    existing = str(movie.get("id", "")).strip()

    if existing:
        return existing

    chat_id = movie.get("chat_id", "")
    message_id = movie.get("message_id", "")

    if chat_id and message_id:
        movie["id"] = generate_movie_id(
            chat_id,
            message_id,
        )
    else:
        # Fallback for malformed legacy records.
        raw = json.dumps(
            movie,
            ensure_ascii=False,
            sort_keys=True,
        )

        movie["id"] = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:16]

    return movie["id"]


def normalize_movie_database():
    changed = False

    for movie in movie_database:
        if not isinstance(movie, dict):
            continue

        old_id = movie.get("id")

        ensure_movie_id(movie)

        if movie.get("id") != old_id:
            changed = True

        if not isinstance(movie.get("genres"), list):
            movie["genres"] = []

    return changed


# ============================================================
# MOVIE DATABASE
# ============================================================

def load_movies():
    global movie_database

    x = load_json(MOVIE_DB_FILE, [])

    if isinstance(x, list):
        movie_database = x
    else:
        movie_database = []

    changed = normalize_movie_database()

    if changed:
        save_movies()

    print(
        "Loaded movies:",
        len(movie_database),
        "from",
        MOVIE_DB_FILE,
    )


def save_movies():
    save_json(
        MOVIE_DB_FILE,
        movie_database,
    )


def load_ai_usage():
    global ai_usage

    x = load_json(AI_USAGE_FILE, {})

    ai_usage = x if isinstance(x, dict) else {}

    print(
        "Loaded AI usage:",
        len(ai_usage),
    )


def save_ai_usage():
    save_json(
        AI_USAGE_FILE,
        ai_usage,
    )


def get_ai_usage(uid):
    return int(
        ai_usage.get(
            str(uid),
            0,
        )
    )


def increase_ai_usage(uid):
    ai_usage[str(uid)] = (
        get_ai_usage(uid) + 1
    )

    save_ai_usage()


load_movies()
load_ai_usage()


# ============================================================
# GEMINI
# ============================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )
    except Exception as e:
        print("Gemini init error:", e)


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(s):
    s = (s or "").lower()

    s = re.sub(
        r"[^\w\u1000-\u109f]+",
        " ",
        s,
        flags=re.UNICODE,
    )

    return re.sub(
        r"\s+",
        " ",
        s,
    ).strip()


def extract_year(s):
    m = re.search(
        r"\b(19\d{2}|20\d{2})\b",
        s or "",
    )

    return m.group(1) if m else ""


def detect_genres(s):
    n = normalize_text(s)

    return [
        genre
        for genre, words in GENRE_KEYWORDS.items()
        if any(
            normalize_text(word) in n
            for word in words
        )
    ]


# ============================================================
# MOVIE POST PARSER
# ============================================================

def parse_movie_post(message):
    text = (
        message.caption
        or message.text
        or ""
    )

    lines = [
        x.strip()
        for x in text.splitlines()
        if x.strip()
    ]

    title = (
        lines[0]
        if lines
        else "Untitled Movie"
    )

    year = extract_year(text)

    quality_match = re.search(
        r"\b(2160p|4k|1080p|720p|480p)\b",
        text,
        re.I,
    )

    audio_match = re.search(
        r"(Burmese|Myanmar|English|Dual|Multi)[ -]?Audio",
        text,
        re.I,
    )

    poster = (
        message.photo[-1].file_id
        if message.photo
        else ""
    )

    movie = {
        "title": title,
        "year": year,
        "quality": (
            quality_match.group(1).upper()
            if quality_match
            else ""
        ),
        "audio": (
            audio_match.group(0)
            if audio_match
            else ""
        ),
        "original_name": title,
        "genres": detect_genres(text),
        "description": text,
        "post_link": "",
        "chat_id": str(message.chat_id),
        "message_id": message.message_id,
        "poster_file_id": poster,
    }

    movie["id"] = generate_movie_id(
        message.chat_id,
        message.message_id,
    )

    return movie


# ============================================================
# MOVIE LINK
# ============================================================

def build_movie_link(movie):
    chat = str(
        movie.get(
            "chat_id",
            "",
        )
    )

    mid = movie.get("message_id")

    if chat.startswith("-100") and mid:
        return (
            f"https://t.me/c/"
            f"{chat[4:]}/{mid}"
        )

    return (
        movie.get("post_link")
        or MOVIE_CHANNEL_URL
    )


# ============================================================
# MOVIE LOOKUP
# ============================================================

def find_movie_by_id(movie_id):
    movie_id = str(movie_id)

    for movie in movie_database:
        if str(movie.get("id", "")) == movie_id:
            return movie

    return None


def movie_search(q):
    qn = normalize_text(q)

    if not qn:
        return []

    year = extract_year(q)

    scored = []

    for movie in movie_database:

        hay = normalize_text(
            " ".join(
                [
                    movie.get("title", ""),
                    movie.get("original_name", ""),
                    movie.get("year", ""),
                    " ".join(
                        movie.get(
                            "genres",
                            [],
                        )
                    ),
                ]
            )
        )

        score = 0

        if (
            qn
            == normalize_text(
                movie.get(
                    "title",
                    "",
                )
            )
        ):
            score += 100

        if qn in hay:
            score += 60

        if (
            year
            and year
            == movie.get(
                "year",
                "",
            )
        ):
            score += 40

        score += int(
            SequenceMatcher(
                None,
                qn,
                normalize_text(
                    movie.get(
                        "title",
                        "",
                    )
                ),
            ).ratio()
            * 30
        )

        if score >= 25:
            scored.append(
                (
                    score,
                    movie,
                )
            )

    scored.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return [
        movie
        for _, movie in scored
    ]


def latest(limit=10):
    return list(
        reversed(
            movie_database[-limit:]
        )
    )


# ============================================================
# CACHE
# ============================================================

def invalidate_carousel():
    carousel_gif_cache.update(
        signature=None,
        bytes=None,
    )

    poster_cache.clear()


# ============================================================
# KEYBOARDS
# ============================================================

def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 Join Movie Myanmar",
                    url=MOVIE_CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ I Joined",
                    callback_data="check_join",
                )
            ],
        ]
    )


def home_keyboard():
    rows = [
        [
            InlineKeyboardButton(
                "🔎 Search Movie",
                callback_data="search",
            ),
            InlineKeyboardButton(
                "🎭 Categories",
                callback_data="categories",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔥 Latest Movies",
                callback_data="latest",
            ),
            InlineKeyboardButton(
                "🤖 Xiao Yu AI",
                callback_data="ai",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏭️ Next Movie",
                callback_data="next_poster",
            ),
            InlineKeyboardButton(
                "📺 Movie Channel",
                url=MOVIE_CHANNEL_URL,
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            )
        ],
    ]

    if WebAppInfo:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    "✨ Open Movie App",
                    web_app=WebAppInfo(
                        url=RENDER_URL + "/app"
                    ),
                )
            ],
        )

    return InlineKeyboardMarkup(rows)


def category_keyboard():
    genres = [
        "Action",
        "Comedy",
        "Romance",
        "Horror",
        "Thriller",
        "Drama",
        "Fantasy",
    ]

    rows = [
        [
            InlineKeyboardButton(
                "🎬 All Movies",
                callback_data="genre:All",
            )
        ]
    ]

    for i in range(
        0,
        len(genres),
        2,
    ):
        row = [
            InlineKeyboardButton(
                genres[i],
                callback_data="genre:" + genres[i],
            )
        ]

        if i + 1 < len(genres):
            row.append(
                InlineKeyboardButton(
                    genres[i + 1],
                    callback_data=(
                        "genre:"
                        + genres[i + 1]
                    ),
                )
            )

        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ============================================================
# HOME
# ============================================================

def home_caption():
    return (
        "🎬 <b>Movie Myanmar</b>\n\n"
        "ဟေး 😝 Xiao Yu CyB ပါ။\n"
        "ကားရှာမယ်၊ နောက်ဆုံးကားတွေကြည့်မယ်၊ "
        "AI နဲ့လည်း စကားပြောလို့ရတယ်။\n\n"
        "👇 အောက်က Menu ကနေရွေးနော်။"
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def is_member(bot, uid):
    try:
        status = await bot.get_chat_member(
            MAIN_CHANNEL,
            uid,
        )

        return status.status in (
            "member",
            "administrator",
            "creator",
            "restricted",
        )

    except Exception:
        return False


async def stop_carousel(chat_id):
    carousel_message_ids.pop(
        chat_id,
        None,
    )


# ============================================================
# HOME COMMAND
# ============================================================

async def show_home(update, context):
    query = update.callback_query

    if query:
        await query.answer()

    target = (
        query.message
        if query
        else update.effective_message
    )

    try:
        await target.reply_photo(
            WELCOME_IMAGE,
            caption=home_caption(),
            parse_mode="HTML",
            reply_markup=home_keyboard(),
        )
    except Exception:
        await target.reply_text(
            home_caption(),
            parse_mode="HTML",
            reply_markup=home_keyboard(),
        )


async def start_command(update, context):
    uid = update.effective_user.id

    if not await is_member(
        context.bot,
        uid,
    ):
        await update.effective_message.reply_text(
            "🎬 Movie Myanmar ထဲဝင်ပြီးမှ "
            "သုံးလို့ရမယ်နော် 😝",
            reply_markup=join_keyboard(),
        )
        return

    user_search_mode.pop(
        uid,
        None,
    )

    user_ai_mode.pop(
        uid,
        None,
    )

    await show_home(
        update,
        context,
    )


async def check_join(update, context):
    query = update.callback_query

    await query.answer()

    if await is_member(
        context.bot,
        query.from_user.id,
    ):
        await query.edit_message_text(
            "✅ ဝင်ပြီးပြီဟေ့ 😝 "
            "ခဏ... Movie Myanmar ဖွင့်ပေးမယ်။"
        )

        await asyncio.sleep(1)

        await show_home(
            update,
            context,
        )

    else:
        await query.answer(
            "အရင် Join လုပ်ပါဦး 😒",
            show_alert=True,
        )


# ============================================================
# SEARCH
# ============================================================

async def start_search(update, context):
    query = update.callback_query

    if query:
        await query.answer()
        message = query.message
    else:
        message = update.effective_message

    user_search_mode[
        update.effective_user.id
    ] = True

    await message.reply_text(
        "🔎 Movie နာမည်ရေးပေးနော်။\n"
        "ဥပမာ — Avatar 2009"
    )


async def show_movie_list(
    message,
    movies,
    title="🎬 Movies",
):
    if not movies:
        await message.reply_text(
            "😒 မတွေ့သေးဘူးဟ။ "
            "Movie နာမည်နည်းနည်းတိတိကျကျ "
            "ရေးကြည့်။"
        )
        return

    buttons = []

    for movie in movies[:15]:
        label = movie.get(
            "title",
            "Movie",
        )[:48]

        if movie.get("year"):
            label += (
                f" ({movie['year']})"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "🎬 " + label,
                    url=build_movie_link(movie),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ]
    )

    await message.reply_text(
        title,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


# ============================================================
# LATEST / GENRES
# ============================================================

async def show_latest_movies(
    update,
    context,
):
    query = update.callback_query

    if query:
        await query.answer()
        message = query.message
    else:
        message = update.effective_message

    await show_movie_list(
        message,
        latest(15),
        "🔥 <b>Latest Movies</b>",
    )


async def show_categories(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🎭 Genre ရွေးနော်",
        reply_markup=category_keyboard(),
    )


async def show_genre(
    update,
    context,
    genre,
):
    query = update.callback_query

    await query.answer()

    if genre == "All":
        movies = latest(15)
    else:
        movies = [
            movie
            for movie in reversed(
                movie_database
            )
            if genre in movie.get(
                "genres",
                [],
            )
        ][:15]

    await show_movie_list(
        query.message,
        movies,
        f"🎭 <b>{html.escape(genre)}</b>",
    )


# ============================================================
# HELP
# ============================================================

async def show_help(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🔎 Search Movie — ကားနာမည်ရှာ\n"
        "🔥 Latest — နောက်ဆုံးတင်ထားတဲ့ကား\n"
        "🎭 Categories — Genre နဲ့ရွေး\n"
        "🤖 Xiao Yu — AI Chat (Free 20 requests)\n"
        "✨ Movie App — Neon Mini App"
    )


# ============================================================
# AI
# ============================================================

async def start_ai(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    uid = query.from_user.id
    used = get_ai_usage(uid)

    if used >= FREE_AI_LIMIT:
        await query.message.reply_text(
            "ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\n"
            "ငါ့ကို Movie မေး 😝🎬\n"
            "ကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။"
        )
        return

    user_ai_mode[uid] = True

    await query.message.reply_text(
        "🤖 Xiao Yu CyB ပါ 😝\n"
        "ဘာမေးမလဲ?\n\n"
        f"Free AI ကျန်တာ — "
        f"{FREE_AI_LIMIT - used} ခါ"
    )


async def ask_gemini(text):
    if not gemini_client:
        return None

    prompt = (
        "You are Xiao Yu CyB, a playful cute Burmese "
        "female movie assistant. "
        "Reply in natural Burmese, concise and friendly. "
        "Help with movies and general chat. "
        "Do not claim you can access unavailable information. "
        "User: "
        + text
    )

    try:
        result = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        return getattr(
            result,
            "text",
            None,
        )

    except Exception as e:
        print(
            "Gemini error:",
            e,
        )
        return None


# ============================================================
# ADMIN REQUEST
# ============================================================

async def send_admin_request(
    context,
    update,
    query,
):
    user = update.effective_user

    try:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            (
                "🔔 <b>Movie Request</b>\n\n"
                f"🎬 Requested: "
                f"<b>{html.escape(query)}</b>\n"
                f"👤 User: "
                f"@{html.escape(user.username or 'NoUsername')}\n"
                f"🆔 ID: "
                f"<code>{user.id}</code>"
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        print(
            "admin request error:",
            e,
        )


# ============================================================
# MESSAGE INTENTS
# ============================================================

def has_movie_intent(text):
    n = normalize_text(text)

    return any(
        normalize_text(word) in n
        for word in MOVIE_INTENT_WORDS
    )


def is_channel_request(text):
    n = normalize_text(text)

    return any(
        normalize_text(word) in n
        for word in CHANNEL_REQUEST_WORDS
    )


async def handle_special_reply(
    message,
    text,
):
    n = normalize_text(text)

    if any(
        x in n
        for x in [
            "မင်္ဂလာပါ",
            "ဟယ်လို",
            "hello",
            "hi",
        ]
    ):
        await message.reply_text(
            "ဟေး 😝 Xiao Yu CyB ပါ။ "
            "Movie ရှာမလား?"
        )
        return True

    if "နေကောင်း" in n:
        await message.reply_text(
            "ကောင်းတယ်ဟ 😝 Movie တွေစောင့်ပေးနေတာ။ "
            "မင်းကော?"
        )
        return True

    return False


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(
    update,
    context,
):
    message = update.effective_message
    uid = update.effective_user.id
    text = message.text or ""

    if not await is_member(
        context.bot,
        uid,
    ):
        await message.reply_text(
            "🎬 Movie Myanmar ကို Join "
            "လုပ်ပြီးမှ သုံးလို့ရမယ်နော်။",
            reply_markup=join_keyboard(),
        )
        return

    if text.lower() in (
        "home",
        "menu",
        "/home",
    ):
        user_search_mode.pop(
            uid,
            None,
        )

        user_ai_mode.pop(
            uid,
            None,
        )

        await show_home(
            update,
            context,
        )
        return

    if is_channel_request(text):
        await message.reply_text(
            "📺 Movie Myanmar Channel",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎬 Open Channel",
                            url=MOVIE_CHANNEL_URL,
                        )
                    ]
                ]
            ),
        )
        return

    if user_search_mode.get(uid):
        user_search_mode.pop(
            uid,
            None,
        )

        results = movie_search(text)

        if results:
            await show_movie_list(
                message,
                results,
                "🔎 <b>Search Results</b>",
            )
        else:
            await send_admin_request(
                context,
                update,
                text,
            )

            await message.reply_text(
                (
                    f"😒 <b>{html.escape(text)}</b> "
                    "ကို Movie Myanmar ထဲမှာ "
                    "မတွေ့သေးဘူးဟာ။\n\n"
                    "Request ပို့ထားပေးတယ်။"
                ),
                parse_mode="HTML",
            )

        return

    if user_ai_mode.get(uid):

        if get_ai_usage(uid) >= FREE_AI_LIMIT:
            user_ai_mode.pop(
                uid,
                None,
            )

            await message.reply_text(
                "ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\n"
                "ငါ့ကို Movie မေး 😝🎬\n"
                "ကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။"
            )
            return

        answer = await ask_gemini(text)

        if answer:
            increase_ai_usage(uid)

            await message.reply_text(
                answer
                + "\n\n🤖 AI ကျန် — "
                + str(
                    FREE_AI_LIMIT
                    - get_ai_usage(uid)
                )
                + " ခါ"
            )

        else:
            await message.reply_text(
                "😵 AI ခဏအလုပ်များနေတယ်။ "
                "Movie နာမည်နဲ့လည်း စမ်းရှာလို့ရတယ်နော်။"
            )

        return

    results = movie_search(text)

    if results and (
        has_movie_intent(text)
        or len(text.split()) <= 5
    ):
        await show_movie_list(
            message,
            results,
            "🔎 <b>Movie Results</b>",
        )
        return

    if has_movie_intent(text):
        await send_admin_request(
            context,
            update,
            text,
        )

        await message.reply_text(
            (
                f"😒 <b>{html.escape(text)}</b> "
                "ကို မတွေ့သေးဘူးဟ။ "
                "Request ပို့ထားပေးတယ်။\n\n"
                "Movie နာမည်ကို တိတိကျကျရေးရင် "
                "ပိုကောင်းတယ် 😝"
            ),
            parse_mode="HTML",
        )
        return

    if await handle_special_reply(
        message,
        text,
    ):
        return

    await message.reply_text(
        "😝 ဘာလုပ်မလဲ? "
        "Movie ရှာချင်ရင် <b>Search Movie</b> ကိုနှိပ်၊ "
        "AI ပြောချင်ရင် <b>Xiao Yu AI</b> ကိုနှိပ်နော်။",
        parse_mode="HTML",
        reply_markup=home_keyboard(),
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(
    update,
    context,
):
    query = update.callback_query
    data = query.data or ""

    if data == "check_join":
        return await check_join(
            update,
            context,
        )

    if data == "home":
        return await show_home(
            update,
            context,
        )

    if data == "search":
        return await start_search(
            update,
            context,
        )

    if data == "categories":
        return await show_categories(
            update,
            context,
        )

    if data == "latest":
        return await show_latest_movies(
            update,
            context,
        )

    if data == "ai":
        return await start_ai(
            update,
            context,
        )

    if data == "help":
        return await show_help(
            update,
            context,
        )

    if data.startswith("genre:"):
        return await show_genre(
            update,
            context,
            data.split(
                ":",
                1,
            )[1],
        )

    if data == "next_poster":
        await query.answer()

        if WebAppInfo:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✨ Open Movie App",
                            web_app=WebAppInfo(
                                url=RENDER_URL
                                + "/app"
                            ),
                        )
                    ]
                ]
            )
        else:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📺 Movie Channel",
                            url=MOVIE_CHANNEL_URL,
                        )
                    ]
                ]
            )

        await query.message.reply_text(
            "⏭️ Latest movie carousel ကို "
            "Mini App ထဲမှာ ပိုလှအောင်ကြည့်လို့ရတယ် 😝",
            reply_markup=markup,
        )

        return

    await query.answer()


# ============================================================
# CHANNEL POST HANDLER
# ============================================================

async def channel_post_handler(
    update,
    context,
):
    message = update.channel_post

    if not message:
        return

    username = (
        message.chat.username
        or ""
    ).lower()

    if username != (
        MAIN_CHANNEL
        .lstrip("@")
        .lower()
    ):
        return

    movie = parse_movie_post(
        message
    )

    movie["post_link"] = (
        f"https://t.me/"
        f"{message.chat.username}/"
        f"{message.message_id}"
    )

    movie_id = movie["id"]

    replaced = False

    for i, old in enumerate(
        movie_database
    ):
        old_id = str(
            old.get(
                "id",
                "",
            )
        )

        old_chat = str(
            old.get(
                "chat_id",
                "",
            )
        )

        old_message = old.get(
            "message_id"
        )

        if (
            old_id == movie_id
            or (
                old_chat
                == str(message.chat_id)
                and old_message
                == message.message_id
            )
        ):
            movie_database[i] = movie
            replaced = True
            break

    if not replaced:
        movie_database.append(
            movie
        )

    save_movies()

    invalidate_carousel()

    print(
        "Saved movie:",
        movie.get("title"),
        "ID:",
        movie.get("id"),
        "Total:",
        len(movie_database),
    )


# ============================================================
# MINI APP
# ============================================================

MINI_APP_HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Movie Myanmar</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

*{
    box-sizing:border-box
}

body{
    margin:0;
    background:#07070d;
    color:#f6f4ff;
    font-family:Arial,sans-serif;
    overflow-x:hidden
}

.app{
    width:100%;
    min-height:100vh;
    background:
        radial-gradient(
            circle at 20% 0,
            #22104a 0,
            #0b0913 38%,
            #05050a 100%
        );
    padding-bottom:28px
}

.top{
    position:sticky;
    top:0;
    z-index:10;
    padding:18px 16px 12px;
    background:rgba(7,7,13,.82);
    backdrop-filter:blur(16px);
    border-bottom:1px solid #282039
}

.brand{
    font-size:24px;
    font-weight:900;
    letter-spacing:1px
}

.brand span{
    color:#6cf6ff
}

.sub{
    font-size:12px;
    color:#aaa3bd;
    margin-top:4px
}

.search{
    margin-top:15px;
    display:flex;
    gap:8px
}

.search input{
    flex:1;
    background:#11101b;
    border:1px solid #302846;
    border-radius:14px;
    color:#fff;
    padding:13px 14px;
    outline:none
}

.search input::placeholder{
    color:#858094
}

.search button,
.pill{
    border:0;
    border-radius:13px;
    padding:0 15px;
    background:linear-gradient(
        135deg,
        #5af5ff,
        #bb5cff
    );
    color:#08080e;
    font-weight:800
}

.section{
    padding:18px 14px 0
}

.sectionhead{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:11px
}

.sectionhead h2{
    font-size:16px;
    margin:0
}

.muted{
    color:#8f899f;
    font-size:11px
}

.rail{
    display:flex;
    gap:11px;
    overflow-x:auto;
    padding-bottom:5px;
    scroll-snap-type:x mandatory
}

.rail::-webkit-scrollbar{
    display:none
}

.card{
    min-width:150px;
    width:150px;
    scroll-snap-align:start;
    background:#100e18;
    border:1px solid #29233a;
    border-radius:16px;
    overflow:hidden;
    box-shadow:0 10px 30px #0007;
    cursor:pointer
}

.card:active{
    transform:scale(.98)
}

.poster{
    height:215px;
    width:100%;
    object-fit:cover;
    background:#181522
}

.ct{
    padding:10px
}

.title{
    font-size:13px;
    font-weight:800;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis
}

.meta{
    font-size:10px;
    color:#9d96ad;
    margin-top:5px
}

.chips{
    display:flex;
    gap:8px;
    flex-wrap:wrap
}

/* FIXED GENRE UI */

.chip{
    padding:10px 13px;
    border:1px solid #51456e;
    background:#171326;
    color:#ffffff;
    border-radius:13px;
    font-size:12px;
    font-weight:800;
    cursor:pointer;
    transition:.15s ease;
    text-shadow:0 1px 2px #000
}

.chip:hover{
    background:#282044;
    border-color:#7c6aa5
}

.chip:active{
    transform:scale(.96)
}

.chip.active{
    background:linear-gradient(
        135deg,
        #5af5ff,
        #bb5cff
    );
    color:#07070d;
    border-color:transparent;
    text-shadow:none
}

.nav{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:9px
}

.nav button{
    padding:12px 7px;
    border:1px solid #302a43;
    background:#11101a;
    color:#ffffff;
    border-radius:13px;
    font-weight:700
}

.nav button:active{
    background:#211b31
}

.empty{
    padding:30px 10px;
    text-align:center;
    color:#9992a8
}

.overlay{
    display:none;
    position:fixed;
    inset:0;
    background:#000b;
    z-index:30;
    padding:30px 14px
}

.modal{
    margin:auto;
    max-width:480px;
    background:#11101a;
    border:1px solid #3a3150;
    border-radius:20px;
    overflow:hidden;
    max-height:90vh;
    overflow-y:auto
}

.modal img{
    width:100%;
    max-height:390px;
    object-fit:cover
}

.mb{
    padding:16px
}

.close{
    float:right;
    background:#292338;
    border:0;
    color:white;
    border-radius:10px;
    padding:7px 11px
}

.open{
    display:block;
    width:100%;
    padding:13px;
    border:0;
    border-radius:13px;
    background:linear-gradient(
        135deg,
        #5af5ff,
        #bb5cff
    );
    font-weight:900;
    margin-top:14px
}

.loader{
    text-align:center;
    padding:20px;
    color:#aaa
}

</style>
</head>

<body>

<main class="app">

<header class="top">

<div class="brand">
MOVIE <span>MYANMAR</span>
</div>

<div class="sub">
Xiao Yu CyB • Cinema Mini App
</div>

<div class="search">

<input
    id="q"
    placeholder="Search movie..."
>

<button onclick="searchMovies()">
🔎
</button>

</div>

</header>


<section class="section">

<div class="nav">

<button onclick="loadLatest()">
🔥 Latest
</button>

<button onclick="showGenres()">
🎭 Genres
</button>

<button onclick="openAi()">
🤖 Xiao Yu
</button>

</div>

</section>


<section class="section">

<div class="sectionhead">

<h2>
NOW SHOWING
</h2>

<span class="muted">
Swipe →
</span>

</div>

<div id="rail" class="rail">

<div class="loader">
Loading movies...
</div>

</div>

</section>


<section class="section">

<div class="sectionhead">

<h2>
GENRES
</h2>

</div>

<div id="genres" class="chips"></div>

</section>


<section
    id="results"
    class="section"
></section>


</main>


<div
    id="overlay"
    class="overlay"
    onclick="if(event.target===this)closeModal()"
>

<div
    id="modal"
    class="modal"
></div>

</div>


<script>

const tg =
    window.Telegram?.WebApp;

if(tg){
    tg.ready();
    tg.expand();
}


const $ =
    id => document.getElementById(id);


let movies = [];


function esc(s){

    return String(s || '').replace(
        /[&<>"]/g,
        m => ({
            '&':'&amp;',
            '<':'&lt;',
            '>':'&gt;',
            '"':'&quot;'
        }[m])
    );

}


function card(m){

    const poster =
        m.poster
        ? '/api/poster?id=' +
          encodeURIComponent(m.id)
        : '';

    return `
    <article
        class="card"
        onclick="detail('${encodeURIComponent(m.id)}')"
    >

        <img
            class="poster"
            src="${poster}"
            onerror="this.style.visibility='hidden'"
        >

        <div class="ct">

            <div class="title">
                ${esc(m.title)}
            </div>

            <div class="meta">
                ${esc(m.year || '')}
                ${esc(m.quality || '')}
            </div>

        </div>

    </article>
    `;

}


async function api(url){

    try{

        const response =
            await fetch(url, {
                cache:'no-store'
            });

        return await response.json();

    }catch(error){

        console.error(error);

        return {};

    }

}


async function loadLatest(){

    const data =
        await api('/api/latest?limit=20');

    movies =
        data.movies || [];

    $('rail').innerHTML =
        movies.length
        ? movies.map(card).join('')
        : '<div class="empty">No movies yet</div>';

}


async function searchMovies(){

    const query =
        $('q').value.trim();

    if(!query){
        return;
    }

    const data =
        await api(
            '/api/search?q=' +
            encodeURIComponent(query)
        );

    renderResults(
        'SEARCH RESULTS',
        data.movies || []
    );

}


function renderResults(
    title,
    arr
){

    $('results').innerHTML = `

        <div class="sectionhead">

            <h2>
                ${title}
            </h2>

            <span class="muted">
                ${arr.length} found
            </span>

        </div>

        <div class="rail">

            ${
                arr.length
                ? arr.map(card).join('')
                : '<div class="empty">Not found 😒</div>'
            }

        </div>
    `;

}


async function showGenres(){

    const data =
        await api('/api/genres');

    $('genres').innerHTML =
        (data.genres || [])
        .map(
            genre => `
            <button
                class="chip"
                onclick="genre('${encodeURIComponent(genre)}', this)"
            >
                ${esc(genre)}
            </button>
            `
        )
        .join('');

}


async function genre(
    encodedGenre,
    button
){

    document
        .querySelectorAll('.chip')
        .forEach(
            x => x.classList.remove('active')
        );

    if(button){
        button.classList.add('active');
    }

    const data =
        await api(
            '/api/genre?name=' +
            encodedGenre
        );

    renderResults(
        '🎭 ' +
        decodeURIComponent(encodedGenre),
        data.movies || []
    );

}


async function detail(
    encodedId
){

    const movieId =
        decodeURIComponent(encodedId);

    const data =
        await api(
            '/api/movie?id=' +
            encodeURIComponent(movieId)
        );

    if(!data.title){
        return;
    }

    const poster =
        data.poster
        ? '/api/poster?id=' +
          encodeURIComponent(data.id)
        : '';

    $('modal').innerHTML = `

        <button
            class="close"
            onclick="closeModal()"
        >
            ✕
        </button>

        ${
            poster
            ? `<img src="${poster}">`
            : ''
        }

        <div class="mb">

            <h2>
                ${esc(data.title)}
            </h2>

            <div class="meta">
                ${esc(data.year || '')}
                •
                ${esc(data.quality || '')}
                •
                ${esc(data.audio || '')}
            </div>

            <p>
                ${esc(
                    data.description
                    || 'Movie Myanmar'
                )}
            </p>

            <a
                href="${esc(data.link || '')}"
                target="_blank"
                rel="noopener"
            >

                <button class="open">
                    🎬 Open Movie
                </button>

            </a>

        </div>
    `;

    $('overlay').style.display =
        'flex';

}


function closeModal(){

    $('overlay').style.display =
        'none';

}


function openAi(){

    if(
        tg &&
        tg.sendData
    ){

        tg.sendData('ai');
        tg.close();

    }else{

        alert(
            'Open Xiao Yu AI from the Telegram bot menu 😝'
        );

    }

}


$('q').addEventListener(
    'keydown',
    e => {

        if(
            e.key === 'Enter'
        ){
            searchMovies();
        }

    }
);


loadLatest();
showGenres();

</script>

</body>
</html>
'''


# ============================================================
# MINI APP MOVIE SERIALIZER
# ============================================================

def mini_movie(movie):
    ensure_movie_id(movie)

    return {
        "id": str(
            movie.get(
                "id",
                "",
            )
        ),
        "title": movie.get(
            "title",
            "Movie",
        ),
        "year": movie.get(
            "year",
            "",
        ),
        "quality": movie.get(
            "quality",
            "",
        ),
        "audio": movie.get(
            "audio",
            "",
        ),
        "genres": movie.get(
            "genres",
            [],
        ),
        "description": movie.get(
            "description",
            "",
        ),
        "link": build_movie_link(
            movie
        ),
        "poster": bool(
            movie.get(
                "poster_file_id"
            )
        ),
    }


# ============================================================
# POSTER
# ============================================================

def get_poster_bytes(movie_id):
    movie = find_movie_by_id(
        movie_id
    )

    if not movie:
        return None, "image/jpeg"

    file_id = movie.get(
        "poster_file_id"
    )

    if not file_id:
        return None, "image/jpeg"

    if file_id in poster_cache:
        return poster_cache[file_id]

    try:

        request = Request(
            (
                "https://api.telegram.org/bot"
                + BOT_TOKEN
                + "/getFile?file_id="
                + file_id
            )
        )

        data = json.loads(
            urlopen(
                request,
                timeout=12,
            )
            .read()
            .decode()
        )

        path = (
            data
            .get("result", {})
            .get("file_path")
        )

        if not path:
            return None, "image/jpeg"

        raw = urlopen(
            (
                "https://api.telegram.org/file/bot"
                + BOT_TOKEN
                + "/"
                + path
            ),
            timeout=20,
        ).read()

        poster_cache[file_id] = (
            raw,
            "image/jpeg",
        )

        return poster_cache[file_id]

    except Exception as e:

        print(
            "poster error:",
            e,
        )

        return None, "image/jpeg"


# ============================================================
# HTTP SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        *args
    ):
        pass


    def send_bytes(
        self,
        data,
        content_type="text/plain; charset=utf-8",
        status=200,
    ):

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.send_header(
            "Content-Length",
            str(len(data)),
        )

        self.end_headers()

        self.wfile.write(data)


    def send_json(
        self,
        obj,
        status=200,
    ):

        self.send_bytes(
            json.dumps(
                obj,
                ensure_ascii=False,
            ).encode(),
            "application/json; charset=utf-8",
            status,
        )


    def do_GET(self):

        try:

            parsed =
                urlparse(self.path)

            path =
                parsed.path

            qs =
                parse_qs(
                    parsed.query
                )


            # ------------------------------------------------
            # HEALTH
            # ------------------------------------------------

            if path in (
                "/",
                "/health",
            ):

                return self.send_bytes(
                    b"Movie Myanmar Bot is alive"
                )


            # ------------------------------------------------
            # MINI APP
            # ------------------------------------------------

            if path in (
                "/app",
                "/miniapp",
            ):

                return self.send_bytes(
                    MINI_APP_HTML.encode(),
                    "text/html; charset=utf-8",
                )


            # ------------------------------------------------
            # ALL / LATEST MOVIES
            # ------------------------------------------------

            if path in (
                "/api/movies",
                "/api/latest",
            ):

                try:
                    limit =
                        int(
                            qs.get(
                                "limit",
                                ["20"],
                            )[0]
                        )
                except Exception:
                    limit = 20

                limit =
                    max(
                        1,
                        min(
                            limit,
                            50,
                        ),
                    )

                movies =
                    latest(limit)

                return self.send_json(
                    {
                        "movies": [
                            mini_movie(movie)
                            for movie in movies
                        ],
                        "count": len(
                            movies
                        ),
                        "total": len(
                            movie_database
                        ),
                    }
                )


            # ------------------------------------------------
            # SEARCH
            # ------------------------------------------------

            if path == "/api/search":

                query =
                    qs.get(
                        "q",
                        [""],
                    )[0]

                results =
                    movie_search(
                        query
                    )[:30]

                return self.send_json(
                    {
                        "movies": [
                            mini_movie(movie)
                            for movie in results
                        ],
                        "count": len(
                            results
                        ),
                    }
                )


            # ------------------------------------------------
            # GENRES
            # ------------------------------------------------

            if path == "/api/genres":

                return self.send_json(
                    {
                        "genres": list(
                            GENRE_KEYWORDS.keys()
                        )
                    }
                )


            # ------------------------------------------------
            # GENRE MOVIES
            # ------------------------------------------------

            if path == "/api/genre":

                genre =
                    qs.get(
                        "name",
                        [""],
                    )[0]

                if genre == "All":
                    results =
                        latest(50)

                else:
                    results = [
                        movie
                        for movie
                        in reversed(
                            movie_database
                        )
                        if genre
                        in movie.get(
                            "genres",
                            [],
                        )
                    ][:50]

                return self.send_json(
                    {
                        "movies": [
                            mini_movie(movie)
                            for movie in results
                        ],
                        "count": len(
                            results
                        ),
                    }
                )


            # ------------------------------------------------
            # SINGLE MOVIE
            # ------------------------------------------------

            if path == "/api/movie":

                movie_id =
                    qs.get(
                        "id",
                        [""],
                    )[0]

                movie =
                    find_movie_by_id(
                        movie_id
                    )

                return self.send_json(
                    mini_movie(movie)
                    if movie
                    else {}
                )


            # ------------------------------------------------
            # POSTER
            # ------------------------------------------------

            if path == "/api/poster":

                movie_id =
                    qs.get(
                        "id",
                        [""],
                    )[0]

                raw, content_type =
                    get_poster_bytes(
                        movie_id
                    )

                return self.send_bytes(
                    raw or b"",
                    content_type,
                    200 if raw else 404,
                )


            return self.send_bytes(
                b"Not Found",
                status=404,
            )


        except Exception:

            print(
                "GET error:"
            )

            print(
                traceback.format_exc()
            )

            return self.send_bytes(
                b"Server error",
                status=500,
            )


    def do_POST(self):

        global webhook_application
        global webhook_loop

        try:

            if (
                urlparse(
                    self.path
                ).path
                != WEBHOOK_PATH
            ):
                return self.send_bytes(
                    b"Not Found",
                    status=404,
                )


            if (
                self.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token"
                )
                != WEBHOOK_SECRET
            ):
                return self.send_bytes(
                    b"Forbidden",
                    status=403,
                )


            length =
                int(
                    self.headers.get(
                        "Content-Length",
                        "0",
                    )
                )

            body =
                self.rfile.read(
                    length
                )

            data =
                json.loads(
                    body.decode(
                        "utf-8"
                    )
                )


            update =
                Update.de_json(
                    data,
                    webhook_application.bot,
                )


            if (
                webhook_loop
                and webhook_application
            ):

                asyncio.run_coroutine_threadsafe(
                    webhook_application
                    .update_queue
                    .put(update),
                    webhook_loop,
                )


            return self.send_bytes(
                b"OK"
            )


        except Exception:

            print(
                "POST error:"
            )

            print(
                traceback.format_exc()
            )

            return self.send_bytes(
                b"Bad Request",
                status=400,
            )


# ============================================================
# HEALTH SERVER
# ============================================================

def start_health_server():

    server =
        HTTPServer(
            (
                "0.0.0.0",
                PORT,
            ),
            HealthHandler,
        )

    print(
        "HTTP server listening on",
        PORT,
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

async def main():

    global webhook_application
    global webhook_loop

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )


    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )


    # --------------------------------------------
    # HANDLERS
    # --------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            channel_post_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler,
        )
    )


    webhook_application = application

    webhook_loop =
        asyncio.get_running_loop()


    await application.initialize()

    await application.start()


    # --------------------------------------------
    # TELEGRAM MENU BUTTON
    # --------------------------------------------

    if (
        WebAppInfo
        and MenuButtonWebApp
    ):

        try:

            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🎬 Movie Myanmar",
                    web_app=WebAppInfo(
                        url=RENDER_URL
                        + "/app"
                    ),
                )
            )

        except Exception as e:

            print(
                "menu web app setup skipped:",
                e,
            )


    # --------------------------------------------
    # HEALTH SERVER
    # --------------------------------------------

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()


    # --------------------------------------------
    # WEBHOOK
    # --------------------------------------------

    await application.bot.set_webhook(
        url=(
            RENDER_URL
            + WEBHOOK_PATH
        ),
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message",
            "callback_query",
            "channel_post",
        ],
    )


    print(
        "========================================"
    )

    print(
        "Movie Myanmar Bot running"
    )

    print(
        "Mini App:",
        RENDER_URL + "/app",
    )

    print(
        "Movie DB:",
        MOVIE_DB_FILE,
    )

    print(
        "Movies:",
        len(movie_database),
    )

    print(
        "========================================"
    )


    while True:
        await asyncio.sleep(
            3600
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
