import os
import asyncio
import hashlib
import json
import threading
import re
import traceback
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

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


# =========================================================
# AI CONFIG
# =========================================================

FREE_AI_LIMIT = 20
AI_USAGE_FILE = "ai_usage.json"

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        gemini_client = None

GEMINI_MODEL = "gemini-3.8-flash"


# =========================================================
# FILES
# =========================================================

MOVIE_DB_FILE = "movies.json"


# =========================================================
# WELCOME IMAGE FALLBACK
# =========================================================

WELCOME_IMAGE = (
    "https://raw.githubusercontent.com/"
    "cyber777mm-rgb/movie-gate-bot/main/"
    "ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"
)


# =========================================================
# MOVIE DATABASE
# =========================================================

movie_database = []


def load_movies():
    global movie_database

    try:
        if os.path.exists(MOVIE_DB_FILE):
            with open(MOVIE_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

                if isinstance(data, list):
                    movie_database = data
                else:
                    movie_database = []

        else:
            movie_database = []

    except Exception:
        movie_database = []


def save_movies():
    try:
        with open(MOVIE_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(
                movie_database,
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        traceback.print_exc()


load_movies()


# =========================================================
# AI USAGE
# =========================================================

ai_usage = {}


def load_ai_usage():
    global ai_usage

    try:
        if os.path.exists(AI_USAGE_FILE):
            with open(AI_USAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

                if isinstance(data, dict):
                    ai_usage = data
                else:
                    ai_usage = {}

    except Exception:
        ai_usage = {}


def save_ai_usage():
    try:
        with open(AI_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                ai_usage,
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        traceback.print_exc()


load_ai_usage()


def get_ai_usage(user_id):
    return int(ai_usage.get(str(user_id), 0))


def increase_ai_usage(user_id):
    key = str(user_id)

    ai_usage[key] = get_ai_usage(user_id) + 1

    save_ai_usage()


# =========================================================
# GENRES
# =========================================================

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


# =========================================================
# USER STATES
# =========================================================

user_search_mode = {}
user_ai_mode = {}
user_last_results = {}


# =========================================================
# MOVIE HELPERS
# =========================================================

def normalize_text(text):
    if not text:
        return ""

    text = text.lower().strip()

    text = re.sub(r"[\[\]\(\)\{\}:,._\-|/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text


def extract_year(text):
    if not text:
        return None

    match = re.search(r"\b(19|20)\d{2}\b", text)

    if match:
        return match.group(0)

    return None


def detect_genres(text):
    if not text:
        return []

    normalized = normalize_text(text)

    genres = []

    for genre, keywords in GENRE_KEYWORDS.items():

        for keyword in keywords:

            if normalize_text(keyword) in normalized:
                genres.append(genre)
                break

    return genres


def parse_movie_post(message):

    caption = message.caption or ""
    text = message.text or ""

    content = caption if caption else text

    if not content:
        return None

    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    title = lines[0]

    year = extract_year(content)

    quality = ""

    quality_match = re.search(
        r"\b(2160p|4k|1080p|720p|480p)\b",
        content,
        re.IGNORECASE,
    )

    if quality_match:
        quality = quality_match.group(1)

    audio = ""

    audio_match = re.search(
        r"\b(မြန်မာစာတန်းထိုး|မြန်မာစာတန်း|မြန်မာအသံ|Myanmar|Burmese|English|Dual Audio|Multi Audio)\b",
        content,
        re.IGNORECASE,
    )

    if audio_match:
        audio = audio_match.group(1)

    genres = detect_genres(content)

    poster_file_id = None

    try:
        if message.photo:
            poster_file_id = message.photo[-1].file_id
    except Exception:
        poster_file_id = None

    movie = {
        "title": title,
        "year": year or "",
        "quality": quality,
        "audio": audio,
        "original_name": title,
        "genres": genres,
        "description": content,
        "post_link": "",
        "chat_id": str(message.chat_id),
        "message_id": message.message_id,
        "poster_file_id": poster_file_id,
    }

    return movie


def build_movie_link(movie):

    chat_id = str(movie.get("chat_id", ""))
    message_id = movie.get("message_id")

    if not message_id:
        return movie.get("post_link", MOVIE_CHANNEL_URL)

    if chat_id.startswith("-100"):

        internal_id = chat_id[4:]

        return f"https://t.me/c/{internal_id}/{message_id}"

    return movie.get(
        "post_link",
        MOVIE_CHANNEL_URL,
    )


def movie_search(query):

    query = normalize_text(query)

    if not query:
        return []

    results = []

    year = extract_year(query)

    for movie in movie_database:

        title = normalize_text(
            movie.get("title", "")
        )

        original_name = normalize_text(
            movie.get("original_name", "")
        )

        description = normalize_text(
            movie.get("description", "")
        )

        score = 0

        if query == title:
            score = 1.0

        elif query == original_name:
            score = 0.98

        elif query in title:
            score = 0.95

        elif title in query:
            score = 0.90

        elif query in original_name:
            score = 0.88

        else:
            ratio = SequenceMatcher(
                None,
                query,
                title,
            ).ratio()

            score = ratio

        if query in description:
            score = max(score, 0.72)

        if year:

            movie_year = str(
                movie.get("year", "")
            )

            if movie_year == year:
                score += 0.08

        if score >= 0.68:

            results.append(
                (
                    score,
                    movie,
                )
            )

    results.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return [
        movie
        for score, movie in results
    ]


def get_latest_movies(limit=10):

    movies = list(movie_database)

    movies.reverse()

    return movies[:limit]


def get_next_poster_movie(current_index=0):

    movies = [
        m
        for m in get_latest_movies(20)
        if m.get("poster_file_id")
    ]

    if not movies:
        return None, 0

    if current_index >= len(movies):
        current_index = 0

    return (
        movies[current_index],
        current_index,
    )


# =========================================================
# KEYBOARDS
# =========================================================

def join_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Join Movie Myanmar",
                url=MOVIE_CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ I Joined",
                callback_data="check_join",
            )
        ],
    ])


def home_keyboard():

    return InlineKeyboardMarkup([
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
                "🆕 Latest Movies",
                callback_data="latest_movies",
            ),
            InlineKeyboardButton(
                "🤖 AI Chat",
                callback_data="ai_chat",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎞️ Next Movie",
                callback_data="next_poster",
            ),
            InlineKeyboardButton(
                "📢 Movie Channel",
                url=MOVIE_CHANNEL_URL,
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            )
        ],
    ])


def category_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎬 All Movies",
                callback_data="genre_All",
            )
        ],
        [
            InlineKeyboardButton(
                "⚔️ Action",
                callback_data="genre_Action",
            ),
            InlineKeyboardButton(
                "😂 Comedy",
                callback_data="genre_Comedy",
            ),
        ],
        [
            InlineKeyboardButton(
                "❤️ Romance",
                callback_data="genre_Romance",
            ),
            InlineKeyboardButton(
                "👻 Horror",
                callback_data="genre_Horror",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔥 Thriller",
                callback_data="genre_Thriller",
            ),
            InlineKeyboardButton(
                "🎭 Drama",
                callback_data="genre_Drama",
            ),
        ],
        [
            InlineKeyboardButton(
                "✨ Fantasy",
                callback_data="genre_Fantasy",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ],
    ])


def movie_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Movie Myanmar Channel",
                url=MOVIE_CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ],
    ])


# =========================================================
# MEMBERSHIP
# =========================================================

async def is_member(bot, user_id):

    try:

        member = await bot.get_chat_member(
            chat_id=MAIN_CHANNEL,
            user_id=user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception:

        return False


# =========================================================
# HOME TEXT
# =========================================================

def home_caption(movie=None):

    if movie:

        title = movie.get(
            "title",
            "Latest Movie",
        )

        year = movie.get(
            "year",
            "",
        )

        quality = movie.get(
            "quality",
            "",
        )

        audio = movie.get(
            "audio",
            "",
        )

        movie_info = title

        if year:
            movie_info += f" • {year}"

        if quality:
            movie_info += f" • {quality}"

        if audio:
            movie_info += f" • {audio}"

        return (
            "🎬 <b>MOVIE MYANMAR</b>\n\n"
            "☔ <b>Xiao Yu CyB (小雨)</b> ရဲ့ Movie Hub ထဲ "
            "ရောက်လာပြီ 😝❤️\n\n"
            f"🔥 <b>Latest Movie</b>\n"
            f"🎞️ {movie_info}\n\n"
            "🎥 Movie ရှာမယ်\n"
            "🎭 Genre ရွေးမယ်\n"
            "🆕 ကားအသစ်တွေကြည့်မယ်\n"
            "🤖 Xiao Yu နဲ့ စကားပြောမယ်\n\n"
            "👇 <b>အောက်က Menu ကနေ ရွေးလိုက်</b>"
        )

    return (
        "🎬 <b>MOVIE MYANMAR</b>\n\n"
        "☔ <b>Xiao Yu CyB (小雨)</b> ပါ 😝❤️\n\n"
        "🎥 Movie ရှာလို့ရတယ်\n"
        "🎭 Genre နဲ့ရှာလို့ရတယ်\n"
        "🆕 Latest Movies ကြည့်လို့ရတယ်\n"
        "🤖 AI နဲ့လည်း စကားပြောလို့ရတယ်\n\n"
        "👇 <b>အောက်က Menu ကိုရွေးပါ</b>"
    )


# =========================================================
# SHOW HOME
# =========================================================

async def show_home(
    update,
    context,
    poster_index=0,
):

    movie, index = get_next_poster_movie(
        poster_index
    )

    keyboard = home_keyboard()

    # -----------------------------------------------------
    # CALLBACK
    # -----------------------------------------------------

    if update.callback_query:

        query = update.callback_query

        await query.answer()

        message = query.message

        try:

            if movie and movie.get(
                "poster_file_id"
            ):

                try:

                    await message.edit_media(
                        media=InputMediaPhoto(
                            media=movie[
                                "poster_file_id"
                            ],
                            caption=home_caption(
                                movie
                            ),
                            parse_mode="HTML",
                        ),
                        reply_markup=keyboard,
                    )

                except Exception:

                    await message.edit_caption(
                        caption=home_caption(
                            movie
                        ),
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )

            else:

                try:

                    await message.edit_caption(
                        caption=home_caption(),
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )

                except Exception:

                    await message.edit_text(
                        home_caption(),
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )

        except Exception:

            try:

                await message.reply_text(
                    home_caption(movie),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            except Exception:
                pass

        return

    # -----------------------------------------------------
    # /START
    # -----------------------------------------------------

    chat = update.effective_chat

    if movie and movie.get(
        "poster_file_id"
    ):

        try:

            await chat.send_photo(
                photo=movie[
                    "poster_file_id"
                ],
                caption=home_caption(
                    movie
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

            return

        except Exception:
            pass

    try:

        await chat.send_photo(
            photo=WELCOME_IMAGE,
            caption=home_caption(),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception:

        await chat.send_message(
            text=home_caption(),
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    joined = await is_member(
        context.bot,
        user.id,
    )

    if not joined:

        await update.message.reply_text(
            "🎬 <b>Movie Myanmar</b>\n\n"
            "အရင်ဆုံး Movie Myanmar Channel ကို "
            "Join လုပ်ပေးပါ 😝❤️\n\n"
            "Join ပြီးရင် <b>✅ I Joined</b> ကိုနှိပ်ပါ။",
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

        return

    user_search_mode[user.id] = False
    user_ai_mode[user.id] = False

    await show_home(
        update,
        context,
    )


# =========================================================
# JOIN CHECK
# =========================================================

async def check_join_callback(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    joined = await is_member(
        context.bot,
        user.id,
    )

    if not joined:

        await query.answer(
            "❌ Channel ကို Join မလုပ်ရသေးဘူးနော် 😝",
            show_alert=True,
        )

        return

    await query.message.edit_text(
        "✅ <b>Join အောင်မြင်ပြီ!</b>\n\n"
        "🎬 Movie Myanmar ထဲကို "
        "ဝင်လို့ရပါပြီ ❤️",
        parse_mode="HTML",
    )

    await asyncio.sleep(1)

    await show_home(
        update,
        context,
    )


# =========================================================
# SEARCH
# =========================================================

async def start_search(
    update,
    context,
):

    user = update.effective_user

    user_search_mode[user.id] = True
    user_ai_mode[user.id] = False

    text = (
        "🔎 <b>Movie Search</b>\n\n"
        "ရှာချင်တဲ့ Movie နာမည်ကို ရိုက်ပေးပါ။\n\n"
        "ဥပမာ —\n"
        "🎬 Avatar\n"
        "🎬 John Wick\n"
        "🎬 Avengers\n\n"
        "❌ ပြန်ထွက်ချင်ရင် <b>Home</b> လို့ရိုက်ပါ။"
    )

    if update.callback_query:

        query = update.callback_query

        await query.answer()

        try:

            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
            )

        except Exception:

            try:

                await query.message.edit_text(
                    text,
                    parse_mode="HTML",
                )

            except Exception:
                pass

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
        )


# =========================================================
# LATEST MOVIES
# =========================================================

async def show_latest_movies(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    latest = get_latest_movies(15)

    if not latest:

        await query.message.edit_caption(
            caption=(
                "🆕 <b>Latest Movies</b>\n\n"
                "အခုထိ Movie မရှိသေးဘူး 😭"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏠 Home",
                        callback_data="home",
                    )
                ]
            ]),
        )

        return

    buttons = []

    for movie in latest:

        title = movie.get(
            "title",
            "Unknown Movie",
        )

        year = movie.get(
            "year",
            "",
        )

        label = f"🎬 {title}"

        if year:
            label += f" ({year})"

        buttons.append([
            InlineKeyboardButton(
                label[:64],
                url=build_movie_link(movie),
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Home",
            callback_data="home",
        )
    ])

    text = (
        "🆕 <b>LATEST MOVIES</b>\n\n"
        "🔥 Movie Myanmar မှာ "
        "နောက်ဆုံးတင်ထားတဲ့ကားတွေ\n\n"
        f"🎞️ Total: <b>{len(latest)}</b>"
    )

    try:

        await query.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )

    except Exception:

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )


# =========================================================
# NEXT POSTER
# =========================================================

async def next_poster(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    movies = [
        m
        for m in get_latest_movies(20)
        if m.get("poster_file_id")
    ]

    if not movies:

        await query.answer(
            "Poster မရှိသေးဘူး 😭",
            show_alert=True,
        )

        return

    current = int(
        context.user_data.get(
            "poster_index",
            0,
        )
    )

    current += 1

    if current >= len(movies):
        current = 0

    context.user_data[
        "poster_index"
    ] = current

    movie = movies[current]

    try:

        await query.message.edit_media(
            media=InputMediaPhoto(
                media=movie[
                    "poster_file_id"
                ],
                caption=home_caption(
                    movie
                ),
                parse_mode="HTML",
            ),
            reply_markup=home_keyboard(),
        )

    except Exception:

        try:

            await query.message.edit_caption(
                caption=home_caption(
                    movie
                ),
                parse_mode="HTML",
                reply_markup=home_keyboard(),
            )

        except Exception:
            pass


# =========================================================
# CATEGORIES
# =========================================================

async def show_categories(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    text = (
        "🎭 <b>MOVIE CATEGORIES</b>\n\n"
        "ကြိုက်တဲ့ Genre ကိုရွေးလိုက် 😝🍿"
    )

    try:

        await query.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=category_keyboard(),
        )

    except Exception:

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=category_keyboard(),
        )


async def show_genre(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    genre = query.data.replace(
        "genre_",
        "",
    )

    if genre == "All":

        results = list(movie_database)

    else:

        results = [
            movie
            for movie in movie_database
            if genre in movie.get(
                "genres",
                [],
            )
        ]

    if not results:

        text = (
            f"🎭 <b>{genre}</b>\n\n"
            "ဒီ Genre ထဲမှာ Movie မတွေ့သေးဘူး 😭"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎭 Categories",
                    callback_data="categories",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Home",
                    callback_data="home",
                )
            ],
        ])

        try:

            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        except Exception:

            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        return

    results = list(reversed(results))[:15]

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

        label = f"🎬 {title}"

        if year:
            label += f" ({year})"

        buttons.append([
            InlineKeyboardButton(
                label[:64],
                url=build_movie_link(movie),
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🎭 Categories",
            callback_data="categories",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Home",
            callback_data="home",
        )
    ])

    text = (
        f"🎭 <b>{genre}</b>\n\n"
        f"🎬 Found: <b>{len(results)}</b>"
    )

    try:

        await query.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )

    except Exception:

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )


# =========================================================
# HELP
# =========================================================

async def show_help(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    text = (
        "ℹ️ <b>Movie Myanmar Help</b>\n\n"
        "🔎 <b>Search Movie</b>\n"
        "Movie နာမည်ရိုက်ပြီး ရှာနိုင်တယ်။\n\n"
        "🎭 <b>Categories</b>\n"
        "Genre အလိုက် ရှာနိုင်တယ်။\n\n"
        "🆕 <b>Latest Movies</b>\n"
        "အသစ်တင်ထားတဲ့ကားတွေ ကြည့်နိုင်တယ်။\n\n"
        "🤖 <b>AI Chat</b>\n"
        "Xiao Yu နဲ့ စကားပြောနိုင်တယ်။\n\n"
        "📢 <b>Movie Channel</b>\n"
        "Movie Myanmar Channel ကို ဝင်နိုင်တယ်။"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ]
    ])

    try:

        await query.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception:

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# AI
# =========================================================

def movie_fallback():

    return (
        "ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\n"
        "ငါ့ကို Movie မေး 😝🎬\n"
        "ကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။\n\n"
        "ဥပမာ — <b>Avatar ရှာပေး</b> လို့ပြော။"
    )


async def start_ai(
    update,
    context,
):

    user = update.effective_user

    user_ai_mode[user.id] = True
    user_search_mode[user.id] = False

    used = get_ai_usage(
        user.id
    )

    remaining = max(
        0,
        FREE_AI_LIMIT - used,
    )

    text = (
        "🤖 <b>Xiao Yu AI Chat</b>\n\n"
        "ဟယ်လို 😝❤️\n"
        "ငါနဲ့ စကားပြောချင်တာလား?\n\n"
        f"🎟️ ဒီနေ့ Free AI ကျန်တာ — "
        f"<b>{remaining}</b>\n\n"
        "ဘာမဆို ရေးလို့ရတယ်။"
    )

    if update.callback_query:

        query = update.callback_query

        await query.answer()

        try:

            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏠 Home",
                            callback_data="home",
                        )
                    ]
                ]),
            )

        except Exception:

            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏠 Home",
                            callback_data="home",
                        )
                    ]
                ]),
            )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
        )


async def ask_gemini(
    user_text,
):

    if not gemini_client:
        return "AI service မဖွင့်ထားသေးဘူး 😭"

    prompt = f"""
You are Xiao Yu CyB (小雨), the friendly female AI assistant
of Movie Myanmar Telegram Bot.

Reply naturally in Burmese.
Be playful, cute, slightly teasing, and friendly.
Keep answers concise unless the user asks for detail.
If the user asks about movies, help them search movies.

User:
{user_text}
"""

    try:

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        return (
            response.text
            if response and response.text
            else "ဟယ်... ငါဘာပြန်ပြောရမလဲ မသိတော့ဘူး 😭"
        )

    except Exception:

        traceback.print_exc()

        return (
            "အခု AI က နည်းနည်းပင်ပန်းနေတယ် 😭\n"
            "ခဏနေ ပြန်မေးနော် ❤️"
        )


# =========================================================
# CHANNEL LINK
# =========================================================

CHANNEL_REQUEST_WORDS = [
    "movie myanmar channel",
    "movie channel link",
    "channel link",
    "telegram channel",
    "channel ပေး",
    "channel link ပေး",
    "channel လင့်",
]


def is_channel_request(text):

    normalized = normalize_text(text)

    return any(
        word in normalized
        for word in CHANNEL_REQUEST_WORDS
    )


# =========================================================
# MOVIE INTENT
# =========================================================

MOVIE_INTENT_WORDS = [
    "movie",
    "film",
    "ကား",
    "ဇာတ်ကား",
    "ရုပ်ရှင်",
    "ရှာပေး",
    "ရှာပေးပါ",
    "ရှာမယ်",
    "ကြည့်ချင်",
    "ကြည့်မယ်",
    "ရှိလား",
    "ရှိမလား",
    "ကားပေး",
    "ကားလိုချင်",
    "movie link",
]


def has_movie_intent(text):

    normalized = normalize_text(text)

    return any(
        word in normalized
        for word in MOVIE_INTENT_WORDS
    )


# =========================================================
# SPECIAL REPLIES
# =========================================================

async def handle_special_reply(
    update,
    context,
    text,
):

    normalized = normalize_text(text)

    if normalized in [
        "ဟေး",
        "ဟယ်လို",
        "hello",
        "hi",
        "hey",
    ]:

        await update.message.reply_text(
            "ဟေးဟေး 😝❤️\n"
            "Xiao Yu ဒီမှာရှိတယ်။\n\n"
            "Movie ရှာမလား၊ စကားပြောမလား?"
        )

        return True

    if (
        "နေကောင်းလား" in normalized
        or "နေကောင်း" in normalized
    ):

        await update.message.reply_text(
            "ကောင်းတယ်ဟဲ့ 😝❤️\n"
            "မင်းကရော?"
        )

        return True

    return False


# =========================================================
# CHANNEL POST
# =========================================================

async def channel_post_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.channel_post

    if not message:
        return

    if str(message.chat.username or "").lower() != MAIN_CHANNEL.replace(
        "@",
        "",
    ).lower():

        return

    movie = parse_movie_post(
        message
    )

    if not movie:
        return

    movie["post_link"] = (
        f"https://t.me/{message.chat.username}/"
        f"{message.message_id}"
    )

    # -----------------------------------------------
    # UPDATE EXISTING MOVIE
    # -----------------------------------------------

    existing_index = None

    for i, old_movie in enumerate(
        movie_database
    ):

        if (
            str(old_movie.get("chat_id"))
            == str(movie.get("chat_id"))
            and str(old_movie.get("message_id"))
            == str(movie.get("message_id"))
        ):

            existing_index = i
            break

    if existing_index is not None:

        old = movie_database[
            existing_index
        ]

        # Keep poster if new edited post
        # doesn't contain a photo.
        if not movie.get(
            "poster_file_id"
        ):

            movie[
                "poster_file_id"
            ] = old.get(
                "poster_file_id"
            )

        movie_database[
            existing_index
        ] = movie

    else:

        movie_database.append(
            movie
        )

    save_movies()

    print(
        "🎬 Movie saved:",
        movie.get("title"),
        "Poster:",
        bool(
            movie.get(
                "poster_file_id"
            )
        ),
    )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    text = (
        update.message.text or ""
    ).strip()

    if not text:
        return

    # -----------------------------------------------------
    # MEMBERSHIP
    # -----------------------------------------------------

    joined = await is_member(
        context.bot,
        user.id,
    )

    if not joined:

        await update.message.reply_text(
            "🔒 <b>Movie Myanmar</b>\n\n"
            "အရင်ဆုံး Channel Join လုပ်ပါ 😝❤️",
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )

        return

    normalized = normalize_text(
        text
    )

    # -----------------------------------------------------
    # HOME
    # -----------------------------------------------------

    if normalized in [
        "home",
        "menu",
        "ပင်မ",
        "မီနူး",
    ]:

        user_search_mode[user.id] = False
        user_ai_mode[user.id] = False

        await show_home(
            update,
            context,
        )

        return

    # -----------------------------------------------------
    # CHANNEL REQUEST
    # -----------------------------------------------------

    if is_channel_request(
        text
    ):

        await update.message.reply_text(
            "📢 <b>Movie Myanmar Channel</b>\n\n"
            "👇 ဒီမှာ ဝင်လို့ရတယ် ❤️",
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SEARCH MODE
    # -----------------------------------------------------

    if user_search_mode.get(
        user.id,
        False,
    ):

        results = movie_search(
            text
        )

        if results:

            results = results[:15]

            user_last_results[
                user.id
            ] = results

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

                label = f"🎬 {title}"

                if year:
                    label += f" ({year})"

                buttons.append([
                    InlineKeyboardButton(
                        label[:64],
                        url=build_movie_link(
                            movie
                        ),
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "🏠 Home",
                    callback_data="home",
                )
            ])

            await update.message.reply_text(
                f"🎬 <b>Found {len(results)} Movie(s)</b>\n\n"
                "👇 ကြည့်ချင်တဲ့ကားကို ရွေးပါ။",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    buttons
                ),
            )

        else:

            try:

                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        "🔔 <b>Movie Request</b>\n\n"
                        f"🎬 Requested: <b>{text}</b>\n"
                        f"👤 User: @{user.username or user.first_name}\n"
                        f"🆔 ID: <code>{user.id}</code>"
                    ),
                    parse_mode="HTML",
                )

            except Exception:
                pass

            await update.message.reply_text(
                f"😒 <b>{text}</b> ကို "
                "Movie Myanmar ထဲမှာ မတွေ့သေးဘူးဟာ။\n\n"
                "📨 Admin ဆီ Request ပို့ထားပြီ။",
                parse_mode="HTML",
            )

        user_search_mode[
            user.id
        ] = False

        return

    # -----------------------------------------------------
    # MOVIE DATABASE SEARCH FIRST
    # -----------------------------------------------------

    results = movie_search(
        text
    )

    if results:

        results = results[:15]

        user_last_results[
            user.id
        ] = results

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

            label = f"🎬 {title}"

            if year:
                label += f" ({year})"

            buttons.append([
                InlineKeyboardButton(
                    label[:64],
                    url=build_movie_link(
                        movie
                    ),
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ])

        await update.message.reply_text(
            f"🎬 <b>{len(results)} Movie(s) Found</b>\n\n"
            "👇 ကြည့်ချင်တဲ့ကားကို ရွေးပါ။",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )

        return

    # -----------------------------------------------------
    # MOVIE INTENT
    # -----------------------------------------------------

    if has_movie_intent(
        text
    ):

        try:

            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    "🔔 <b>Movie Request</b>\n\n"
                    f"🎬 Requested: <b>{text}</b>\n"
                    f"👤 User: @{user.username or user.first_name}\n"
                    f"🆔 ID: <code>{user.id}</code>"
                ),
                parse_mode="HTML",
            )

        except Exception:
            pass

        await update.message.reply_text(
            f"😒 <b>{text}</b> ကို "
            "Movie Myanmar ထဲမှာ မတွေ့သေးဘူးဟာ။\n\n"
            "📨 Admin ဆီ Request ပို့ထားပြီ။",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # SPECIAL REPLIES
    # -----------------------------------------------------

    handled = await handle_special_reply(
        update,
        context,
        text,
    )

    if handled:
        return

    # -----------------------------------------------------
    # AI MODE
    # -----------------------------------------------------

    ai_words = [
        "စကားပြော",
        "စကားပြောမယ်",
        "ai",
        "xiao",
        "小雨",
        "ဘာလုပ်နေလဲ",
        "နေကောင်းလား",
    ]

    if any(
        word in normalized
        for word in ai_words
    ):

        user_ai_mode[
            user.id
        ] = True

    if user_ai_mode.get(
        user.id,
        False,
    ):

        used = get_ai_usage(
            user.id
        )

        if used >= FREE_AI_LIMIT:

            await update.message.reply_text(
                movie_fallback(),
                parse_mode="HTML",
            )

            return

        reply = await ask_gemini(
            text
        )

        # Only count successful AI usage.
        if (
            reply
            and not reply.startswith(
                "အခု AI က"
            )
            and not reply.startswith(
                "AI service"
            )
        ):

            increase_ai_usage(
                user.id
            )

        remaining = max(
            0,
            FREE_AI_LIMIT
            - get_ai_usage(
                user.id
            ),
        )

        await update.message.reply_text(
            reply
            + f"\n\n<code>AI ကျန်သေးတာ: {remaining}</code>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # DEFAULT
    # -----------------------------------------------------

    await update.message.reply_text(
        "ဟေး 😝❤️\n\n"
        "🎬 Movie ရှာချင်ရင် Movie နာမည်ရိုက်ပါ။\n"
        "🤖 ငါနဲ့စကားပြောချင်ရင် <b>စကားပြောမယ်</b> လို့ပြော။\n\n"
        "ဒါမှမဟုတ် 👇 <b>Home</b> ကိုနှိပ်ပြီး Menu သုံးလို့ရတယ်။",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🏠 Home",
                    callback_data="home",
                )
            ]
        ]),
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    data = query.data

    if data == "check_join":

        await check_join_callback(
            update,
            context,
        )

        return

    if data == "home":

        context.user_data[
            "poster_index"
        ] = 0

        await show_home(
            update,
            context,
        )

        return

    if data == "next_poster":

        await next_poster(
            update,
            context,
        )

        return

    if data == "search_movie":

        await start_search(
            update,
            context,
        )

        return

    if data == "categories":

        await show_categories(
            update,
            context,
        )

        return

    if data == "latest_movies":

        await show_latest_movies(
            update,
            context,
        )

        return

    if data == "ai_chat":

        await start_ai(
            update,
            context,
        )

        return

    if data == "help":

        await show_help(
            update,
            context,
        )

        return

    if data.startswith(
        "genre_"
    ):

        await show_genre(
            update,
            context,
        )

        return

    await query.answer()


# =========================================================
# HEALTH SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path == "/health":

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.end_headers()

            self.wfile.write(
                b"Movie Myanmar Bot is alive"
            )

        else:

            self.send_response(
                404
            )

            self.end_headers()

    def log_message(
        self,
        format,
        *args,
    ):

        return


def start_health_server():

    server = HTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        HealthHandler,
    )

    server.serve_forever()


# =========================================================
# MAIN
# =========================================================

async def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )

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

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    await application.initialize()

    await application.start()

    webhook_url = (
        RENDER_URL
        + WEBHOOK_PATH
    )

    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message",
            "callback_query",
            "channel_post",
        ],
    )

    print(
        "🎬 Movie Myanmar Bot started"
    )

    print(
        "🌐 Webhook:",
        webhook_url,
    )

    await asyncio.Event().wait()


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot stopped."
        )

    except Exception:

        traceback.print_exc()
