from pathlib import Path

# Create the complete bot.py from the user's pasted source, with the carousel redesigned
# as a smooth animated GIF using Pillow. Core bot behavior is retained.

code = r'''import os
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
from PIL import Image, ImageDraw, ImageOps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
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

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8072749262").strip()
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]


# =========================================================
# WEBHOOK RUNTIME
# =========================================================

webhook_application = None
webhook_loop = None


# =========================================================
# MOVIE CAROUSEL
# =========================================================

CAROUSEL_LIMIT = 5
CAROUSEL_VISIBLE = 3

# Portrait movie-poster cards
CARD_WIDTH = 210
CARD_HEIGHT = 315
CARD_GAP = 10

# Small side padding gives a cleaner Netflix-like row
SIDE_PADDING = 8
CAROUSEL_BG = (18, 18, 18)

# Smooth slide settings
SLIDE_FRAMES = 18
SLIDE_FRAME_MS = 55
HOLD_MS = 1150

# Active Home carousel messages.
# No endless Telegram edit loop is needed: the GIF loops by itself.
carousel_message_ids = {}

# Cache downloaded Telegram posters
poster_image_cache = {}

# Cache generated GIF while latest-5 set is unchanged
carousel_gif_cache = {
    "signature": None,
    "bytes": None,
}


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
            with open(MOVIE_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                movie_database = data if isinstance(data, list) else []
        else:
            movie_database = []
    except Exception:
        movie_database = []


def save_movies():
    try:
        with open(MOVIE_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(movie_database, f, ensure_ascii=False, indent=2)
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
                ai_usage = data if isinstance(data, dict) else {}
    except Exception:
        ai_usage = {}


def save_ai_usage():
    try:
        with open(AI_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(ai_usage, f, ensure_ascii=False, indent=2)
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
    "Action": ["action", "အက်ရှင်"],
    "Comedy": ["comedy", "ဟာသ"],
    "Romance": ["romance", "romantic", "အချစ်"],
    "Horror": ["horror", "ကြောက်", "သရဲ"],
    "Thriller": ["thriller"],
    "Drama": ["drama", "ဒရာမာ"],
    "Fantasy": ["fantasy"],
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
    text = re.sub(r"[\[\]\(\)\{\}:,._\-|/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_year(text):
    if not text:
        return None

    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else None


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
    content = caption if caption else text

    if not content:
        return None

    lines = [line.strip() for line in content.splitlines() if line.strip()]

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

    return {
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


def build_movie_link(movie):
    chat_id = str(movie.get("chat_id", ""))
    message_id = movie.get("message_id")

    if not message_id:
        return movie.get("post_link", MOVIE_CHANNEL_URL)

    if chat_id.startswith("-100"):
        internal_id = chat_id[4:]
        return f"https://t.me/c/{internal_id}/{message_id}"

    return movie.get("post_link", MOVIE_CHANNEL_URL)


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
        title = normalize_text(movie.get("title", ""))
        original_name = normalize_text(movie.get("original_name", ""))
        description = normalize_text(movie.get("description", ""))

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
            score = SequenceMatcher(None, query, title).ratio()

        if query in description:
            score = max(score, 0.72)

        if year and str(movie.get("year", "")) == year:
            score += 0.08

        if score >= 0.68:
            results.append((score, movie))

    results.sort(key=lambda x: x[0], reverse=True)
    return [movie for score, movie in results]


def get_latest_movies(limit=10):
    movies = list(movie_database)
    movies.reverse()
    return movies[:limit]


# =========================================================
# CAROUSEL MOVIES
# =========================================================

def get_carousel_movies(limit=CAROUSEL_LIMIT):
    movies = [
        movie
        for movie in reversed(movie_database)
        if movie.get("poster_file_id")
    ]
    return movies[:limit]


def movie_identity(movie):
    if not movie:
        return ""

    return (
        str(movie.get("chat_id", ""))
        + ":"
        + str(movie.get("message_id", ""))
    )


def carousel_signature(movies):
    return "|".join(movie_identity(movie) for movie in movies)


def invalidate_carousel_cache():
    carousel_gif_cache["signature"] = None
    carousel_gif_cache["bytes"] = None


# =========================================================
# POSTER IMAGE HELPERS
# =========================================================

async def get_poster_image(bot, file_id):
    if not file_id:
        return None

    if file_id in poster_image_cache:
        try:
            return poster_image_cache[file_id].copy()
        except Exception:
            pass

    try:
        telegram_file = await bot.get_file(file_id)
        image_bytes = await telegram_file.download_as_bytearray()

        image = Image.open(
            io.BytesIO(bytes(image_bytes))
        ).convert("RGB")

        poster_image_cache[file_id] = image.copy()
        return image

    except Exception:
        traceback.print_exc()
        return None


def fit_poster(image, width, height):
    """
    Preserve the WHOLE poster. No 16:9 crop.
    The poster is fitted inside a portrait card and centered.
    """
    image = image.convert("RGB")

    fitted = ImageOps.contain(
        image,
        (width, height),
        method=Image.Resampling.LANCZOS,
    )

    card = Image.new(
        "RGB",
        (width, height),
        (8, 8, 8),
    )

    x = (width - fitted.width) // 2
    y = (height - fitted.height) // 2
    card.paste(fitted, (x, y))

    # Rounded-corner presentation.
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=12,
        fill=255,
    )

    rounded = Image.new("RGB", (width, height), CAROUSEL_BG)
    rounded.paste(card, (0, 0), mask)
    return rounded


async def prepare_carousel_cards(context, movies):
    cards = []

    for movie in movies:
        poster = await get_poster_image(
            context.bot,
            movie.get("poster_file_id"),
        )

        if poster is None:
            placeholder = Image.new(
                "RGB",
                (CARD_WIDTH, CARD_HEIGHT),
                (35, 35, 35),
            )
            cards.append(placeholder)
            continue

        cards.append(
            fit_poster(
                poster,
                CARD_WIDTH,
                CARD_HEIGHT,
            )
        )

    return cards


def ease_in_out(progress):
    """
    Smoothstep: slower at start/end, smooth in the middle.
    """
    return progress * progress * (3.0 - 2.0 * progress)


async def create_carousel_gif(context, movies, start_index=0):
    if not movies:
        return None

    # Cache only the normal newest-first Home animation.
    signature = carousel_signature(movies)

    if (
        start_index == 0
        and carousel_gif_cache.get("signature") == signature
        and carousel_gif_cache.get("bytes")
    ):
        return carousel_gif_cache["bytes"]

    count = len(movies)
    start_index %= count

    ordered_movies = [
        movies[(start_index + i) % count]
        for i in range(count)
    ]

    cards = await prepare_carousel_cards(
        context,
        ordered_movies,
    )

    if not cards:
        return None

    # Repeat cards enough for seamless wrap-around.
    repeated_cards = cards * 3

    step = CARD_WIDTH + CARD_GAP

    strip_width = (
        len(repeated_cards) * CARD_WIDTH
        + (len(repeated_cards) - 1) * CARD_GAP
    )

    strip = Image.new(
        "RGB",
        (strip_width, CARD_HEIGHT),
        CAROUSEL_BG,
    )

    for index, card in enumerate(repeated_cards):
        x = index * step
        strip.paste(card, (x, 0))

    viewport_width = (
        SIDE_PADDING * 2
        + CARD_WIDTH * CAROUSEL_VISIBLE
        + CARD_GAP * (CAROUSEL_VISIBLE - 1)
    )

    viewport_height = CARD_HEIGHT

    frames = []
    durations = []

    transitions = count if count > 1 else 1

    for transition in range(transitions):
        base_offset = transition * step

        # Hold the current 3-poster view before sliding.
        hold_frame = Image.new(
            "RGB",
            (viewport_width, viewport_height),
            CAROUSEL_BG,
        )

        crop = strip.crop(
            (
                base_offset,
                0,
                base_offset + viewport_width - SIDE_PADDING * 2,
                CARD_HEIGHT,
            )
        )

        hold_frame.paste(crop, (SIDE_PADDING, 0))
        frames.append(hold_frame)
        durations.append(HOLD_MS)

        if count <= 1:
            continue

        # Smoothly move exactly one poster-card to the left.
        for frame_number in range(1, SLIDE_FRAMES + 1):
            progress = frame_number / SLIDE_FRAMES
            eased = ease_in_out(progress)
            offset = int(base_offset + step * eased)

            frame = Image.new(
                "RGB",
                (viewport_width, viewport_height),
                CAROUSEL_BG,
            )

            moving_crop = strip.crop(
                (
                    offset,
                    0,
                    offset + viewport_width - SIDE_PADDING * 2,
                    CARD_HEIGHT,
                )
            )

            frame.paste(
                moving_crop,
                (SIDE_PADDING, 0),
            )

            frames.append(frame)
            durations.append(SLIDE_FRAME_MS)

    if not frames:
        return None

    output = io.BytesIO()

    # Palette conversion reduces GIF size substantially.
    gif_frames = [
        frame.quantize(
            colors=128,
            method=Image.Quantize.MEDIANCUT,
        )
        for frame in frames
    ]

    gif_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=gif_frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )

    output.seek(0)
    gif_bytes = output.getvalue()

    if start_index == 0:
        carousel_gif_cache["signature"] = signature
        carousel_gif_cache["bytes"] = gif_bytes

    return gif_bytes


def carousel_caption(movies, start_index=0):
    if not movies:
        return home_caption()

    # Show the latest three names in the text, while the GIF
    # continuously rotates all five posters.
    visible = []

    for offset in range(min(CAROUSEL_VISIBLE, len(movies))):
        movie = movies[
            (start_index + offset)
            % len(movies)
        ]
        visible.append(
            movie.get("title", "Movie")
        )

    return (
        "🎬 <b>MOVIE MYANMAR</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "☔ <b>Xiao Yu CyB (小雨)</b> ❤️\n\n"
        "🎞️ "
        + "  •  ".join(visible)
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

async def stop_carousel(chat_id):
    carousel_message_ids.pop(chat_id, None)


async def start_carousel(context, chat_id, message_id, start_index=0):
    carousel_message_ids[chat_id] = message_id


async def refresh_active_carousels(context):
    """
    When a new channel movie is posted, rebuild the latest-5 GIF
    and refresh users who are still sitting on the Home carousel.
    """
    if not carousel_message_ids:
        return

    movies = get_carousel_movies()

    if not movies:
        return

    invalidate_carousel_cache()

    gif_bytes = await create_carousel_gif(
        context,
        movies,
        0,
    )

    if not gif_bytes:
        return

    caption = carousel_caption(movies, 0)

    for chat_id, message_id in list(carousel_message_ids.items()):
        try:
            await context.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaAnimation(
                    media=InputFile(
                        io.BytesIO(gif_bytes),
                        filename="movie_carousel.gif",
                    ),
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=home_keyboard(),
            )
        except Exception as e:
            error_text = str(e).lower()

            if "message is not modified" in error_text:
                continue

            if any(
                value in error_text
                for value in [
                    "message to edit not found",
                    "message can't be edited",
                    "chat not found",
                    "forbidden",
                ]
            ):
                carousel_message_ids.pop(chat_id, None)
                continue

            traceback.print_exc()


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
# HOME CAPTION
# =========================================================

def home_caption(movie=None):
    if movie:
        title = movie.get("title", "Latest Movie")
        year = movie.get("year", "")
        quality = movie.get("quality", "")
        audio = movie.get("audio", "")

        movie_info = title

        if year:
            movie_info += f" • {year}"
        if quality:
            movie_info += f" • {quality}"
        if audio:
            movie_info += f" • {audio}"

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

async def show_home(update, context, poster_index=0):
    movies = get_carousel_movies()
    keyboard = home_keyboard()

    if update.callback_query:
        query = update.callback_query
        await query.answer()

        message = query.message
        chat_id = message.chat_id

        await stop_carousel(chat_id)

        if movies:
            start_index = poster_index % len(movies)

            gif_bytes = await create_carousel_gif(
                context,
                movies,
                start_index,
            )

            if gif_bytes:
                caption = carousel_caption(
                    movies,
                    start_index,
                )

                try:
                    # Editing photo -> animation may fail on some Telegram
                    # message types, so fall back to sending a fresh animation.
                    try:
                        await message.edit_media(
                            media=InputMediaAnimation(
                                media=InputFile(
                                    io.BytesIO(gif_bytes),
                                    filename="movie_carousel.gif",
                                ),
                                caption=caption,
                                parse_mode="HTML",
                            ),
                            reply_markup=keyboard,
                        )

                        message_id = message.message_id

                    except Exception:
                        sent = await context.bot.send_animation(
                            chat_id=chat_id,
                            animation=InputFile(
                                io.BytesIO(gif_bytes),
                                filename="movie_carousel.gif",
                            ),
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )

                        try:
                            await message.delete()
                        except Exception:
                            pass

                        message_id = sent.message_id

                    context.user_data["poster_index"] = start_index

                    await start_carousel(
                        context,
                        chat_id,
                        message_id,
                        start_index,
                    )
                    return

                except Exception:
                    traceback.print_exc()

        try:
            await context.bot.send_photo(
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

    chat = update.effective_chat

    await stop_carousel(chat.id)

    if movies:
        start_index = poster_index % len(movies)

        gif_bytes = await create_carousel_gif(
            context,
            movies,
            start_index,
        )

        if gif_bytes:
            try:
                sent = await chat.send_animation(
                    animation=InputFile(
                        io.BytesIO(gif_bytes),
                        filename="movie_carousel.gif",
                    ),
                    caption=carousel_caption(
                        movies,
                        start_index,
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

                context.user_data["poster_index"] = start_index

                await start_carousel(
                    context,
                    chat.id,
                    sent.message_id,
                    start_index,
                )
                return

            except Exception:
                traceback.print_exc()

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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await show_home(update, context)


# =========================================================
# JOIN CHECK
# =========================================================

async def check_join_callback(update, context):
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

    await stop_carousel(query.message.chat_id)

    await query.message.edit_text(
        "✅ <b>Join အောင်မြင်ပြီ!</b>\n\n"
        "🎬 Movie Myanmar ထဲကို "
        "ဝင်လို့ရပါပြီ ❤️",
        parse_mode="HTML",
    )

    await asyncio.sleep(1)
    await show_home(update, context)


# =========================================================
# SEARCH
# =========================================================

async def start_search(update, context):
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
# LATEST
# =========================================================

async def show_latest_movies(update, context):
    query = update.callback_query
    await query.answer()

    latest = get_latest_movies(15)

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
        title = movie.get("title", "Unknown Movie")
        year = movie.get("year", "")

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
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# =========================================================
# NEXT MOVIE
# =========================================================

async def next_poster(update, context):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    await stop_carousel(chat_id)

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

    current = (current + 1) % len(movies)
    context.user_data["poster_index"] = current

    gif_bytes = await create_carousel_gif(
        context,
        movies,
        current,
    )

    if not gif_bytes:
        return

    caption = carousel_caption(
        movies,
        current,
    )

    try:
        try:
            await query.message.edit_media(
                media=InputMediaAnimation(
                    media=InputFile(
                        io.BytesIO(gif_bytes),
                        filename="movie_carousel.gif",
                    ),
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=home_keyboard(),
            )

            message_id = query.message.message_id

        except Exception:
            sent = await context.bot.send_animation(
                chat_id=chat_id,
                animation=InputFile(
                    io.BytesIO(gif_bytes),
                    filename="movie_carousel.gif",
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

async def show_categories(update, context):
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


async def show_genre(update, context):
    query = update.callback_query
    await query.answer()

    genre = query.data.replace("genre_", "")

    if genre == "All":
        results = list(movie_database)
    else:
        results = [
            movie
            for movie in movie_database
            if genre in movie.get("genres", [])
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
        title = movie.get("title", "Unknown")
        year = movie.get("year", "")

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
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# =========================================================
# HELP
# =========================================================

async def show_help(update, context):
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


async def start_ai(update, context):
    user = update.effective_user

    user_ai_mode[user.id] = True
    user_search_mode[user.id] = False

    used = get_ai_usage(user.id)
    remaining = max(0, FREE_AI_LIMIT - used)

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


async def ask_gemini(user_text):
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

async def handle_special_reply(update, context, text):
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

    if "နေကောင်းလား" in normalized or "နေကောင်း" in normalized:
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
        str(message.chat.username or "").lower()
        != MAIN_CHANNEL.replace("@", "").lower()
    ):
        print(
            "⚠️ Ignored channel:",
            message.chat.username,
        )
        return

    movie = parse_movie_post(message)

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

    for i, old_movie in enumerate(movie_database):
        if (
            str(old_movie.get("chat_id"))
            == str(movie.get("chat_id"))
            and str(old_movie.get("message_id"))
            == str(movie.get("message_id"))
        ):
            existing_index = i
            break

    if existing_index is not None:
        old = movie_database[existing_index]

        if not movie.get("poster_file_id"):
            movie["poster_file_id"] = old.get(
                "poster_file_id"
            )

        movie_database[existing_index] = movie

    else:
        movie_database.append(movie)

    save_movies()

    # Latest-5 Home carousel must now use the newly posted movie.
    invalidate_carousel_cache()

    print(
        "🎬 Movie saved:",
        movie.get("title"),
        "Poster:",
        bool(movie.get("poster_file_id")),
        "Total:",
        len(movie_database),
    )

    # Refresh only users who are still on Home.
    try:
        await refresh_active_carousels(context)
    except Exception:
        traceback.print_exc()


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

    text = (update.message.text or "").strip()

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

    normalized = normalize_text(text)

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

    if is_channel_request(text):
        await update.message.reply_text(
            "📢 <b>Movie Myanmar Channel</b>\n\n"
            "👇 ဒီမှာ ဝင်လို့ရတယ် ❤️",
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )
        return

    if user_search_mode.get(user.id, False):
        results = movie_search(text)

        if results:
            results = results[:15]
            user_last_results[user.id] = results
            buttons = []

            for movie in results:
                title = movie.get("title", "Unknown")
                year = movie.get("year", "")

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

            await update.message.reply_text(
                f"🎬 <b>Found {len(results)} Movie(s)</b>\n\n"
                "👇 ကြည့်ချင်တဲ့ကားကို ရွေးပါ။",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
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

        user_search_mode[user.id] = False
        return

    results = movie_search(text)

    if results:
        results = results[:15]
        user_last_results[user.id] = results
        buttons = []

        for movie in results:
            title = movie.get("title", "Unknown")
            year = movie.get("year", "")

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

        await update.message.reply_text(
            f"🎬 <b>{len(results)} Movie(s) Found</b>\n\n"
            "👇 ကြည့်ချင်တဲ့ကားကို ရွေးပါ။",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if has_movie_intent(text):
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
        user_ai_mode[user.id] = True

    if user_ai_mode.get(user.id, False):
        used = get_ai_usage(user.id)

        if used >= FREE_AI_LIMIT:
            await update.message.reply_text(
                movie_fallback(),
                parse_mode="HTML",
            )
            return

        reply = await ask_gemini(text)

        if (
            reply
            and not reply.startswith("အခု AI က")
            and not reply.startswith("AI service")
        ):
            increase_ai_usage(user.id)

        remaining = max(
            0,
            FREE_AI_LIMIT - get_ai_usage(user.id),
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
        context.user_data["poster_index"] = 0

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

    if data.startswith("genre_"):
        await show_genre(
            update,
            context,
        )
        return

    await query.answer()


# =========================================================
# HTTP SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def _send_text(self, status_code, text):
        self.send_response(status_code)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(text.encode("utf-8"))),
        )
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/health"):
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
                raw_body.decode("utf-8")
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

            telegram_update = Update.de_json(
                data=data,
                bot=webhook_application.bot,
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

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
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
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    webhook_application = application
    webhook_loop = asyncio.get_running_loop()

    await application.initialize()
    await application.start()

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    webhook_url = RENDER_URL + WEBHOOK_PATH

    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message",
            "callback_query",
            "channel_post",
        ],
    )

    print("🎬 Movie Myanmar Bot started")
    print("🌐 Webhook:", webhook_url)
    print("🔐 Webhook secret enabled")
    print(
        f"🎞️ Smooth Portrait Carousel: "
        f"Latest {CAROUSEL_LIMIT} movies / "
        f"{CAROUSEL_VISIBLE} visible"
    )

    await asyncio.Event().wait()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Bot stopped.")

    except Exception:
        traceback.print_exc()
'''

path = Path("/mnt/data/bot.py")
path.write_text(code, encoding="utf-8")

# compile check
compile(code, str(path), "exec")

print(f"Created {path}")
print(f"Lines: {len(code.splitlines())}")
print(f"Size: {path.stat().st_size / 1024:.1f} KB")
print("Python syntax check: OK")
