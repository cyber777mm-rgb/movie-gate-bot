import os
import asyncio
import hashlib
import json
import threading
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types

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


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MAIN_CHANNEL = "@moviemyanmar567"

MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"

RENDER_URL = "https://movie-gate-bot.onrender.com"
WEBHOOK_PATH = "/telegram-webhook"

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()


# ============================================================
# WELCOME IMAGE
# ============================================================

WELCOME_IMAGE = (
    "https://raw.githubusercontent.com/"
    "cyber777mm-rgb/movie-gate-bot/main/"
    "ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"
)


# ============================================================
# WEBHOOK SECRET
# ============================================================

WEBHOOK_SECRET = (
    hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
    if BOT_TOKEN
    else ""
)


# ============================================================
# GEMINI
# ============================================================

gemini_client = None
GEMINI_MODEL = "gemini-3.8-flash"

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini client initialized.", flush=True)
    except Exception as e:
        print(f"Gemini init error: {e}", flush=True)
        gemini_client = None


# ============================================================
# GLOBALS
# ============================================================

telegram_application = None
main_loop = None


# ============================================================
# MOVIE DATABASE
# ============================================================

MOVIE_DB_FILE = "movies.json"

movie_db_lock = threading.Lock()
movie_database = []


def load_movie_database():
    global movie_database

    try:
        if not os.path.exists(MOVIE_DB_FILE):
            movie_database = []
            print("movies.json not found. Starting empty movie database.", flush=True)
            return

        with open(MOVIE_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            movie_database = data
        else:
            movie_database = []

        print(
            f"Movie database loaded: {len(movie_database)} movies",
            flush=True,
        )

    except Exception as e:
        print(f"Movie database load error: {e}", flush=True)
        movie_database = []


def save_movie_database():
    try:
        with movie_db_lock:
            with open(MOVIE_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    movie_database,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

    except Exception as e:
        print(f"Movie database save error: {e}", flush=True)


def normalize_movie_name(text):
    if not text:
        return ""

    text = text.lower()

    text = re.sub(r"[^a-z0-9\u1000-\u109f]+", " ", text)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def movie_search(query):
    q = normalize_movie_name(query)

    if not q:
        return []

    results = []

    for movie in movie_database:
        title = normalize_movie_name(
            movie.get("title", "")
        )

        original_name = normalize_movie_name(
            movie.get("original_name", "")
        )

        combined = f"{title} {original_name}"

        if q in combined or combined.find(q) >= 0:
            results.append(movie)
            continue

        query_words = q.split()

        if query_words:
            matched = sum(
                1
                for word in query_words
                if word in combined
            )

            if matched >= max(1, len(query_words) // 2):
                results.append(movie)

    return results


# ============================================================
# MOVIE LINK
# ============================================================

def build_movie_link(chat_id, message_id):
    try:
        chat_id = int(chat_id)

        if str(chat_id).startswith("-100"):
            internal_id = str(chat_id)[4:]

            return (
                f"https://t.me/c/"
                f"{internal_id}/"
                f"{message_id}"
            )

    except Exception:
        pass

    return MOVIE_CHANNEL_URL


# ============================================================
# GENRE DETECTION
# ============================================================

GENRE_KEYWORDS = {
    "Action": [
        "action",
        "အက်ရှင်",
    ],
    "Comedy": [
        "comedy",
        "ဟာသ",
    ],
    "Romance": [
        "romance",
        "romantic",
        "အချစ်",
    ],
    "Horror": [
        "horror",
        "ကြောက်",
        "သရဲ",
    ],
    "Thriller": [
        "thriller",
    ],
    "Drama": [
        "drama",
        "ဒရာမာ",
    ],
    "Fantasy": [
        "fantasy",
    ],
}


# ============================================================
# MOVIE TITLE CLEANER
# ============================================================

def clean_movie_title(title):
    if not title:
        return ""

    title = re.sub(
        r"^[\W_🎬🔥⭐️]+",
        "",
        title,
        flags=re.UNICODE,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    return title.strip()


# ============================================================
# PARSE MOVIE POST
# ============================================================

def parse_movie_post(update):
    try:
        message = update.channel_post

        if not message:
            return None

        text = message.text or message.caption or ""

        if not text.strip():
            return None

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        if not lines:
            return None

        first_line = lines[0]

        title = clean_movie_title(first_line)

        year = ""

        year_match = re.search(
            r"\((19|20)\d{2}\)",
            first_line,
        )

        if year_match:
            year = year_match.group(0).strip("()")
        else:
            year_match = re.search(
                r"\b(19|20)\d{2}\b",
                first_line,
            )

            if year_match:
                year = year_match.group(0)

        quality = ""

        quality_match = re.search(
            r"\b(2160p|4K|1080p|720p|480p)\b",
            text,
            re.IGNORECASE,
        )

        if quality_match:
            quality = quality_match.group(1)

        audio = ""

        audio_patterns = [
            r"\bAAC2\.0\b",
            r"\bAAC\b",
            r"\bDDP\b",
            r"\b5\.1\b",
            r"\b2\.0\b",
            r"\bDual Audio\b",
            r"\bMyanmar\b",
            r"\bBurmese\b",
        ]

        for pattern in audio_patterns:
            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if match:
                audio = match.group(0)
                break

        original_name = ""

        original_match = re.search(
            r"(?:Original Name|Original|File Name)\s*[:\-]\s*(.+)",
            text,
            re.IGNORECASE,
        )

        if original_match:
            original_name = original_match.group(1).strip()

        genres = []

        lower_text = text.lower()

        for genre, keywords in GENRE_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in lower_text:
                    genres.append(genre)
                    break

        description = text

        movie_link = build_movie_link(
            message.chat_id,
            message.message_id,
        )

        movie = {
            "title": title,
            "year": year,
            "quality": quality,
            "audio": audio,
            "original_name": original_name,
            "genres": genres,
            "description": description,
            "post_link": movie_link,
            "chat_id": message.chat_id,
            "message_id": message.message_id,
        }

        return movie

    except Exception as e:
        print(
            f"Movie parse error: {e}",
            flush=True,
        )

        return None


# ============================================================
# SAVE MOVIE
# ============================================================

def save_movie(movie):
    global movie_database

    if not movie:
        return

    new_title = normalize_movie_name(
        movie.get("title", "")
    )

    new_year = str(
        movie.get("year", "")
    )

    new_chat_id = str(
        movie.get("chat_id", "")
    )

    new_message_id = str(
        movie.get("message_id", "")
    )

    with movie_db_lock:

        for existing in movie_database:

            same_message = (
                str(existing.get("chat_id", "")) == new_chat_id
                and
                str(existing.get("message_id", "")) == new_message_id
            )

            if same_message:
                return

            same_movie = (
                normalize_movie_name(
                    existing.get("title", "")
                ) == new_title
                and
                str(existing.get("year", "")) == new_year
            )

            if same_movie:
                existing.update(movie)
                save_movie_database()
                return

        movie_database.append(movie)

    save_movie_database()

    print(
        f"MOVIE SAVED: {movie.get('title')} {movie.get('year')}",
        flush=True,
    )


# ============================================================
# CHANNEL POST HANDLER
# ============================================================

async def channel_post_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "CHANNEL POST RECEIVED",
        flush=True,
    )

    movie = parse_movie_post(update)

    if movie:
        save_movie(movie)

        print(
            f"Movie detected: "
            f"{movie.get('title')} "
            f"{movie.get('year')}",
            flush=True,
        )


# ============================================================
# KEYBOARDS
# ============================================================

def movie_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 Movie Myanmar Channel",
                    url=MOVIE_CHANNEL_URL,
                )
            ]
        ]
    )


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url="https://t.me/moviemyanmar567",
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


def category_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 All Movies",
                    callback_data="category_all",
                )
            ],
            [
                InlineKeyboardButton(
                    "⚔️ Action",
                    callback_data="category_Action",
                ),
                InlineKeyboardButton(
                    "😂 Comedy",
                    callback_data="category_Comedy",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❤️ Romance",
                    callback_data="category_Romance",
                ),
                InlineKeyboardButton(
                    "👻 Horror",
                    callback_data="category_Horror",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔥 Thriller",
                    callback_data="category_Thriller",
                ),
                InlineKeyboardButton(
                    "🎭 Drama",
                    callback_data="category_Drama",
                ),
            ],
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def is_member(
    user_id,
    context,
):

    try:
        member = await context.bot.get_chat_member(
            chat_id=MAIN_CHANNEL,
            user_id=user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        print(
            f"Membership check error: {e}",
            flush=True,
        )

        return False


# ============================================================
# MAIN MENU
# ============================================================

async def show_main_menu(
    update,
    context,
):

    text = (
        "🎬 <b>Movie Myanmar</b>\n\n"
        "ဟယ်လို 👋\n"
        "ငါက <b>Xiao Yu CyB (小雨)</b> ပါ 😝❤️\n\n"
        "🎥 Movie ရှာလို့ရတယ်\n"
        "🎭 Genre နဲ့ရှာလို့ရတယ်\n"
        "💬 AI နဲ့လည်း စကားပြောလို့ရတယ်\n\n"
        "👇 အောက်က Menu ကိုရွေးပါ"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Search Movie",
                    callback_data="search_movie",
                ),
                InlineKeyboardButton(
                    "🎭 Categories",
                    callback_data="categories",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🤖 AI Chat",
                    callback_data="ai_chat",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Movie Channel",
                    url=MOVIE_CHANNEL_URL,
                )
            ],
        ]
    )

    if update.callback_query:

        await update.callback_query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ============================================================
# HEALTH / WEBHOOK SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
        ):

            body = b"Movie Myanmar Bot is alive."

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(body)

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        if self.path != WEBHOOK_PATH:

            self.send_response(404)
            self.end_headers()
            return

        try:

            secret = self.headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                "",
            )

            if secret != WEBHOOK_SECRET:

                self.send_response(403)
                self.end_headers()
                return

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            body = self.rfile.read(
                content_length
            )

            data = json.loads(
                body.decode("utf-8")
            )

            update = Update.de_json(
                data,
                telegram_application.bot,
            )

            asyncio.run_coroutine_threadsafe(
                telegram_application.update_queue.put(update),
                main_loop,
            )

            response = b"OK"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain",
            )

            self.send_header(
                "Content-Length",
                str(len(response)),
            )

            self.end_headers()

            self.wfile.write(response)

        except Exception as e:

            print(
                f"Webhook error: {e}",
                flush=True,
            )

            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_http_server():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        f"HTTP server started on port {port}",
        flush=True,
    )


# ============================================================
# BOT IDENTITY
# ============================================================

BOT_NAME = "Xiao Yu CyB (小雨)"

CREATOR_NAME = "CyB Lin Myat"

CREATOR_USERNAME = "@cyblinmyat777"


# ============================================================
# STICKERS
# ============================================================

SLAP_STICKER = (
    "CAACAgUAAxkBAAOKatIKZED6Z7w1Rc35vlMHSyCv8IQAAgYcAAJLd6lXJrpKjRqPHfM9BA"
)

RUDE_STICKER = (
    "CAACAgUAAxkBAAOLatIKrrZ5UGKd3ce5l5FbA1p8YO8AAtAXAALs76hX2aWY1l_EUxE9BA"
)

TONGUE_STICKER = (
    "CAACAgUAAxkBAAOVatIdX07G_2IAAWLl7AABRasK_UlJHwACWhgAAutFoFe_7dF5-2-ZLj0E"
)


# ============================================================
# AI SYSTEM
# ============================================================

SYSTEM_INSTRUCTION = """
You are Xiao Yu CyB (小雨), the playful AI girl of Movie Myanmar.

Creator:
CyB Lin Myat
@cyblinmyat777

Personality:
- Friendly
- Playful
- Slightly teasing
- Cute
- Sometimes grumpy
- Natural Burmese conversation
- Do not sound like a robot
- Keep normal replies reasonably short

Important:
Movie searching is handled by the Movie Myanmar movie database.
Do not pretend you searched a movie if the database did not return it.

If the user asks about a movie:
- Encourage them to search the Movie Myanmar movie database.
- If appropriate, tell them to send the movie name.

Do not use dangerous or self-harm language.
Do not tell users to die or hurt themselves.
"""


# ============================================================
# AI HISTORY
# ============================================================

user_histories = {}

MAX_HISTORY = 12


def normalize_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text.strip().lower(),
    )


def add_history(
    user_id,
    role,
    text,
):

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append(
        {
            "role": role,
            "text": text,
        }
    )

    user_histories[user_id] = (
        user_histories[user_id][-MAX_HISTORY:]
    )


# =============================================
