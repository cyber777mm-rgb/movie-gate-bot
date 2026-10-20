import os
import asyncio
import hashlib
import json
import threading
import re
import traceback
import io
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from PIL import Image, ImageDraw

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputFile,
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

WEBHOOK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode()
).hexdigest()[:32]


# =========================================================
# WEBHOOK RUNTIME
# =========================================================

webhook_application = None
webhook_loop = None


# =========================================================
# MOVIE CAROUSEL
# =========================================================

CAROUSEL_LIMIT = 5

# How often the visible 3-card group changes
CAROUSEL_INTERVAL = 4

# Number of cards visible at once
CAROUSEL_VISIBLE = 3

# Each movie card is 16:9
CARD_WIDTH = 420
CARD_HEIGHT = 236

# Gap between cards
CARD_GAP = 12

# Non-black background
CAROUSEL_BG = (245, 245, 245)

carousel_tasks = {}
carousel_message_ids = {}

# Cache downloaded Telegram posters
poster_image_cache = {}


# =========================================================
# AI CONFIG
# =========================================================

FREE_AI_LIMIT = 20
AI_USAGE_FILE = "ai_usage.json"

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )
    except Exception:
        gemini_client = None

GEMINI_MODEL = "gemini-3.8-flash"


# =========================================================
# FILES
# =========================================================

MOVIE_DB_FILE = "movies.json"


# =========================================================
# WELCOME IMAGE
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

            with open(
                MOVIE_DB_FILE,
                "r",
                encoding="utf-8",
            ) as f:

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

        with open(
            MOVIE_DB_FILE,
            "w",
            encoding="utf-8",
        ) as f:

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

            with open(
                AI_USAGE_FILE,
                "r",
                encoding="utf-8",
            ) as f:

                data = json.load(f)

                if isinstance(data, dict):
                    ai_usage = data
                else:
                    ai_usage = {}

    except Exception:

        ai_usage = {}


def save_ai_usage():

    try:

        with open(
            AI_USAGE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

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

    return int(
        ai_usage.get(
            str(user_id),
            0,
        )
    )


def increase_ai_usage(user_id):

    key = str(user_id)

    ai_usage[key] = (
        get_ai_usage(user_id)
        + 1
    )

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
# TEXT HELPERS
# =========================================================

def normalize_text(text):

    if not text:
        return ""

    text = text.lower().strip()

    text = re.sub(
        r"[\[\]\(\)\{\}:,._\-|/\\]+",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text


def extract_year(text):

    if not text:
        return None

    match = re.search(
        r"\b(19|20)\d{2}\b",
        text,
    )

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


# =========================================================
# MOVIE PARSER
# =========================================================

def parse_movie_post(message):

    caption = message.caption or ""
    text = message.text or ""

    content = (
        caption
        if caption
        else text
    )

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

        "chat_id": str(
            message.chat_id
        ),

        "message_id": message.message_id,

        "poster_file_id": poster_file_id,

    }

    return movie


def build_movie_link(movie):

    chat_id = str(
        movie.get(
            "chat_id",
            "",
        )
    )

    message_id = movie.get(
        "message_id"
    )

    if not message_id:

        return movie.get(
            "post_link",
            MOVIE_CHANNEL_URL,
        )

    if chat_id.startswith("-100"):

        internal_id = chat_id[4:]

        return (
            f"https://t.me/c/"
            f"{internal_id}/"
            f"{message_id}"
        )

    return movie.get(
        "post_link",
        MOVIE_CHANNEL_URL,
    )


# =========================================================
# SEARCH
# =========================================================

def movie_search(query):

    query = normalize_text(query)

    if not query:
        return []

    results = []

    year = extract_year(query)

    for movie in movie_database:

        title = normalize_text(
            movie.get(
                "title",
                "",
            )
        )

        original_name = normalize_text(
            movie.get(
                "original_name",
                "",
            )
        )

        description = normalize_text(
            movie.get(
                "description",
                "",
            )
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

            score = max(
                score,
                0.72,
            )

        if year:

            movie_year = str(
                movie.get(
                    "year",
                    "",
                )
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

    movies = list(
        movie_database
    )

    movies.reverse()

    return movies[:limit]


# =========================================================
# CAROUSEL MOVIES
# =========================================================

def get_carousel_movies(
    limit=CAROUSEL_LIMIT
):

    movies = [

        movie

        for movie in reversed(
            movie_database
        )

        if movie.get(
            "poster_file_id"
        )

    ]

    return movies[:limit]


def movie_identity(movie):

    if not movie:
        return ""

    return (
        str(
            movie.get(
                "chat_id",
                "",
            )
        )
        + ":"
        + str(
            movie.get(
                "message_id",
                "",
            )
        )
    )


# =========================================================
# POSTER IMAGE HELPERS
# =========================================================

async def get_poster_image(
    bot,
    file_id,
):

    if not file_id:
        return None

    if file_id in poster_image_cache:

        try:

            return poster_image_cache[
                file_id
            ].copy()

        except Exception:

            pass

    try:

        telegram_file = await bot.get_file(
            file_id
        )

        image_bytes = (
            await telegram_file.download_as_bytearray()
        )

        image = Image.open(
            io.BytesIO(
                bytes(image_bytes)
            )
        ).convert("RGB")

        poster_image_cache[
            file_id
        ] = image.copy()

        return image

    except Exception:

        traceback.print_exc()

        return None


def crop_to_16_9(
    image,
    width,
    height,
):

    target_ratio = (
        width / height
    )

    image_ratio = (
        image.width / image.height
    )

    if image_ratio > target_ratio:

        new_width = int(
            image.height
            * target_ratio
        )

        left = (
            image.width
            - new_width
        ) // 2

        image = image.crop(
            (
                left,
                0,
                left + new_width,
                image.height,
            )
        )

    else:

        new_height = int(
            image.width
            / target_ratio
        )

        top = (
            image.height
            - new_height
        ) // 2

        image = image.crop(
            (
                0,
                top,
                image.width,
                top + new_height,
            )
        )

    return image.resize(
        (
            width,
            height,
        ),
        Image.Resampling.LANCZOS,
    )


async def create_carousel_image(
    context,
    movies,
    start_index,
):

    if not movies:
        return None

    count = len(movies)

    visible_movies = []

    for offset in range(
        CAROUSEL_VISIBLE
    ):

        movie = movies[
            (start_index + offset)
            % count
        ]

        visible_movies.append(
            movie
        )

    total_width = (
        CARD_WIDTH * CAROUSEL_VISIBLE
        + CARD_GAP
        * (CAROUSEL_VISIBLE - 1)
    )

    canvas = Image.new(
        "RGB",
        (
            total_width,
            CARD_HEIGHT,
        ),
        CAROUSEL_BG,
    )

    for position, movie in enumerate(
        visible_movies
    ):

        poster_id = movie.get(
            "poster_file_id"
        )

        poster = await get_poster_image(
            context.bot,
            poster_id,
        )

        if poster is None:
            continue

        card = crop_to_16_9(
            poster,
            CARD_WIDTH,
            CARD_HEIGHT,
        )

        x = (
            position
            * (
                CARD_WIDTH
                + CARD_GAP
            )
        )

        canvas.paste(
            card,
            (
                x,
                0,
            ),
        )

    output = io.BytesIO()

    canvas.save(
        output,
        format="JPEG",
        quality=90,
        optimize=True,
    )

    output.seek(0)

    return output.getvalue()


def carousel_caption(
    movies,
    start_index,
):

    if not movies:
        return home_caption()

    visible = []

    for offset in range(
        CAROUSEL_VISIBLE
    ):

        movie = movies[
            (start_index + offset)
            % len(movies)
        ]

        title = movie.get(
            "title",
            "Movie",
        )

        visible.append(
            title
        )

    return (

        "🎬 <b>MOVIE MYANMAR</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "☔ <b>Xiao Yu CyB (小雨)</b> ❤️\n\n"

        "🎞️ "
        + "  •  ".join(
            visible
        )
        + "\n\n"

        "🎥 Movie ရှာမယ်\n"
        "🎭 Genre ရွေးမယ်\n"
        "🆕 ကားအသစ်တွေကြည့်မယ်\n"
        "🤖 Xiao Yu နဲ့ စကားပြောမယ်\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>အောက်က Menu ကနေ ရွေးလိုက်</b>"

    )


# =========================================================
# CAROUSEL CONTROL
# =========================================================

async def stop_carousel(
    chat_id
):

    task = carousel_tasks.pop(
        chat_id,
        None,
    )

    carousel_message_ids.pop(
        chat_id,
        None,
    )

    if not task:
        return

    if task is asyncio.current_task():
        return

    task.cancel()

    try:

        await task

    except asyncio.CancelledError:

        pass

    except Exception:

        pass


async def carousel_loop(
    context,
    chat_id,
    message_id,
    current_start=0,
):

    try:

        while True:

            await asyncio.sleep(
                CAROUSEL_INTERVAL
            )

            movies = get_carousel_movies()

            if not movies:
                continue

            count = len(movies)

            # Keep position valid when a new movie arrives.
            current_start %= count

            # If there are fewer than 3 movies,
            # repeat them to fill the row.
            if count < CAROUSEL_VISIBLE:

                display_movies = list(
                    movies
                )

                while len(
                    display_movies
                ) < CAROUSEL_VISIBLE:

                    display_movies.extend(
                        movies
                    )

                display_movies = (
                    display_movies[
                        :CAROUSEL_VISIBLE
                    ]
                )

                image_bytes = (
                    await create_carousel_image(
                        context,
                        display_movies,
                        0,
                    )
                )

                caption = carousel_caption(
                    display_movies,
                    0,
                )

            else:

                image_bytes = (
                    await create_carousel_image(
                        context,
                        movies,
                        current_start,
                    )
                )

                caption = carousel_caption(
                    movies,
                    current_start,
                )

            if not image_bytes:
                continue

            if (
                carousel_message_ids.get(
                    chat_id
                )
                != message_id
            ):

                return

            try:

                media_file = InputFile(
                    io.BytesIO(
                        image_bytes
                    ),
                    filename="movie_carousel.jpg",
                )

                await context.bot.edit_message_media(

                    chat_id=chat_id,

                    message_id=message_id,

                    media=InputMediaPhoto(
                        media=media_file,
                        caption=caption,
                        parse_mode="HTML",
                    ),

                    reply_markup=home_keyboard(),

                )

                current_start = (
                    current_start + 1
                ) % max(
                    1,
                    len(movies)
                )

            except Exception as e:

                error_text = str(
                    e
                ).lower()

                if (
                    "message is not modified"
                    in error_text
                ):

                    current_start = (
                        current_start + 1
                    ) % max(
                        1,
                        len(movies)
                    )

                    continue

                if any(
                    error in error_text
                    for error in [
                        "message to edit not found",
                        "message can't be edited",
                        "message identifier is not specified",
                        "chat not found",
                        "forbidden",
                    ]
                ):

                    return

                traceback.print_exc()

    except asyncio.CancelledError:

        raise

    except Exception:

        traceback.print_exc()

    finally:

        current_task = (
            asyncio.current_task()
        )

        if (
            carousel_tasks.get(
                chat_id
            )
            is current_task
        ):

            carousel_tasks.pop(
                chat_id,
                None,
            )

            carousel_message_ids.pop(
                chat_id,
                None,
            )


async def start_carousel(
    context,
    chat_id,
    message_id,
    start_index=0,
):

    await stop_carousel(
        chat_id
    )

    carousel_message_ids[
        chat_id
    ] = message_id

    task = asyncio.create_task(

        carousel_loop(

            context,

            chat_id,

            message_id,

            start_index,

        )

    )

    carousel_tasks[
        chat_id
    ] = task


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

async def is_member(
    bot,
    user_id
):

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
# HOME CAPTION
# =========================================================

def home_caption(
    movie=None
):

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

            movie_info += (
                f" • {year}"
            )

        if quality:

            movie_info += (
                f" • {quality}"
            )

        if audio:

            movie_info += (
                f" • {audio}"
            )

        return (

            "🎬 <b>MOVIE MYANMAR</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            "☔ <b>Xiao Yu CyB (小雨)</b>\n"
            "🍿 Your Movie Hub ❤️\n\n"

            f"🔥 <b>Latest Movie</b>\n"
            f"🎞️ {movie_info}\n\n"

            "🎥 Movie ရှာမယ်\n"
            "🎭 Genre ရွေးမယ်\n"
            "🆕 ကားအသစ်တွေကြည့်မယ်\n"
            "🤖 Xiao Yu နဲ့ စကားပြောမယ်\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "👇 <b>အောက်က Menu ကနေ ရွေးလိုက်</b>"

        )

    return (

        "🎬 <b>MOVIE MYANMAR</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

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

    movies = get_carousel_movies()

    keyboard = home_keyboard()

    # -----------------------------------------------------
    # CALLBACK HOME
    # -----------------------------------------------------

    if update.callback_query:

        query = update.callback_query

        await query.answer()

        message = query.message
        chat_id = message.chat_id

        await stop_carousel(
            chat_id
        )

        if movies:

            # For the first home screen, start at newest.
            start_index = (
                poster_index
                % len(movies)
            )

            image_bytes = (
                await create_carousel_image(
                    context,
                    movies,
                    start_index,
                )
            )

            if image_bytes:

                caption = carousel_caption(
                    movies,
                    start_index,
                )

                try:

                    if message.photo:

                        media_file = InputFile(
                            io.BytesIO(
                                image_bytes
                            ),
                            filename="movie_carousel.jpg",
                        )

                        await message.edit_media(

                            media=InputMediaPhoto(
                                media=media_file,
                                caption=caption,
                                parse_mode="HTML",
                            ),

                            reply_markup=keyboard,

                        )

                        message_id = (
                            message.message_id
                        )

                    else:

                        sent = (
                            await context.bot.send_photo(

                                chat_id=chat_id,

                                photo=InputFile(
                                    io.BytesIO(
                                        image_bytes
                                    ),
                                    filename="movie_carousel.jpg",
                                ),

                                caption=caption,

                                parse_mode="HTML",

                                reply_markup=keyboard,

                            )
                        )

                        try:
                            await message.delete()
                        except Exception:
                            pass

                        message_id = (
                            sent.message_id
                        )

                    context.user_data[
                        "poster_index"
                    ] = start_index

                    await start_carousel(

                        context,

                        chat_id,

                        message_id,

                        start_index,

                    )

                    return

                except Exception:

                    traceback.print_exc()

        # -------------------------------------------------
        # Fallback
        # -------------------------------------------------

        try:

            sent = await context.bot.send_photo(

                chat_id=chat_id,

                photo=WELCOME_IMAGE,

                caption=home_caption(),

                parse_mode="HTML",

                reply_markup=keyboard,

            )

            try:

                await message.delete()

            except Exception:

                pass

            return

        except Exception:

            try:

                await message.edit_text(

                    home_caption(),

                    parse_mode="HTML",

                    reply_markup=keyboard,

                )

            except Exception:

                pass

        return

    # -----------------------------------------------------
    # NORMAL /START
    # -----------------------------------------------------

    chat = update.effective_chat

    await stop_carousel(
        chat.id
    )

    if movies:

        start_index = (
            poster_index
            % len(movies)
        )

        image_bytes = (
            await create_carousel_image(
                context,
                movies,
                start_index,
            )
        )

        if image_bytes:

            try:

                sent = await chat.send_photo(

                    photo=InputFile(
                        io.BytesIO(
                            image_bytes
                        ),
                        filename="movie_carousel.jpg",
                    ),

                    caption=carousel_caption(
                        movies,
                        start_index,
                    ),

                    parse_mode="HTML",

                    reply_markup=keyboard,

                )

                context.user_data[
                    "poster_index"
                ] = start_index

                await start_carousel(

                    context,

                    chat.id,

                    sent.message_id,

                    start_index,

                )

                return

            except Exception:

                traceback.print_exc()

    # -----------------------------------------------------
    # FALLBACK WELCOME IMAGE
    # -----------------------------------------------------

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

    user_search_mode[
        user.id
    ] = False

    user_ai_mode[
        user.id
    ] = False

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

    await stop_carousel(
        query.message.chat_id
    )

    await query.message.edit_text(

        "✅ <b>Join အောင်မြင်ပြီ!</b>\n\n"
        "🎬 Movie Myanmar ထဲကို "
        "ဝင်လို့ရပါပြီ ❤️",

        parse_mode="HTML",

    )

    await asyncio.sleep(
        1
    )

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

    user_search_mode[
        user.id
    ] = True

    user_ai_mode[
        user.id
    ] = False

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
# LATEST
# =========================================================

async def show_latest_movies(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    latest = get_latest_movies(
        15
    )

    if not latest:

        text = (

            "🆕 <b>Latest Movies</b>\n\n"

            "အခုထိ Movie မရှိသေးဘူး 😭"

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

            label += (
                f" ({year})"
            )

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
# NEXT MOVIE
# =========================================================

async def next_poster(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    chat_id = query.message.chat_id

    await stop_carousel(
        chat_id
    )

    movies = get_carousel_movies()

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

    current %= len(movies)

    context.user_data[
        "poster_index"
    ] = current

    image_bytes = (
        await create_carousel_image(
            context,
            movies,
            current,
        )
    )

    if not image_bytes:
        return

    caption = carousel_caption(
        movies,
        current,
    )

    try:

        if query.message.photo:

            media_file = InputFile(
                io.BytesIO(
                    image_bytes
                ),
                filename="movie_carousel.jpg",
            )

            await query.message.edit_media(

                media=InputMediaPhoto(
                    media=media_file,
                    caption=caption,
                    parse_mode="HTML",
                ),

                reply_markup=home_keyboard(),

            )

            message_id = (
                query.message.message_id
            )

        else:

            sent = await context.bot.send_photo(

                chat_id=chat_id,

                photo=InputFile(
                    io.BytesIO(
                        image_bytes
                    ),
                    filename="movie_carousel.jpg",
                ),

                caption=caption,

                parse_mode="HTML",

                reply_markup=home_keyboard(),

            )

            try:
                await query.message.delete()
            except Exception:
                pass

            message_id = sent.message_id

        await start_carousel(

            context,

            chat_id,

            message_id,

            current,

        )

    except Exception:

        traceback.print_exc()


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

        results = list(
            movie_database
        )

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

    results = list(
        reversed(results)
    )[:15]

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

            label += (
                f" ({year})"
            )

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

    user_ai_mode[
        user.id
    ] = True

    user_search_mode[
        user.id
    ] = False

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

    keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )

        ]

    ])

    if update.callback_query:

        query = update.callback_query

        await query.answer()

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

    else:

        await update.message.reply_text(

            text,

            parse_mode="HTML",

            reply_markup=keyboard,

        )


async def ask_gemini(
    user_text
):

    if not gemini_client:

        return (
            "AI service မဖွင့်ထားသေးဘူး 😭"
        )

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

        response = (

            gemini_client
            .models
            .generate_content(

                model=GEMINI_MODEL,

                contents=prompt,

            )

        )

        return (

            response.text

            if response
            and response.text

            else
            "ဟယ်... ငါဘာပြန်ပြောရမလဲ မသိတော့ဘူး 😭"

        )

    except Exception:

        traceback.print_exc()

        return (

            "အခု AI က နည်းနည်းပင်ပန်းနေတယ် 😭\n"
            "ခဏနေ ပြန်မေးနော် ❤️"

        )


# =========================================================
# CHANNEL REQUEST
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

    normalized = normalize_text(
        text
    )

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

    normalized = normalize_text(
        text
    )

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

    normalized = normalize_text(
        text
    )

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
        "နေကောင်းလား"
        in normalized
        or
        "နေကောင်း"
        in normalized
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

    print(
        "📺 Channel post received:",
        message.message_id,
        "Chat:",
        message.chat.username,
    )

    if (

        str(
            message.chat.username or ""
        ).lower()

        !=

        MAIN_CHANNEL.replace(
            "@",
            "",
        ).lower()

    ):

        print(
            "⚠️ Ignored channel:",
            message.chat.username,
        )

        return

    movie = parse_movie_post(
        message
    )

    if not movie:

        print(
            "⚠️ Channel post has no caption/text:",
            message.message_id,
        )

        return

    movie["post_link"] = (

        f"https://t.me/"
        f"{message.chat.username}/"
        f"{message.message_id}"

    )

    existing_index = None

    for i, old_movie in enumerate(
        movie_database
    ):

        if (

            str(
                old_movie.get(
                    "chat_id"
                )
            )

            ==

            str(
                movie.get(
                    "chat_id"
                )
            )

            and

            str(
                old_movie.get(
                    "message_id"
                )
            )

            ==

            str(
                movie.get(
                    "message_id"
                )
            )

        ):

            existing_index = i
            break

    if existing_index is not None:

        old = movie_database[
            existing_index
        ]

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
        "Total:",
        len(movie_database),
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

    await stop_carousel(
        update.effective_chat.id
    )

    normalized = normalize_text(
        text
    )

    if normalized in [
        "home",
        "menu",
        "ပင်မ",
        "မီနူး",
    ]:

        user_search_mode[
            user.id
        ] = False

        user_ai_mode[
            user.id
        ] = False

        await show_home(
            update,
            context,
        )

        return

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

                    label += (
                        f" ({year})"
                    )

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

                label += (
                    f" ({year})"
                )

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

    handled = await handle_special_reply(
        update,
        context,
        text,
    )

    if handled:
        return

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

    await update.message.reply_text(

        "ဟေး 😝❤️\n\n"

        "🎬 Movie ရှာချင်ရင် Movie နာမည်ရိုက်ပါ။\n"

        "🤖 ငါနဲ့စကားပြောချင်ရင် "
        "<b>စကားပြောမယ်</b> လို့ပြော။\n\n"

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

    if query.message:

        await stop_carousel(
            query.message.chat_id
        )

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
# HTTP SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def _send_text(
        self,
        status_code,
        text,
    ):

        self.send_response(
            status_code
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(
                len(
                    text.encode(
                        "utf-8"
                    )
                )
            ),
        )

        self.end_headers()

        self.wfile.write(
            text.encode(
                "utf-8"
            )
        )

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
        ):

            self._send_text(
                200,
                "Movie Myanmar Bot is alive",
            )

            return

        self._send_text(
            404,
            "Not Found",
        )

    def do_POST(self):

        if self.path != WEBHOOK_PATH:

            self._send_text(
                404,
                "Not Found",
            )

            return

        secret = self.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if secret != WEBHOOK_SECRET:

            print(
                "⚠️ Rejected webhook request: bad secret"
            )

            self._send_text(
                403,
                "Forbidden",
            )

            return

        try:

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

        except ValueError:

            content_length = 0

        if content_length <= 0:

            self._send_text(
                400,
                "Empty request",
            )

            return

        try:

            raw_body = self.rfile.read(
                content_length
            )

            data = json.loads(
                raw_body.decode(
                    "utf-8"
                )
            )

        except Exception:

            traceback.print_exc()

            self._send_text(
                400,
                "Invalid JSON",
            )

            return

        try:

            if (
                webhook_application is None
                or webhook_loop is None
            ):

                print(
                    "⚠️ Webhook application is not ready"
                )

                self._send_text(
                    503,
                    "Bot not ready",
                )

                return

            telegram_update = (
                Update.de_json(
                    data=data,
                    bot=webhook_application.bot,
                )
            )

            if telegram_update is None:

                self._send_text(
                    400,
                    "Invalid Telegram update",
                )

                return

            webhook_loop.call_soon_threadsafe(

                webhook_application
                .update_queue
                .put_nowait,

                telegram_update,

            )

            print(
                "📩 Telegram update received:",
                telegram_update.update_id,
            )

            self._send_text(
                200,
                "OK",
            )

        except Exception:

            traceback.print_exc()

            self._send_text(
                500,
                "Webhook processing error",
            )


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

    print(
        f"❤️ Health/Webhook server listening on port {PORT}"
    )

    server.serve_forever()


# =========================================================
# MAIN
# =========================================================

async def main():

    global webhook_application
    global webhook_loop

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

    webhook_application = application

    webhook_loop = (
        asyncio.get_running_loop()
    )

    await application.initialize()

    await application.start()

    threading.Thread(

        target=start_health_server,

        daemon=True,

    ).start()

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

    print(
        "🔐 Webhook secret enabled"
    )

    print(
        f"🎞️ 3-Card 16:9 Carousel: "
        f"Latest {CAROUSEL_LIMIT} movies / "
        f"{CAROUSEL_INTERVAL}s"
    )

    await asyncio.Event().wait()


# =========================================================
# ENTRY POINT
# =========================================================

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
