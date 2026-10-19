import os
import asyncio
import hashlib
import json
import threading
import re
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

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


# ============================================================
# STARTUP DIAGNOSTIC
# ============================================================

print("========================================", flush=True)
print("BOT.PY LOADED", flush=True)
print("Movie Myanmar Bot is starting...", flush=True)
print("Python version:", __import__("sys").version, flush=True)
print("========================================", flush=True)


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    "",
).strip()

MAIN_CHANNEL = "@moviemyanmar567"

MOVIE_CHANNEL_URL = (
    "https://t.me/+7EGkZEWlzfAyNGM1"
)

RENDER_URL = (
    "https://movie-gate-bot.onrender.com"
)

WEBHOOK_PATH = "/telegram-webhook"

ADMIN_CHAT_ID = os.getenv(
    "ADMIN_CHAT_ID",
    "8072749262",
).strip()

PORT = int(
    os.getenv(
        "PORT",
        "10000",
    )
)


# ============================================================
# AI FREE LIMIT
# ============================================================

FREE_AI_LIMIT = 20

AI_USAGE_FILE = "ai_usage.json"

ai_usage_lock = threading.Lock()

ai_usage = {}


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
    hashlib.sha256(
        BOT_TOKEN.encode()
    ).hexdigest()[:32]
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

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        print(
            "Gemini client initialized.",
            flush=True,
        )

    except Exception as e:

        print(
            f"Gemini init error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        gemini_client = None

else:

    print(
        "WARNING: GEMINI_API_KEY is not set.",
        flush=True,
    )


# ============================================================
# GLOBALS
# ============================================================

telegram_application = None

main_loop = None

http_server = None


# ============================================================
# MOVIE DATABASE
# ============================================================

MOVIE_DB_FILE = "movies.json"

movie_db_lock = threading.Lock()

movie_database = []


def load_movie_database():

    global movie_database

    try:

        if not os.path.exists(
            MOVIE_DB_FILE
        ):

            movie_database = []

            print(
                "movies.json not found. "
                "Starting empty database.",
                flush=True,
            )

            return

        with open(
            MOVIE_DB_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            list,
        ):

            movie_database = data

        else:

            movie_database = []

        print(
            f"Movie database loaded: "
            f"{len(movie_database)} movies",
            flush=True,
        )

    except Exception as e:

        print(
            f"Movie database load error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        movie_database = []


def save_movie_database():

    try:

        with movie_db_lock:

            temp_file = (
                MOVIE_DB_FILE
                + ".tmp"
            )

            with open(
                temp_file,
                "w",
                encoding="utf-8",
            ) as f:

                json.dump(
                    movie_database,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            os.replace(
                temp_file,
                MOVIE_DB_FILE,
            )

    except Exception as e:

        print(
            f"Movie database save error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


# ============================================================
# AI USAGE DATABASE
# ============================================================

def load_ai_usage():

    global ai_usage

    try:

        if not os.path.exists(
            AI_USAGE_FILE
        ):

            ai_usage = {}

            print(
                "ai_usage.json not found. "
                "Starting AI usage database.",
                flush=True,
            )

            return

        with open(
            AI_USAGE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            dict,
        ):

            ai_usage = data

        else:

            ai_usage = {}

        print(
            f"AI usage database loaded: "
            f"{len(ai_usage)} users",
            flush=True,
        )

    except Exception as e:

        print(
            f"AI usage load error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        ai_usage = {}


def save_ai_usage():

    try:

        # Copy data while holding the lock.
        # File writing happens after the lock is released.
        with ai_usage_lock:

            data_to_save = dict(
                ai_usage
            )

        temp_file = (
            AI_USAGE_FILE
            + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data_to_save,
                f,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            temp_file,
            AI_USAGE_FILE,
        )

    except Exception as e:

        print(
            f"AI usage save error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


def get_ai_usage(
    user_id,
):

    key = str(user_id)

    with ai_usage_lock:

        value = ai_usage.get(
            key,
            0,
        )

        try:

            return int(value)

        except Exception:

            return 0


def increment_ai_usage(
    user_id,
):

    key = str(user_id)

    with ai_usage_lock:

        current = ai_usage.get(
            key,
            0,
        )

        try:

            current = int(current)

        except Exception:

            current = 0

        new_count = current + 1

        ai_usage[key] = new_count

    # IMPORTANT:
    # Save AFTER releasing ai_usage_lock.
    save_ai_usage()

    print(
        f"AI usage: user={user_id} "
        f"count={new_count}/"
        f"{FREE_AI_LIMIT}",
        flush=True,
    )

    return new_count


# ============================================================
# MOVIE NORMALIZATION
# ============================================================

def normalize_movie_name(
    text,
):

    if not text:
        return ""

    text = text.lower()

    text = re.sub(
        r"[^a-z0-9\u1000-\u109f]+",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# MOVIE SEARCH
# ============================================================

def movie_search(
    query,
):

    q = normalize_movie_name(
        query
    )

    if not q:
        return []

    results = []

    query_words = q.split()

    for movie in movie_database:

        title = normalize_movie_name(
            movie.get(
                "title",
                "",
            )
        )

        original_name = normalize_movie_name(
            movie.get(
                "original_name",
                "",
            )
        )

        combined = (
            f"{title} "
            f"{original_name}"
        ).strip()

        if not combined:
            continue

        # Exact substring match
        if q in combined:

            results.append(
                movie
            )

            continue

        # Word match
        matched = sum(
            1
            for word in query_words
            if word
            and word in combined
        )

        if matched >= max(
            1,
            len(query_words) // 2,
        ):

            results.append(
                movie
            )

    return results


# ============================================================
# MOVIE LINK
# ============================================================

def build_movie_link(
    chat_id,
    message_id,
):

    try:

        chat_id = int(
            chat_id
        )

        if str(
            chat_id
        ).startswith("-100"):

            internal_id = str(
                chat_id
            )[4:]

            return (
                "https://t.me/c/"
                f"{internal_id}/"
                f"{message_id}"
            )

    except Exception:

        pass

    return MOVIE_CHANNEL_URL


# ============================================================
# GENRE
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

def clean_movie_title(
    title,
):

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

def parse_movie_post(
    update,
):

    try:

        message = (
            update.channel_post
        )

        if not message:
            return None

        text = (
            message.text
            or message.caption
            or ""
        )

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

        first_line = first_line.replace(
            "**",
            "",
        )

        title = clean_movie_title(
            first_line
        )

        title = re.sub(
            r"^[🎬🔥⭐️⚡️]+\s*",
            "",
            title,
        ).strip()

        year = ""

        year_match = re.search(
            r"\((19|20)\d{2}\)",
            first_line,
        )

        if year_match:

            year = (
                year_match
                .group(0)
                .strip("()")
            )

        else:

            year_match = re.search(
                r"\b(19|20)\d{2}\b",
                first_line,
            )

            if year_match:

                year = (
                    year_match
                    .group(0)
                )

        quality = ""

        quality_match = re.search(
            r"\b(2160p|4K|1080p|720p|480p)\b",
            text,
            re.IGNORECASE,
        )

        if quality_match:

            quality = (
                quality_match
                .group(1)
            )

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

                audio = (
                    match.group(0)
                )

                break

        original_name = ""

        original_match = re.search(
            r"(?:Original Name|Original|File Name)"
            r"\s*[:\-]\s*(.+)",
            text,
            re.IGNORECASE,
        )

        if original_match:

            original_name = (
                original_match
                .group(1)
                .strip()
            )

        genres = []

        lower_text = text.lower()

        for genre, keywords in (
            GENRE_KEYWORDS.items()
        ):

            for keyword in keywords:

                if (
                    keyword.lower()
                    in lower_text
                ):

                    genres.append(
                        genre
                    )

                    break

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

            "description": text,

            "post_link": movie_link,

            "chat_id": message.chat_id,

            "message_id": message.message_id,

        }

        return movie

    except Exception as e:

        print(
            f"Movie parse error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        return None


# ============================================================
# SAVE MOVIE
# ============================================================

def save_movie(
    movie,
):

    global movie_database

    if not movie:
        return

    new_title = normalize_movie_name(
        movie.get(
            "title",
            "",
        )
    )

    new_year = str(
        movie.get(
            "year",
            "",
        )
    )

    new_chat_id = str(
        movie.get(
            "chat_id",
            "",
        )
    )

    new_message_id = str(
        movie.get(
            "message_id",
            "",
        )
    )

    should_save = False

    with movie_db_lock:

        for existing in movie_database:

            same_message = (
                str(
                    existing.get(
                        "chat_id",
                        "",
                    )
                )
                == new_chat_id
                and
                str(
                    existing.get(
                        "message_id",
                        "",
                    )
                )
                == new_message_id
            )

            if same_message:
                return

            same_movie = (
                normalize_movie_name(
                    existing.get(
                        "title",
                        "",
                    )
                )
                == new_title
                and
                str(
                    existing.get(
                        "year",
                        "",
                    )
                )
                == new_year
            )

            if same_movie:

                existing.update(
                    movie
                )

                should_save = True

                break

        else:

            movie_database.append(
                movie
            )

            should_save = True

    if should_save:

        save_movie_database()

        print(
            f"MOVIE SAVED: "
            f"{movie.get('title')} "
            f"{movie.get('year')}",
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

    movie = parse_movie_post(
        update
    )

    if movie:

        save_movie(
            movie
        )

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
                    url=(
                        "https://t.me/"
                        "moviemyanmar567"
                    ),
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

            [
                InlineKeyboardButton(
                    "✨ Fantasy",
                    callback_data="category_Fantasy",
                )
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

        member = (
            await context.bot.get_chat_member(
                chat_id=MAIN_CHANNEL,
                user_id=user_id,
            )
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        print(
            f"Membership check error: "
            f"{type(e).__name__}: {e}",
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

    elif update.message:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ============================================================
# HEALTH / WEBHOOK SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

        if self.path in (
            "/",
            "/health",
        ):

            body = (
                b"Movie Myanmar Bot is alive."
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; "
                "charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(
                body
            )

            return

        self.send_response(
            404
        )

        self.end_headers()

    def do_POST(
        self
    ):

        if self.path != WEBHOOK_PATH:

            self.send_response(
                404
            )

            self.end_headers()

            return

        try:

            secret = (
                self.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token",
                    "",
                )
            )

            if secret != WEBHOOK_SECRET:

                self.send_response(
                    403
                )

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
                body.decode(
                    "utf-8"
                )
            )

            update = Update.de_json(
                data,
                telegram_application.bot,
            )

            asyncio.run_coroutine_threadsafe(
                telegram_application
                .update_queue
                .put(update),
                main_loop,
            )

            response = b"OK"

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain",
            )

            self.send_header(
                "Content-Length",
                str(len(response)),
            )

            self.end_headers()

            self.wfile.write(
                response
            )

        except Exception as e:

            print(
                f"Webhook error: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

            traceback.print_exc()

            self.send_response(
                500
            )

            self.end_headers()

    def log_message(
        self,
        format,
        *args,
    ):

        return


def start_http_server():

    global http_server

    http_server = HTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        HealthHandler,
    )

    thread = threading.Thread(
        target=http_server.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        f"HTTP server started on port {PORT}",
        flush=True,
    )


# ============================================================
# BOT IDENTITY
# ============================================================

BOT_NAME = (
    "Xiao Yu CyB (小雨)"
)

CREATOR_NAME = (
    "CyB Lin Myat"
)

CREATOR_USERNAME = (
    "@cyblinmyat777"
)


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
- Cute
- Slightly teasing
- Sometimes grumpy
- Natural Burmese conversation
- Do not sound like a robot
- Keep replies reasonably short

Important:
Movie searching is handled by the Movie Myanmar movie database.

Do not pretend you searched a movie if the database did not return it.

If the user asks about a movie:
- Encourage them to search Movie Myanmar.
- Ask them to send the movie name if needed.

Never use dangerous or self-harm language.
Never tell users to die or hurt themselves.
"""


# ============================================================
# AI HISTORY
# ============================================================

user_histories = {}

MAX_HISTORY = 12


def normalize_text(
    text,
):

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
        user_histories[user_id][
            -MAX_HISTORY:
        ]
    )


# ============================================================
# SEND LONG TEXT
# ============================================================

async def send_long_text(
    message,
    text,
):

    if not text:
        return

    max_length = 3900

    while len(text) > max_length:

        cut = text.rfind(
            "\n",
            0,
            max_length,
        )

        if cut < 1000:

            cut = max_length

        await message.reply_text(
            text[:cut]
        )

        text = (
            text[cut:]
            .lstrip()
        )

    if text:

        await message.reply_text(
            text
        )


# ============================================================
# ADMIN MOVIE REQUEST
# ============================================================

async def send_movie_request(
    message,
    query,
):

    if not ADMIN_CHAT_ID:

        print(
            f"ADMIN_CHAT_ID not configured. "
            f"Missing movie: {query}",
            flush=True,
        )

        return

    try:

        user = (
            message.from_user
        )

        username = (
            f"@{user.username}"
            if user
            and user.username
            else "No username"
        )

        user_id = (
            user.id
            if user
            else "Unknown"
        )

        admin_text = (
            "🔔 <b>Movie Request</b>\n\n"
            f"🎬 Requested: "
            f"<b>{query}</b>\n"
            f"👤 User: {username}\n"
            f"🆔 ID: "
            f"<code>{user_id}</code>"
        )

        await message.get_bot().send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_text,
            parse_mode="HTML",
        )

        print(
            f"Movie request sent to admin: "
            f"{query}",
            flush=True,
        )

    except Exception as e:

        print(
            f"Admin request error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


# ============================================================
# MOVIE SEARCH RESPONSE
# ============================================================

async def reply_movie_search(
    message,
    query,
    context=None,
):

    results = movie_search(
        query
    )

    if not results:

        await send_movie_request(
            message,
            query,
        )

        if context is not None:

            context.user_data[
                "last_movie_query"
            ] = query

        await message.reply_text(
            f"😒 <b>{query}</b> ကို "
            "Movie Myanmar ထဲမှာ "
            "မတွေ့သေးဘူးဟာ။\n\n"
            "ဟွန့်... ငါလည်း "
            "ရှာပေးထားပြီ 😝\n"
            "ကိုကို လင်းမြတ်ဆီကို "
            "request ပို့ထားတယ်။\n\n"
            "ရှိလာရင် ပြန်ထည့်ပေးမယ် "
            "🎬❤️",
            parse_mode="HTML",
        )

        return False

    if context is not None:

        context.user_data[
            "last_movie_query"
        ] = query

    await message.reply_text(
        f"🔎 <b>{query}</b>\n\n"
        f"🎬 Movie Myanmar ထဲမှာ "
        f"<b>{len(results)}</b> "
        "ကားတွေ့တယ်နော် ❤️\n\n"
        "ကြည့်ချင်တဲ့ကားကို "
        "နှိပ်လိုက် 👇",
        parse_mode="HTML",
    )

    buttons = []

    for movie in results[:15]:

        title = movie.get(
            "title",
            "Unknown Movie",
        )

        year = movie.get(
            "year",
            "",
        )

        label = (
            f"🎬 {title}"
        )

        if year:

            label += (
                f" ({year})"
            )

        link = movie.get(
            "post_link",
            MOVIE_CHANNEL_URL,
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    label[:64],
                    url=link,
                )
            ]
        )

    if buttons:

        await message.reply_text(
            "👇 ဇာတ်ကားရွေးပါ",
            reply_markup=(
                InlineKeyboardMarkup(
                    buttons
                )
            ),
        )

    return True


# ============================================================
# CATEGORY
# ============================================================

async def reply_category(
    message,
    category,
):

    if category == "all":

        results = (
            movie_database[:30]
        )

    else:

        results = [

            movie

            for movie
            in movie_database

            if category
            in movie.get(
                "genres",
                [],
            )

        ][:30]

    if not results:

        await message.reply_text(
            f"😅 {category} ထဲမှာ "
            "ကားမတွေ့သေးဘူးဟာ။"
        )

        return

    buttons = []

    for movie in results:

        title = movie.get(
            "title",
            "Unknown",
        )

        year = movie.get(
            "year",
            "",
        )

        label = (
            f"🎬 {title}"
        )

        if year:

            label += (
                f" ({year})"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    label[:64],
                    url=movie.get(
                        "post_link",
                        MOVIE_CHANNEL_URL,
                    ),
                )
            ]
        )

    await message.reply_text(
        f"🎭 <b>{category}</b>\n\n"
        f"{len(results)} ကားတွေ့တယ် 👇",
        parse_mode="HTML",
        reply_markup=(
            InlineKeyboardMarkup(
                buttons
            )
        ),
    )


# ============================================================
# SPECIAL REPLIES
# ============================================================

async def handle_special_reply(
    message,
    text,
):

    normalized = normalize_text(
        text
    )

    if normalized in (
        "ဟိုင်း",
        "ဟေး",
        "hi",
        "hello",
        "hey",
    ):

        await message.reply_text(
            "ဟိုင်း 😝❤️\n"
            "ဘာကားရှာပေးရမလဲ "
            "သယ်ချင်း 🎬"
        )

        return True

    if (
        "နာမည်" in normalized
        or "who are you" in normalized
    ):

        await message.reply_text(
            f"ငါက <b>{BOT_NAME}</b> "
            "ပါ 😝❤️\n\n"
            f"ဖန်တီးသူက "
            f"<b>{CREATOR_NAME}</b> "
            f"{CREATOR_USERNAME} ပါ။",
            parse_mode="HTML",
        )

        return True

    if (
        "ကျေးဇူး" in normalized
        or "thanks" in normalized
        or "thank you" in normalized
    ):

        await message.reply_text(
            "အေးပါဟာ 😝❤️\n"
            "ကားကြည့်ချင်ရင် "
            "နာမည်ပဲပြော 🎬"
        )

        return True

    return False


# ============================================================
# GEMINI
# ============================================================

async def gemini_reply(
    user_id,
    text,
):

    if not gemini_client:

        return (
            "ဟွန့် 😒 Gemini မရသေးဘူးဟာ။\n"
            "နင်ကြည့်ချင်တဲ့ကားနာမည်ပဲ "
            "ပြော၊ ငါ Movie Myanmar ထဲမှာ "
            "ရှာပေးမယ် 🎬😝",
            False,
        )

    add_history(
        user_id,
        "user",
        text,
    )

    history = user_histories.get(
        user_id,
        [],
    )

    conversation = []

    for item in history:

        conversation.append(
            f"{item['role']}: "
            f"{item['text']}"
        )

    prompt = (
        SYSTEM_INSTRUCTION
        + "\n\nConversation:\n"
        + "\n".join(
            conversation
        )
        + "\n\nReply naturally "
          "in Burmese."
    )

    try:

        response = await asyncio.to_thread(
            gemini_client
            .models
            .generate_content,
            model=GEMINI_MODEL,
            contents=prompt,
        )

        reply = getattr(
            response,
            "text",
            None,
        )

        if not reply:

            reply = (
                "ဟွန့် 😒 ဘာပြောရမလဲတောင် "
                "ငါမသိတော့ဘူး 😂\n"
                "ကားနာမည်ပဲ ပြောဟာ 🎬"
            )

        add_history(
            user_id,
            "assistant",
            reply,
        )

        return (
            reply,
            True,
        )

    except Exception as e:

        print(
            f"Gemini error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        return (
            "ဟွန့် 😒 AI က ခဏလေး "
            "အနားယူနေတယ်။\n"
            "နင်ကြည့်ချင်တဲ့ကားနာမည်ပဲ "
            "ပြော၊ ငါ Movie Myanmar ထဲမှာ "
            "ရှာပေးမယ် 🎬😝",
            False,
        )


# ============================================================
# FALLBACK
# ============================================================

async def movie_fallback(
    message,
):

    await message.reply_text(
        "ဟွန့် 😒 အခုတော့ ငါနဲ့ "
        "စကားအများကြီး မပြောနဲ့တော့ဟာ 😂\n\n"
        "နင်ကြည့်ချင်တဲ့ကားနာမည်ပဲ ပြော၊ "
        "ငါ Movie Myanmar ထဲမှာ "
        "ရှာပေးမယ် 🎬😝"
    )


# ============================================================
# CONVERSATION / AI DETECTION
# ============================================================

CONVERSATION_WORDS = [

    "စကားပြော",
    "စကားပြောမယ်",
    "စကားပြောချင်",
    "စကားပြောချင်တယ်",
    "စကားပြောချင်လို့",
    "စကားပြောမယ်လေ",
    "စကားပြောကြမယ်",
    "စကားပြောရအောင်",

    "နင်နဲ့စကားပြော",
    "နင်နဲ့ စကားပြော",
    "နင်နဲ့ပြော",
    "နင်နဲ့ပြောမယ်",

    "မင်းနဲ့စကားပြော",
    "မင်းနဲ့ စကားပြော",
    "မင်းနဲ့ပြော",
    "မင်းနဲ့ပြောမယ်",

    "ငါနဲ့စကားပြော",
    "ငါနဲ့ စကားပြော",
    "ငါနဲ့ပြော",
    "ငါနဲ့ပြောမယ်",

    "ဘာလုပ်နေလဲ",
    "ဘာလုပ်နေတာလဲ",
    "ဘာလုပ်နေလို့လဲ",

    "ဘာဖြစ်နေလဲ",
    "ဘာဖြစ်နေတာလဲ",

    "နေကောင်းလား",
    "ဘယ်လိုနေလဲ",
    "အိပ်သေးလား",

    "ကောင်မလေး",
    "တိနေ့ဘာလုပ်",
    "ဒီနေ့ဘာလုပ်",
    "ဘာလုပ်မလဲ",

    "ဟိတ်",
    "ဟိတ်ဟာ",
    "ဟေ့",
    "ဟေး",

]


AI_WORDS = [

    "ai",
    "xiao",
    "小雨",
    "hello",
    "hi",
    "hey",

]


def is_ai_request(
    text,
):

    normalized = normalize_text(
        text
    )

    if any(
        word in normalized
        for word in CONVERSATION_WORDS
    ):

        return True

    if any(
        word in normalized
        for word in AI_WORDS
    ):

        return True

    return False


# ============================================================
# FOLLOW-UP MOVIE REQUEST DETECTION
# ============================================================

FOLLOWUP_MOVIE_WORDS = [

    "ရှာပေး",
    "ရှာပေးပါ",
    "ပေး",
    "ရပြီလား",
    "ရပြီ",
    "အေး",
    "အေးပေး",
    "အေး ရှာပေး",
    "ဟုတ်",
    "ဟုတ်ကဲ့",
    "ရမလား",
    "ကြည့်မယ်",
    "ကြည့်ချင်တယ်",

]


def is_movie_followup(
    text,
):

    normalized = normalize_text(
        text
    )

    return any(
        word in normalized
        for word in FOLLOWUP_MOVIE_WORDS
    )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = (
        update.effective_user
    )

    if not user:
        return

    # Reset only current interaction mode.
    context.user_data[
        "mode"
    ] = ""

    joined = await is_member(
        user.id,
        context,
    )

    if not joined:

        await update.message.reply_text(
            "🎬 <b>Movie Myanmar</b>\n\n"
            "ဟယ်လို 👋❤️\n"
            "Bot ကိုအသုံးပြုဖို့ "
            "<b>Movie Myanmar Channel</b> "
            "ကို အရင် Join လုပ်ပေးရမယ်နော်။\n\n"
            "👇 Join ပြီးရင် "
            "<b>I Joined</b> ကိုနှိပ်ပါ။",
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

        return

    try:

        await update.message.reply_photo(
            photo=WELCOME_IMAGE,
            caption=(
                "🎬 <b>Movie Myanmar</b>\n\n"
                "ဟယ်လို 👋❤️\n"
                "ငါက "
                "<b>Xiao Yu CyB (小雨)</b> "
                "ပါ 😝\n\n"
                "ကားရှာချင်ရင် Menu ကနေ "
                "<b>Search Movie</b> ကို "
                "နှိပ်နော် 🎥"
            ),
            parse_mode="HTML",
        )

    except Exception as e:

        print(
            f"Welcome image error: {e}",
            flush=True,
        )

    await show_main_menu(
        update,
        context,
    )


# ============================================================
# RESET
# ============================================================

async def reset_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = (
        update.effective_user
    )

    if user:

        user_histories.pop(
            user.id,
            None,
        )

    context.user_data[
        "mode"
    ] = ""

    context.user_data.pop(
        "last_movie_query",
        None,
    )

    await update.message.reply_text(
        "🔄 Chat history reset "
        "လုပ်ပြီးပြီ 😝\n"
        "AI quota ကိုတော့ မreset လုပ်ဘူးနော်။\n"
        "ပြန်စကြမယ် 🎬❤️"
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = (
        update.callback_query
    )

    await query.answer()

    user = query.from_user

    # --------------------------------------------------------
    # CHECK JOIN
    # --------------------------------------------------------

    if query.data == "check_join":

        joined = await is_member(
            user.id,
            context,
        )

        if not joined:

            await query.message.reply_text(
                "😅 နင် Join မထားသေးဘူးဟာ။\n\n"
                "Channel ကို Join ပြီးမှ "
                "I Joined ပြန်နှိပ်နော် 👇",
                reply_markup=join_keyboard(),
            )

            return

        await query.message.reply_text(
            "✅ Join ထားတာ "
            "အတည်ပြုပြီးပြီ 😝❤️"
        )

        await show_main_menu(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # MEMBERSHIP CHECK
    # --------------------------------------------------------

    joined = await is_member(
        user.id,
        context,
    )

    if not joined:

        await query.message.reply_text(
            "⚠️ အရင် Channel Join "
            "လုပ်ပါနော်။",
            reply_markup=join_keyboard(),
        )

        return

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    if query.data == "search_movie":

        context.user_data[
            "mode"
        ] = "search"

        await query.message.reply_text(
            "🔎 <b>Movie Search</b>\n\n"
            "ရှာချင်တဲ့ကားနာမည်ကို "
            "ရိုက်ပို့လိုက် 🎬❤️",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # AI
    # --------------------------------------------------------

    if query.data == "ai_chat":

        context.user_data[
            "mode"
        ] = "ai"

        count = get_ai_usage(
            user.id
        )

        if count >= FREE_AI_LIMIT:

            await query.message.reply_text(
                "ဟွန့် 😒\n"
                "နင်နဲ့ AI စကားပြောတာ "
                "free 20 ချက်ပြည့်သွားပြီဟာ 😂\n\n"
                "ကားနာမည်ပဲ ပြော၊ "
                "ငါ Movie Myanmar ထဲမှာ "
                "ရှာပေးမယ် 🎬😝"
            )

            return

        remaining = (
            FREE_AI_LIMIT
            - count
        )

        await query.message.reply_text(
            "🤖 <b>Xiao Yu AI</b>\n\n"
            "ပြောလေ 😝❤️\n"
            "ငါနားထောင်နေတယ်။\n\n"
            f"🎟️ Free AI ကျန်တာ "
            f"<b>{remaining}</b> ချက်",
            parse_mode="HTML",
        )

        return

    # --------------------------------------------------------
    # CATEGORIES
    # --------------------------------------------------------

    if query.data == "categories":

        await query.message.reply_text(
            "🎭 <b>Movie Categories</b>\n\n"
            "ကြည့်ချင်တဲ့ Genre ရွေးပါ 👇",
            parse_mode="HTML",
            reply_markup=category_keyboard(),
        )

        return

    # --------------------------------------------------------
    # CATEGORY RESULT
    # --------------------------------------------------------

    if query.data.startswith(
        "category_"
    ):

        category = (
            query.data.replace(
                "category_",
                "",
                1,
            )
        )

        await reply_category(
            query.message,
            category,
        )

        return


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    user = (
        update.effective_user
    )

    if not user:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    # --------------------------------------------------------
    # MEMBERSHIP GATE
    # --------------------------------------------------------

    joined = await is_member(
        user.id,
        context,
    )

    if not joined:

        await update.message.reply_text(
            "🔒 <b>Movie Myanmar</b>\n\n"
            "Bot ကိုအသုံးပြုဖို့ "
            "Channel ကိုအရင် Join "
            "လုပ်ရမယ်နော် 😝\n\n"
            "Join ပြီးရင် အောက်က "
            "<b>I Joined</b> ကိုနှိပ်ပါ 👇",
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

        return

    mode = context.user_data.get(
        "mode",
        "",
    )

    normalized = normalize_text(
        text
    )

    # --------------------------------------------------------
    # SEARCH MODE
    # --------------------------------------------------------

    if mode == "search":

        context.user_data[
            "mode"
        ] = ""

        await reply_movie_search(
            update.message,
            text,
            context,
        )

        return

    # --------------------------------------------------------
    # MOVIE DATABASE SEARCH FIRST
    #
    # IMPORTANT:
    # This runs BEFORE AI/quota.
    # Therefore AI exhaustion cannot break movie search.
    # --------------------------------------------------------

    results = movie_search(
        text
    )

    if results:

        context.user_data[
            "mode"
        ] = ""

        await reply_movie_search(
            update.message,
            text,
            context,
        )

        return

    # --------------------------------------------------------
    # FOLLOW-UP TO LAST MOVIE
    #
    # Example:
    # User: By Any Means
    # Bot: found movie
    # User: အေး ရှာပေး
    #
    # Search the previous movie again.
    # --------------------------------------------------------

    last_movie_query = (
        context.user_data.get(
            "last_movie_query",
            "",
        )
    )

    if (
        last_movie_query
        and is_movie_followup(text)
    ):

        followup_results = movie_search(
            last_movie_query
        )

        if followup_results:

            context.user_data[
                "mode"
            ] = ""

            await reply_movie_search(
                update.message,
                last_movie_query,
                context,
            )

            return

    # --------------------------------------------------------
    # MOVIE REQUEST DETECTION
    # --------------------------------------------------------

    movie_request_words = [

        "ရှိလား",
        "ရှိမလား",
        "ရှာပေး",
        "ရှာပေးပါ",
        "ကားရှာ",
        "ဇာတ်ကားရှာ",
        "movieရှာ",
        "movie ရှာ",
        "filmရှာ",
        "film ရှာ",
        "ကြည့်ချင်",
        "ကြည့်မယ်",
        "ကားလိုချင်",
        "ကားပေး",
        "movie",
        "film",

    ]

    has_movie_word = any(
        word in normalized
        for word in movie_request_words
    )

    has_year = bool(
        re.search(
            r"\b(19|20)\d{2}\b",
            text,
        )
    )

    looks_like_movie_title = (
        has_year
        or has_movie_word
    )

    if looks_like_movie_title:

        context.user_data[
            "mode"
        ] = ""

        await reply_movie_search(
            update.message,
            text,
            context,
        )

        return

    # --------------------------------------------------------
    # SPECIAL REPLIES
    # --------------------------------------------------------

    handled = (
        await handle_special_reply(
            update.message,
            text,
        )
    )

    if handled:
        return

    # --------------------------------------------------------
    # CONVERSATION / AI
    # --------------------------------------------------------

    ai_request = (
        mode == "ai"
        or is_ai_request(text)
    )

    if ai_request:

        context.user_data[
            "mode"
        ] = "ai"

        current_count = (
            get_ai_usage(
                user.id
            )
        )

        # ----------------------------------------------------
        # FREE 20 EXHAUSTED
        #
        # ONLY AI conversation stops here.
        # Movie search above is still available.
        # ----------------------------------------------------

        if (
            current_count
            >= FREE_AI_LIMIT
        ):

            await movie_fallback(
                update.message
            )

            return

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        reply, used_ai = (
            await gemini_reply(
                user.id,
                text,
            )
        )

        # ----------------------------------------------------
        # ONLY COUNT REAL GEMINI SUCCESS
        # ----------------------------------------------------

        if used_ai:

            new_count = (
                increment_ai_usage(
                    user.id
                )
            )

            remaining = max(
                0,
                FREE_AI_LIMIT
                - new_count,
            )

            # Don't add quota text to every AI response.
            # It is only logged server-side.

            print(
                f"AI remaining for "
                f"user={user.id}: "
                f"{remaining}",
                flush=True,
            )

        await send_long_text(
            update.message,
            reply,
        )

        return

    # --------------------------------------------------------
    # DEFAULT
    # --------------------------------------------------------

    await update.message.reply_text(
        "😝 ဘာလုပ်ပေးရမလဲ သယ်ချင်း?\n\n"
        "🎬 Movie ရှာချင်ရင် "
        "ကားနာမည်ပို့\n"
        "💬 ငါနဲ့စကားပြောချင်ရင် "
        "\"နင်နဲ့စကားပြောမယ်\" လို့ပြော 😏"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "================ BOT ERROR ================",
        flush=True,
    )

    print(
        f"Error type: "
        f"{type(context.error).__name__}",
        flush=True,
    )

    print(
        f"Error: {context.error}",
        flush=True,
    )

    traceback.print_exception(
        type(context.error),
        context.error,
        context.error.__traceback__,
    )

    print(
        "============================================",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    global telegram_application

    global main_loop

    print(
        "Movie Myanmar Bot starting...",
        flush=True,
    )

    # --------------------------------------------------------
    # TOKEN CHECK
    # --------------------------------------------------------

    if not BOT_TOKEN:

        print(
            "FATAL ERROR: "
            "BOT_TOKEN is missing.",
            flush=True,
        )

        raise RuntimeError(
            "BOT_TOKEN environment "
            "variable is missing."
        )

    print(
        "BOT_TOKEN detected.",
        flush=True,
    )

    if not GEMINI_API_KEY:

        print(
            "WARNING: GEMINI_API_KEY is missing. "
            "AI chat will use fallback.",
            flush=True,
        )

    # --------------------------------------------------------
    # LOAD DATABASES
    # --------------------------------------------------------

    load_movie_database()

    load_ai_usage()

    # --------------------------------------------------------
    # EVENT LOOP
    # --------------------------------------------------------

    main_loop = (
        asyncio.get_running_loop()
    )

    print(
        "Main event loop ready.",
        flush=True,
    )

    # --------------------------------------------------------
    # BUILD APPLICATION
    # --------------------------------------------------------

    try:

        telegram_application = (
            Application.builder()
            .token(BOT_TOKEN)
            .updater(None)
            .build()
        )

        print(
            "Telegram Application built.",
            flush=True,
        )

    except Exception as e:

        print(
            "FATAL ERROR while building "
            "Telegram Application:",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        raise

    # --------------------------------------------------------
    # HANDLERS
    # --------------------------------------------------------

    telegram_application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    telegram_application.add_handler(
        CommandHandler(
            "reset",
            reset_command,
        )
    )

    telegram_application.add_handler(
        CallbackQueryHandler(
            button_handler,
        )
    )

    telegram_application.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL,
            channel_post_handler,
        )
    )

    telegram_application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler,
        )
    )

    telegram_application.add_error_handler(
        error_handler
    )

    print(
        "Telegram handlers registered.",
        flush=True,
    )

    # --------------------------------------------------------
    # INITIALIZE
    # --------------------------------------------------------

    try:

        print(
            "Initializing Telegram application...",
            flush=True,
        )

        await telegram_application.initialize()

        print(
            "Telegram application initialized.",
            flush=True,
        )

    except Exception as e:

        print(
            "FATAL ERROR during initialize:",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        raise

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    try:

        print(
            "Starting Telegram application...",
            flush=True,
        )

        await telegram_application.start()

        print(
            "Telegram application started.",
            flush=True,
        )

    except Exception as e:

        print(
            "FATAL ERROR during start:",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        raise

    # --------------------------------------------------------
    # HTTP SERVER
    # --------------------------------------------------------

    try:

        start_http_server()

    except Exception as e:

        print(
            "FATAL ERROR starting "
            "HTTP server:",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        raise

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    webhook_url = (
        f"{RENDER_URL}"
        f"{WEBHOOK_PATH}"
    )

    try:

        print(
            f"Setting Telegram webhook: "
            f"{webhook_url}",
            flush=True,
        )

        await telegram_application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
        )

        print(
            "Telegram webhook set successfully.",
            flush=True,
        )

    except Exception as e:

        print(
            "FATAL ERROR setting webhook:",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        raise

    # --------------------------------------------------------
    # BOT INFO
    # --------------------------------------------------------

    try:

        bot_info = (
            await telegram_application
            .bot
            .get_me()
        )

        print(
            "========================================",
            flush=True,
        )

        print(
            "Movie Myanmar Bot is LIVE.",
            flush=True,
        )

        print(
            f"Bot: @{bot_info.username}",
            flush=True,
        )

        print(
            f"Database: "
            f"{len(movie_database)} movies",
            flush=True,
        )

        print(
            f"AI users: "
            f"{len(ai_usage)}",
            flush=True,
        )

        print(
            f"AI Free Limit: "
            f"{FREE_AI_LIMIT}",
            flush=True,
        )

        print(
            f"Admin ID: "
            f"{ADMIN_CHAT_ID}",
            flush=True,
        )

        print(
            f"Webhook: "
            f"{webhook_url}",
            flush=True,
        )

        print(
            "PROCESS KEEP-ALIVE: ACTIVE",
            flush=True,
        )

        print(
            "========================================",
            flush=True,
        )

    except Exception as e:

        print(
            f"Bot info error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )

    # --------------------------------------------------------
    # KEEP PROCESS ALIVE
    # --------------------------------------------------------

    try:

        await asyncio.Event().wait()

    finally:

        print(
            "Shutting down Movie Myanmar Bot...",
            flush=True,
        )

        if http_server:

            try:

                http_server.shutdown()

            except Exception:

                pass

        try:

            await telegram_application.stop()

        except Exception:

            pass

        try:

            await telegram_application.shutdown()

        except Exception:

            pass


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    print(
        "Executing bot.py main entry point...",
        flush=True,
    )

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot stopped by keyboard interrupt.",
            flush=True,
        )

    except Exception as e:

        print(
            "========================================",
            flush=True,
        )

        print(
            "FATAL STARTUP ERROR",
            flush=True,
        )

        print(
            f"{type(e).__name__}: {e}",
            flush=True,
        )

        traceback.print_exc()

        print(
            "========================================",
            flush=True,
        )

        raise
