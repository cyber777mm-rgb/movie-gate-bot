# Movie Myanmar - single-file bot + embedded Mini App
# Gate + Telegram Mini App Auth + Loading/Wake Recovery

import os
import asyncio
import base64
import hashlib
import hmac
import json
import threading
import re
import traceback
import html
import time
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from google import genai

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
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
    WebAppInfo = None
    MenuButtonWebApp = None


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MAIN_CHANNEL = "@moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"

MOVIE_STORAGE_CHAT_ID = os.getenv(
    "MOVIE_STORAGE_CHAT_ID",
    "",
).strip()

RENDER_URL = "https://movie-gate-bot.onrender.com"
WEBHOOK_PATH = "/telegram-webhook"

ADMIN_CHAT_ID = os.getenv(
    "ADMIN_CHAT_ID",
    "8072749262",
).strip()

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode("utf-8")
).hexdigest()[:32]

FREE_AI_LIMIT = 20

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash",
).strip()

WELCOME_IMAGE = (
    "https://raw.githubusercontent.com/"
    "cyber777mm-rgb/movie-gate-bot/main/"
    "ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"
)

# Telegram Mini App initData should not be kept valid forever.
# This is only an anti-replay protection; membership is checked
# separately on every protected API request.
WEBAPP_AUTH_MAX_AGE = 24 * 60 * 60

# How many times the Mini App retries while Render wakes up.
MINI_APP_RETRIES = 8
MINI_APP_RETRY_DELAY = 1500


# ============================================================
# PERSISTENT DATA DIRECTORY
# ============================================================

REQUESTED_DATA_DIR = os.getenv(
    "MOVIE_DATA_DIR",
    "/data",
).strip()

try:
    os.makedirs(
        REQUESTED_DATA_DIR,
        exist_ok=True,
    )
    DATA_DIR = REQUESTED_DATA_DIR
except Exception:
    DATA_DIR = os.path.abspath("./data")
    os.makedirs(
        DATA_DIR,
        exist_ok=True,
    )

print("Movie data directory:", DATA_DIR)

MOVIE_DB_FILE = os.path.join(
    DATA_DIR,
    "movies.json",
)

AI_USAGE_FILE = os.path.join(
    DATA_DIR,
    "ai_usage.json",
)

STORAGE_CHANNEL_FILE = os.path.join(
    DATA_DIR,
    "storage_channel.json",
)

uploader_sessions = {}
album_buffers = {}
album_tasks = {}
published_album_ids = set()


# ============================================================
# STORAGE CHANNEL
# ============================================================

def load_storage_channel_id():
    if MOVIE_STORAGE_CHAT_ID:
        return MOVIE_STORAGE_CHAT_ID

    try:
        with open(
            STORAGE_CHANNEL_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return str(
            data.get("chat_id", "")
        ).strip()

    except Exception:
        return ""


def save_storage_channel_id(chat_id):
    chat_id = str(chat_id or "").strip()

    if not chat_id or MOVIE_STORAGE_CHAT_ID:
        return

    try:
        with open(
            STORAGE_CHANNEL_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {"chat_id": chat_id},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(
            "Storage channel save error:",
            e,
        )


AUTO_STORAGE_CHAT_ID = load_storage_channel_id()


def get_storage_channel_id():
    return (
        MOVIE_STORAGE_CHAT_ID
        or AUTO_STORAGE_CHAT_ID
        or ""
    ).strip()


def is_admin_user(user_id):
    return str(user_id) == str(ADMIN_CHAT_ID)


def clear_uploader(user_id):
    uploader_sessions.pop(
        str(user_id),
        None,
    )


def uploader_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚀 PUBLISH",
                    callback_data="uploader_publish",
                ),
                InlineKeyboardButton(
                    "❌ CANCEL",
                    callback_data="uploader_cancel",
                ),
            ]
        ]
    )


# ============================================================
# GITHUB MOVIE BACKUP
# ============================================================

GITHUB_TOKEN = os.getenv(
    "GITHUB_TOKEN",
    "",
).strip()

GITHUB_REPO = os.getenv(
    "GITHUB_REPO",
    "cyber777mm-rgb/movie-myanmar-data",
).strip().strip("/")

GITHUB_MOVIE_FILE = os.getenv(
    "GITHUB_MOVIE_FILE",
    "movies.json",
).strip().lstrip("/")

GITHUB_BRANCH = os.getenv(
    "GITHUB_BRANCH",
    "main",
).strip() or "main"

LEGACY_GITHUB_REPO = (
    "cyber777mm-rgb/movie-gate-bot"
)

GITHUB_API_BASE = "https://api.github.com"

github_sync_lock = threading.RLock()


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "movie-myanmar-bot",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


def github_enabled():
    return bool(
        GITHUB_TOKEN
        and GITHUB_REPO
        and GITHUB_MOVIE_FILE
    )


def github_get_movies_from_repo(repo):
    if not (
        GITHUB_TOKEN
        and repo
        and GITHUB_MOVIE_FILE
    ):
        return [], None

    url = (
        f"{GITHUB_API_BASE}/repos/{repo}/contents/"
        f"{GITHUB_MOVIE_FILE}?ref={GITHUB_BRANCH}"
    )

    req = Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    with urlopen(
        req,
        timeout=10,
    ) as response:

        payload = json.loads(
            response.read().decode(
                "utf-8"
            )
        )

    encoded = payload.get(
        "content",
        "",
    ).replace("\n", "")

    if not encoded:
        return [], payload.get("sha")

    raw = base64.b64decode(
        encoded
    ).decode("utf-8")

    data = json.loads(raw)

    if not isinstance(data, list):
        data = []

    return data, payload.get("sha")


def github_get_movies():
    if not github_enabled():
        return [], None

    repos = [GITHUB_REPO]

    if LEGACY_GITHUB_REPO not in repos:
        repos.append(
            LEGACY_GITHUB_REPO
        )

    errors = []

    for repo in repos:
        try:
            movies, sha = (
                github_get_movies_from_repo(
                    repo
                )
            )

            if isinstance(
                movies,
                list,
            ):
                print(
                    "GitHub movie load OK:",
                    len(movies),
                    "movies from",
                    repo,
                )

                return movies, sha

        except Exception as e:
            errors.append(
                f"{repo}: {e}"
            )

    print(
        "GitHub movie load failed:",
        " | ".join(errors)
        if errors
        else "no repositories",
    )

    return [], None


def github_put_movies(movies):
    if not github_enabled():
        return False

    with github_sync_lock:

        repos = [GITHUB_REPO]

        if LEGACY_GITHUB_REPO not in repos:
            repos.append(
                LEGACY_GITHUB_REPO
            )

        payload_text = (
            json.dumps(
                movies,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

        encoded = base64.b64encode(
            payload_text.encode(
                "utf-8"
            )
        ).decode("ascii")

        last_error = None

        for repo in repos:

            try:
                _, sha = (
                    github_get_movies_from_repo(
                        repo
                    )
                )

                url = (
                    f"{GITHUB_API_BASE}/repos/"
                    f"{repo}/contents/"
                    f"{GITHUB_MOVIE_FILE}"
                )

                body = {
                    "message": (
                        "Update Movie Myanmar "
                        "movie database"
                    ),
                    "content": encoded,
                    "branch": GITHUB_BRANCH,
                }

                if sha:
                    body["sha"] = sha

                req = Request(
                    url,
                    data=json.dumps(
                        body
                    ).encode("utf-8"),
                    headers={
                        **github_headers(),
                        "Content-Type":
                            "application/json",
                    },
                    method="PUT",
                )

                with urlopen(
                    req,
                    timeout=15,
                ) as response:
                    response.read()

                print(
                    "GitHub movie backup saved:",
                    len(movies),
                    "movies to",
                    repo,
                )

                return True

            except Exception as e:
                last_error = e

                print(
                    "GitHub backup repo failed:",
                    repo,
                    e,
                )

        print(
            "GitHub movie backup failed:",
            last_error,
        )

        return False


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

movie_lock = threading.RLock()
ai_lock = threading.RLock()


# ============================================================
# GENRES
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
# CAROUSEL
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

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            "load_json error:",
            path,
            e,
        )
        return default


def save_json(path, data):
    temp_path = path + ".tmp"

    try:
        os.makedirs(
            os.path.dirname(path) or ".",
            exist_ok=True,
        )

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

            f.flush()
            os.fsync(
                f.fileno()
            )

        os.replace(
            temp_path,
            path,
        )

    except Exception as e:

        print(
            "save error:",
            path,
            e,
        )

        try:
            if os.path.exists(
                temp_path
            ):
                os.remove(
                    temp_path
                )
        except Exception:
            pass


# ============================================================
# MOVIE ID
# ============================================================

def generate_movie_id(
    chat_id,
    message_id,
):
    raw = (
        f"{chat_id}:{message_id}"
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def ensure_movie_id(movie):
    existing = str(
        movie.get(
            "id",
            "",
        )
    ).strip()

    if existing:
        return existing

    chat_id = movie.get(
        "chat_id",
        "",
    )

    message_id = movie.get(
        "message_id",
        "",
    )

    if chat_id and message_id:
        movie["id"] = (
            generate_movie_id(
                chat_id,
                message_id,
            )
        )

    else:
        raw = json.dumps(
            movie,
            ensure_ascii=False,
            sort_keys=True,
        )

        movie["id"] = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:16]

    return movie["id"]


def merge_movie_lists(
    primary,
    secondary,
):
    """
    Merge movie databases by stable identity.
    Primary wins for duplicate records.
    """

    result = []
    seen = set()

    for source in (
        primary,
        secondary,
    ):

        if not isinstance(
            source,
            list,
        ):
            continue

        for movie in source:

            if not isinstance(
                movie,
                dict,
            ):
                continue

            ensure_movie_id(movie)

            movie_id = str(
                movie.get(
                    "id",
                    "",
                )
            )

            if movie_id in seen:
                continue

            seen.add(
                movie_id
            )

            result.append(
                movie
            )

    return result


# ============================================================
# OLD DATA MIGRATION
# ============================================================

def migrate_old_data():

    old_movie_file = os.path.abspath(
        "movies.json"
    )

    old_ai_file = os.path.abspath(
        "ai_usage.json"
    )

    current_movie_file = os.path.abspath(
        MOVIE_DB_FILE
    )

    current_ai_file = os.path.abspath(
        AI_USAGE_FILE
    )

    if old_movie_file != current_movie_file:

        try:
            if os.path.exists(
                old_movie_file
            ):

                old_movies = load_json(
                    old_movie_file,
                    [],
                )

                new_movies = load_json(
                    MOVIE_DB_FILE,
                    [],
                )

                if (
                    isinstance(
                        old_movies,
                        list,
                    )
                    and isinstance(
                        new_movies,
                        list,
                    )
                ):

                    merged = merge_movie_lists(
                        new_movies,
                        old_movies,
                    )

                    if merged:
                        save_json(
                            MOVIE_DB_FILE,
                            merged,
                        )

                        print(
                            "Movie data migration:",
                            len(merged),
                        )

        except Exception:
            print(
                "Movie migration error:"
            )
            print(
                traceback.format_exc()
            )

    if old_ai_file != current_ai_file:

        try:
            if os.path.exists(
                old_ai_file
            ):

                old_usage = load_json(
                    old_ai_file,
                    {},
                )

                new_usage = load_json(
                    AI_USAGE_FILE,
                    {},
                )

                if not isinstance(
                    old_usage,
                    dict,
                ):
                    old_usage = {}

                if not isinstance(
                    new_usage,
                    dict,
                ):
                    new_usage = {}

                merged_usage = dict(
                    old_usage
                )

                merged_usage.update(
                    new_usage
                )

                if merged_usage:
                    save_json(
                        AI_USAGE_FILE,
                        merged_usage,
                    )

        except Exception:
            print(
                "AI usage migration error:"
            )
            print(
                traceback.format_exc()
            )


# ============================================================
# NORMALIZE DATABASE
# ============================================================

def normalize_movie_database():

    changed = False

    with movie_lock:

        for movie in movie_database:

            if not isinstance(
                movie,
                dict,
            ):
                continue

            old_id = movie.get(
                "id"
            )

            ensure_movie_id(
                movie
            )

            if movie.get(
                "id"
            ) != old_id:
                changed = True

            if not isinstance(
                movie.get(
                    "genres"
                ),
                list,
            ):
                movie["genres"] = []
                changed = True

            if not isinstance(
                movie.get(
                    "description"
                ),
                str,
            ):
                movie["description"] = str(
                    movie.get(
                        "description",
                        "",
                    )
                    or ""
                )

                changed = True

    return changed


# ============================================================
# MOVIE DATABASE LOAD
# ============================================================

def load_movies():

    global movie_database

    local_movies = load_json(
        MOVIE_DB_FILE,
        [],
    )

    if not isinstance(
        local_movies,
        list,
    ):
        local_movies = []

    remote_movies = []

    if github_enabled():

        try:
            remote_movies, _ = (
                github_get_movies()
            )

            if not isinstance(
                remote_movies,
                list,
            ):
                remote_movies = []

        except Exception:
            remote_movies = []

    # IMPORTANT:
    # Never replace valid local records with an empty GitHub response.
    if remote_movies:

        movie_database = merge_movie_lists(
            remote_movies,
            local_movies,
        )

    elif local_movies:

        movie_database = merge_movie_lists(
            local_movies,
            [],
        )

    else:

        movie_database = []

    normalize_movie_database()

    with movie_lock:
        snapshot = [
            dict(movie)
            for movie in movie_database
        ]

    save_json(
        MOVIE_DB_FILE,
        snapshot,
    )

    # IMPORTANT:
    # No GitHub write here.
    # Startup/restart must never create a blank GitHub backup.

    print(
        "Loaded movies:",
        len(movie_database),
        "from",
        MOVIE_DB_FILE,
        "GitHub:",
        "ON" if github_enabled() else "OFF",
    )

    if github_enabled() and not remote_movies:

        print(
            "GitHub returned no records; "
            "local movies preserved:",
            len(local_movies),
        )


def save_movies():

    with movie_lock:

        snapshot = [
            dict(movie)
            for movie in movie_database
            if isinstance(
                movie,
                dict,
            )
        ]

    save_json(
        MOVIE_DB_FILE,
        snapshot,
    )

    if not github_enabled():
        return

    try:

        remote_movies, _ = (
            github_get_movies()
        )

        if not isinstance(
            remote_movies,
            list,
        ):
            remote_movies = []

        merged = merge_movie_lists(
            snapshot,
            remote_movies,
        )

        if not github_put_movies(
            merged
        ):

            print(
                "WARNING: Local movie save OK, "
                "GitHub backup failed."
            )

    except Exception:

        print(
            "GitHub movie backup error:"
        )

        print(
            traceback.format_exc()
        )


# ============================================================
# AI USAGE
# ============================================================

def load_ai_usage():

    global ai_usage

    data = load_json(
        AI_USAGE_FILE,
        {},
    )

    ai_usage = (
        data
        if isinstance(
            data,
            dict,
        )
        else {}
    )

    print(
        "Loaded AI usage:",
        len(ai_usage),
    )


def save_ai_usage():

    with ai_lock:
        snapshot = dict(
            ai_usage
        )

    save_json(
        AI_USAGE_FILE,
        snapshot,
    )


def get_ai_usage(uid):

    with ai_lock:

        try:
            return int(
                ai_usage.get(
                    str(uid),
                    0,
                )
            )

        except Exception:
            return 0


def increase_ai_usage(uid):

    with ai_lock:

        ai_usage[str(uid)] = (
            get_ai_usage(uid) + 1
        )

    save_ai_usage()


migrate_old_data()
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

        print(
            "Gemini initialized:",
            GEMINI_MODEL,
        )

    except Exception as e:

        print(
            "Gemini init error:",
            e,
        )


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(s):

    s = (
        s or ""
    ).lower()

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

    return (
        m.group(1)
        if m
        else ""
    )


def detect_genres(s):

    n = normalize_text(s)

    return [
        genre
        for genre, words
        in GENRE_KEYWORDS.items()
        if any(
            normalize_text(word)
            in n
            for word in words
        )
    ]


# ============================================================
# MOVIE PARSER
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

    year = extract_year(
        text
    )

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
        "genres": detect_genres(
            text
        ),
        "description": text,
        "post_link": "",
        "chat_id": str(
            message.chat_id
        ),
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

    post_link = str(
        movie.get(
            "post_link",
            "",
        )
        or ""
    ).strip()

    if post_link:
        return post_link

    chat = str(
        movie.get(
            "chat_id",
            "",
        )
    )

    mid = movie.get(
        "message_id"
    )

    if (
        chat.startswith("-100")
        and mid
    ):

        return (
            f"https://t.me/c/"
            f"{chat[4:]}/{mid}"
        )

    return MOVIE_CHANNEL_URL


# ============================================================
# LOOKUP
# ============================================================

def find_movie_by_id(movie_id):

    movie_id = str(
        movie_id
    )

    with movie_lock:

        for movie in movie_database:

            if str(
                movie.get(
                    "id",
                    "",
                )
            ) == movie_id:

                return movie

    return None


def movie_search(q):

    qn = normalize_text(
        q
    )

    if not qn:
        return []

    year = extract_year(
        q
    )

    scored = []

    with movie_lock:
        movies = list(
            movie_database
        )

    for movie in movies:

        hay = normalize_text(
            " ".join(
                [
                    str(
                        movie.get(
                            "title",
                            "",
                        )
                    ),
                    str(
                        movie.get(
                            "original_name",
                            "",
                        )
                    ),
                    str(
                        movie.get(
                            "year",
                            "",
                        )
                    ),
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

        if qn == normalize_text(
            movie.get(
                "title",
                "",
            )
        ):
            score += 100

        if qn in hay:
            score += 60

        if (
            year
            and year
            == str(
                movie.get(
                    "year",
                    "",
                )
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

    with movie_lock:

        return list(
            reversed(
                movie_database[
                    -limit:
                ]
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
                        url=(
                            RENDER_URL
                            + "/app"
                        )
                    ),
                )
            ],
        )

    return InlineKeyboardMarkup(
        rows
    )


def category_keyboard():

    genres = list(
        GENRE_KEYWORDS.keys()
    )

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
                callback_data=(
                    "genre:"
                    + genres[i]
                ),
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

        rows.append(
            row
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        rows
    )


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

    except Exception as e:

        print(
            "membership check error:",
            e,
        )

        return False


async def stop_carousel(chat_id):

    carousel_message_ids.pop(
        chat_id,
        None,
    )


# ============================================================
# HOME
# ============================================================

async def show_home(
    update,
    context,
):

    query = update.callback_query

    if query:

        try:
            await query.answer()
        except Exception:
            pass

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


async def start_command(
    update,
    context,
):

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


async def check_join(
    update,
    context,
):

    query = update.callback_query

    if await is_member(
        context.bot,
        query.from_user.id,
    ):

        await query.answer()

        try:

            await query.edit_message_text(
                "✅ ဝင်ပြီးပြီဟေ့ 😝 "
                "ခဏ... Movie Myanmar ဖွင့်ပေးမယ်။"
            )

        except Exception:
            pass

        await asyncio.sleep(
            0.8
        )

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

async def start_search(
    update,
    context,
):

    query = update.callback_query

    if query:

        await query.answer()
        message = query.message

    else:

        message = (
            update.effective_message
        )

    user_search_mode[
        update.effective_user.id
    ] = True

    user_ai_mode.pop(
        update.effective_user.id,
        None,
    )

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

        label = str(
            movie.get(
                "title",
                "Movie",
            )
        )[:48]

        if movie.get("year"):

            label += (
                f" ({movie['year']})"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "🎬 " + label,
                    url=build_movie_link(
                        movie
                    ),
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

        message = (
            update.effective_message
        )

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

        with movie_lock:

            movies = [
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

    user_search_mode.pop(
        uid,
        None,
    )

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
        "Do not claim you can access unavailable information.\n\n"
        "User: "
        + text
    )

    try:

        result = (
            gemini_client
            .models
            .generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
        )

        answer = getattr(
            result,
            "text",
            None,
        )

        if answer:
            return str(
                answer
            ).strip()

        return None

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

    n = normalize_text(
        text
    )

    return any(
        normalize_text(word)
        in n
        for word
        in MOVIE_INTENT_WORDS
    )


def is_channel_request(text):

    n = normalize_text(
        text
    )

    return any(
        normalize_text(word)
        in n
        for word
        in CHANNEL_REQUEST_WORDS
    )


async def handle_special_reply(
    message,
    text,
):

    n = normalize_text(
        text
    )

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
# WEB APP DATA
# ============================================================

async def web_app_data_handler(
    update,
    context,
):

    message = update.effective_message

    if (
        not message
        or not message.web_app_data
    ):
        return

    uid = update.effective_user.id

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

    data = (
        message.web_app_data.data
        or ""
    ).strip()

    if data == "ai":

        used = get_ai_usage(
            uid
        )

        if used >= FREE_AI_LIMIT:

            await message.reply_text(
                "ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\n"
                "ငါ့ကို Movie မေး 😝🎬\n"
                "ကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။"
            )

            return

        user_search_mode.pop(
            uid,
            None,
        )

        user_ai_mode[uid] = True

        await message.reply_text(
            "🤖 Xiao Yu CyB ပါ 😝\n"
            "Mini App ကနေ AI ထဲဝင်လာတာလား?\n\n"
            f"Free AI ကျန်တာ — "
            f"{FREE_AI_LIMIT - used} ခါ"
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(
    update,
    context,
):

    message = update.effective_message

    if not message:
        return

    uid = update.effective_user.id

    text = (
        message.text
        or ""
    ).strip()

    # Admin uploader stays available even if admin is not a
    # normal member of the public gate channel.
    if is_admin_user(uid):

        if await uploader_caption_handler(
            update,
            context,
        ):
            return

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

    if is_channel_request(
        text
    ):

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

    if user_search_mode.get(
        uid
    ):

        user_search_mode.pop(
            uid,
            None,
        )

        results = movie_search(
            text
        )

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

    if user_ai_mode.get(
        uid
    ):

        if get_ai_usage(
            uid
        ) >= FREE_AI_LIMIT:

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

        answer = await ask_gemini(
            text
        )

        if answer:

            increase_ai_usage(
                uid
            )

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

    results = movie_search(
        text
    )

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

    if has_movie_intent(
        text
    ):

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
# ADMIN UPLOADER
# ============================================================

async def uploader_video_handler(
    update,
    context,
):

    message = update.effective_message
    user = update.effective_user

    if (
        not message
        or not user
        or not is_admin_user(user.id)
    ):
        return False

    file_id = ""

    if message.video:

        file_id = (
            message.video.file_id
        )

    elif message.document:

        mime = str(
            message.document.mime_type
            or ""
        ).lower()

        name = str(
            message.document.file_name
            or ""
        ).lower()

        if (
            mime.startswith("video/")
            or name.endswith(
                (
                    ".mp4",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".webm",
                    ".m4v",
                )
            )
        ):

            file_id = (
                message.document.file_id
            )

    if not file_id:
        return False

    uid = str(
        user.id
    )

    uploader_sessions[uid] = {
        "video_file_id": file_id,
        "poster_file_id": "",
        "caption": "",
        "state": "poster",
    }

    await message.reply_text(
        "🎬 Video ရပါပြီ။\n\n"
        "🖼️ အခု Poster ပုံတစ်ပုံ ပို့ပေးပါ။"
    )

    return True


async def uploader_photo_handler(
    update,
    context,
):

    message = update.effective_message
    user = update.effective_user

    if (
        not message
        or not user
        or not is_admin_user(user.id)
    ):
        return False

    uid = str(
        user.id
    )

    session = uploader_sessions.get(
        uid
    )

    if (
        not session
        or session.get("state")
        != "poster"
    ):
        return False

    if not message.photo:
        return False

    session[
        "poster_file_id"
    ] = message.photo[
        -1
    ].file_id

    session["state"] = "caption"

    await message.reply_text(
        "🖼️ Poster ရပါပြီ။\n\n"
        "📝 အခု Movie Caption ပို့ပေးပါ။\n"
        "ဥပမာ — Movie Name (2026) 1080p Myanmar Audio"
    )

    return True


async def uploader_caption_handler(
    update,
    context,
):

    message = update.effective_message
    user = update.effective_user

    if (
        not message
        or not user
        or not is_admin_user(user.id)
    ):
        return False

    uid = str(
        user.id
    )

    session = uploader_sessions.get(
        uid
    )

    if (
        not session
        or session.get("state")
        != "caption"
    ):
        return False

    caption = str(
        message.text
        or message.caption
        or ""
    ).strip()

    if not caption:

        await message.reply_text(
            "📝 Caption လေး ပို့ပေးပါဦး။"
        )

        return True

    session[
        "caption"
    ] = caption

    session[
        "state"
    ] = "preview"

    try:

        await context.bot.send_media_group(
            chat_id=user.id,
            media=[
                InputMediaPhoto(
                    media=session[
                        "poster_file_id"
                    ],
                ),
                InputMediaVideo(
                    media=session[
                        "video_file_id"
                    ],
                    caption=caption,
                ),
            ],
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "👀 <b>Preview</b>\n\n"
                "အဆင်ပြေရင် <b>PUBLISH</b> နှိပ်ပါ။\n"
                "မတင်ချင်ရင် <b>CANCEL</b> နှိပ်ပါ။"
            ),
            parse_mode="HTML",
            reply_markup=uploader_keyboard(),
        )

    except Exception as e:

        print(
            "Uploader preview error:",
            e,
        )

        await message.reply_text(
            "❌ Preview ပို့မရသေးဘူး။ ထပ်စမ်းကြည့်ပါ။"
        )

    return True


async def cancel_upload(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if (
        not user
        or not is_admin_user(user.id)
    ):

        await query.answer(
            "Admin only",
            show_alert=True,
        )

        return

    clear_uploader(
        user.id
    )

    await query.answer(
        "Cancelled"
    )

    try:

        await query.edit_message_text(
            "❌ Movie upload cancelled."
        )

    except Exception:

        await context.bot.send_message(
            chat_id=user.id,
            text="❌ Movie upload cancelled.",
        )


async def publish_upload(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if (
        not user
        or not is_admin_user(user.id)
    ):

        await query.answer(
            "Admin only",
            show_alert=True,
        )

        return

    uid = str(
        user.id
    )

    session = uploader_sessions.get(
        uid
    )

    if (
        not session
        or session.get("state")
        != "preview"
    ):

        await query.answer(
            "Upload session မရှိတော့ဘူး။",
            show_alert=True,
        )

        return

    storage_id = (
        get_storage_channel_id()
    )

    if not storage_id:

        await query.answer(
            "Storage Channel ID မသိသေးဘူး။",
            show_alert=True,
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ Private Movie Storage Channel ID "
                "ကို auto-detect မလုပ်ရသေးဘူး။\n\n"
                "Bot ကို Storage Channel ထဲမှာ Admin ထားပြီး၊ "
                "အဲဒီ Channel ထဲကို test message တစ်ခု post လုပ်ပေးပါ။\n"
                "ပြီးရင် ဒီ movie ကို ပြန် upload လုပ်ပါ။"
            ),
        )

        return

    try:

        sent = (
            await context.bot.send_media_group(
                chat_id=int(storage_id),
                media=[
                    InputMediaPhoto(
                        media=session[
                            "poster_file_id"
                        ],
                    ),
                    InputMediaVideo(
                        media=session[
                            "video_file_id"
                        ],
                        caption=session[
                            "caption"
                        ],
                    ),
                ],
            )
        )

        sent = sorted(
            sent,
            key=lambda m: m.message_id,
        )

        movie = build_movie_from_messages(
            sent,
            session["caption"],
        )

        if not movie:
            raise RuntimeError(
                "Could not build movie record"
            )

        movie[
            "storage_channel"
        ] = str(
            storage_id
        )

        save_movie_record(
            movie
        )

        group_id = str(
            getattr(
                sent[0],
                "media_group_id",
                "",
            )
            or ""
        )

        if group_id:

            published_album_ids.add(
                (
                    str(storage_id),
                    group_id,
                )
            )

        clear_uploader(
            user.id
        )

        await query.answer(
            "Published 🎬"
        )

        try:

            await query.edit_message_text(
                "✅ <b>PUBLISHED</b>\n\n"
                + html.escape(
                    str(
                        movie.get(
                            "title",
                            "Movie",
                        )
                    )
                )
                + "\n\nMini App ထဲမှာလည်း "
                "ထည့်ပြီးပါပြီ။",
                parse_mode="HTML",
            )

        except Exception:

            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "✅ Movie published successfully."
                ),
            )

        print(
            "Admin published movie:",
            movie.get("title"),
            "Storage:",
            storage_id,
            "Messages:",
            movie.get(
                "storage_message_ids"
            ),
        )

    except Exception as e:

        print(
            "Uploader publish error:",
            e,
        )

        await query.answer(
            "Publish မအောင်မြင်သေးဘူး။",
            show_alert=True,
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "❌ Publish မအောင်မြင်သေးဘူး။\n\n"
                "Storage Channel ထဲမှာ Bot က post/media "
                "ပို့ခွင့်ရှိမရှိ စစ်ပေးပါ။\n"
                f"Error: {e}"
            ),
        )


async def cancel_upload_command(
    update,
    context,
):

    user = update.effective_user

    if (
        not user
        or not is_admin_user(user.id)
    ):
        return

    clear_uploader(
        user.id
    )

    await update.effective_message.reply_text(
        "❌ Upload session cancelled."
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
    uid = query.from_user.id

    # --------------------------------------------------------
    # ADMIN UPLOADER CALLBACKS
    # These are intentionally allowed before public membership
    # because the admin uploader must continue working.
    # --------------------------------------------------------

    if data == "uploader_publish":
        return await publish_upload(
            update,
            context,
        )

    if data == "uploader_cancel":
        return await cancel_upload(
            update,
            context,
        )

    # --------------------------------------------------------
    # PUBLIC GATE
    # Every normal callback is checked again.
    # If user leaves the channel after opening the bot,
    # callbacks become locked again.
    # --------------------------------------------------------

    if not await is_member(
        context.bot,
        uid,
    ):

        try:
            await query.answer(
                "🎬 အရင် Movie Myanmar ကို Join လုပ်ပါ။",
                show_alert=True,
            )
        except Exception:
            pass

        try:

            await query.message.reply_text(
                "🔒 Movie Myanmar ကို Join လုပ်ပြီးမှ "
                "ဆက်သုံးလို့ရမယ်နော်။",
                reply_markup=join_keyboard(),
            )

        except Exception:
            pass

        return

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

    if data.startswith(
        "genre:"
    ):

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
                                url=(
                                    RENDER_URL
                                    + "/app"
                                )
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

def is_movie_storage_channel(
    message
):

    if (
        not message
        or not message.chat
    ):
        return False

    chat_id = str(
        message.chat_id
    )

    if MOVIE_STORAGE_CHAT_ID:

        return (
            chat_id
            == MOVIE_STORAGE_CHAT_ID
        )

    known = get_storage_channel_id()

    if known:

        return (
            chat_id
            == known
        )

    username = (
        message.chat.username
        or ""
    ).strip().lower()

    expected_main = (
        MAIN_CHANNEL
        .lstrip("@")
        .lower()
    )

    if username == expected_main:
        return False

    return not username


def build_storage_post_link(
    message
):

    chat_id = str(
        message.chat_id
    )

    message_id = (
        message.message_id
    )

    if (
        chat_id.startswith("-100")
        and message_id
    ):

        return (
            "https://t.me/c/"
            f"{chat_id[4:]}/"
            f"{message_id}"
        )

    if message.chat.username:

        return (
            "https://t.me/"
            f"{message.chat.username}/"
            f"{message_id}"
        )

    return MOVIE_CHANNEL_URL


def build_movie_from_messages(
    messages,
    caption_override=None,
):

    messages = [
        m
        for m in messages
        if m
    ]

    if not messages:
        return None

    messages.sort(
        key=lambda m: m.message_id
    )

    video_message = next(
        (
            m
            for m in messages
            if getattr(
                m,
                "video",
                None,
            )
        ),
        None,
    )

    photo_message = next(
        (
            m
            for m in messages
            if getattr(
                m,
                "photo",
                None,
            )
        ),
        None,
    )

    if video_message is None:

        video_message = next(
            (
                m
                for m in messages
                if (
                    getattr(
                        m,
                        "document",
                        None,
                    )
                    and str(
                        getattr(
                            m.document,
                            "mime_type",
                            "",
                        )
                        or ""
                    )
                    .lower()
                    .startswith(
                        "video/"
                    )
                )
            ),
            None,
        )

    caption = str(
        caption_override
        or ""
    ).strip()

    if not caption:

        for m in messages:

            candidate = str(
                getattr(
                    m,
                    "caption",
                    "",
                )
                or getattr(
                    m,
                    "text",
                    "",
                )
                or ""
            ).strip()

            if candidate:

                caption = candidate
                break

    if video_message is None:
        return None

    source = video_message

    text = (
        caption
        or "Untitled Movie"
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

    year = extract_year(
        text
    )

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

    poster_file_id = ""

    if (
        photo_message
        and photo_message.photo
    ):

        poster_file_id = (
            photo_message
            .photo[-1]
            .file_id
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
        "genres": detect_genres(
            text
        ),
        "description": text,
        "post_link": (
            build_storage_post_link(
                source
            )
        ),
        "chat_id": str(
            source.chat_id
        ),
        "message_id": (
            source.message_id
        ),
        "poster_file_id": (
            poster_file_id
        ),
        "media_group_id": str(
            getattr(
                source,
                "media_group_id",
                "",
            )
            or ""
        ),
        "storage_message_ids": [
            m.message_id
            for m in messages
        ],
    }

    if getattr(
        source,
        "video",
        None,
    ):

        movie[
            "video_file_id"
        ] = source.video.file_id

    elif getattr(
        source,
        "document",
        None,
    ):

        movie[
            "video_file_id"
        ] = source.document.file_id

    movie["id"] = generate_movie_id(
        source.chat_id,
        source.message_id,
    )

    return movie


def movie_already_saved(
    movie
):

    if not movie:
        return False

    chat_id = str(
        movie.get(
            "chat_id",
            "",
        )
    )

    message_id = movie.get(
        "message_id"
    )

    group_id = str(
        movie.get(
            "media_group_id",
            "",
        )
    )

    movie_id = str(
        movie.get(
            "id",
            "",
        )
    )

    with movie_lock:

        for old in movie_database:

            if (
                movie_id
                and str(
                    old.get(
                        "id",
                        "",
                    )
                )
                == movie_id
            ):
                return True

            if (
                chat_id
                and str(
                    old.get(
                        "chat_id",
                        "",
                    )
                )
                == chat_id
                and message_id
                and old.get(
                    "message_id"
                )
                == message_id
            ):
                return True

            if (
                group_id
                and str(
                    old.get(
                        "media_group_id",
                        "",
                    )
                )
                == group_id
                and str(
                    old.get(
                        "storage_channel",
                        "",
                    )
                )
                == chat_id
            ):
                return True

    return False


def save_movie_record(
    movie
):

    if not movie:
        return False

    replaced = False

    with movie_lock:

        for i, old in enumerate(
            movie_database
        ):

            same = (
                str(
                    old.get(
                        "id",
                        "",
                    )
                )
                == str(
                    movie.get(
                        "id",
                        "",
                    )
                )
            )

            if not same:

                same = (
                    str(
                        old.get(
                            "chat_id",
                            "",
                        )
                    )
                    == str(
                        movie.get(
                            "chat_id",
                            "",
                        )
                    )
                    and old.get(
                        "message_id"
                    )
                    == movie.get(
                        "message_id"
                    )
                )

            if same:

                movie_database[i] = movie
                replaced = True
                break

        if not replaced:

            movie_database.append(
                movie
            )

    # Actual movie update/add only.
    save_movies()

    invalidate_carousel()

    return not replaced


async def save_album_as_movie(
    messages,
    caption=None,
):

    if not messages:
        return

    messages = sorted(
        [
            m
            for m in messages
            if m
        ],
        key=lambda m: m.message_id,
    )

    group_id = str(
        getattr(
            messages[0],
            "media_group_id",
            "",
        )
        or ""
    )

    key = (
        str(
            messages[0].chat_id
        ),
        group_id,
    )

    if key in published_album_ids:
        return

    movie = build_movie_from_messages(
        messages,
        caption,
    )

    if not movie:
        return

    movie[
        "storage_channel"
    ] = str(
        messages[0].chat_id
    )

    if movie_already_saved(
        movie
    ):

        published_album_ids.add(
            key
        )

        return

    save_movie_record(
        movie
    )

    published_album_ids.add(
        key
    )

    print(
        "Saved PRIVATE movie album:",
        movie.get("title"),
        "Storage Chat:",
        messages[0].chat_id,
        "Messages:",
        movie.get(
            "storage_message_ids"
        ),
        "Link:",
        movie.get(
            "post_link"
        ),
        "ID:",
        movie.get(
            "id"
        ),
        "Total:",
        len(movie_database),
    )


async def process_album_later(
    key
):

    try:

        await asyncio.sleep(
            1.2
        )

        messages = album_buffers.pop(
            key,
            [],
        )

        album_tasks.pop(
            key,
            None,
        )

        if messages:

            await save_album_as_movie(
                messages
            )

    except Exception as e:

        print(
            "Album processing error:",
            e,
        )


async def channel_post_handler(
    update,
    context,
):

    message = update.channel_post

    if (
        not message
        or not is_movie_storage_channel(
            message
        )
    ):
        return

    global AUTO_STORAGE_CHAT_ID

    if not MOVIE_STORAGE_CHAT_ID:

        discovered = str(
            message.chat_id
        )

        if not AUTO_STORAGE_CHAT_ID:

            AUTO_STORAGE_CHAT_ID = (
                discovered
            )

            save_storage_channel_id(
                discovered
            )

            print(
                "Auto-detected Movie Storage Channel:",
                discovered,
            )

    group_id = str(
        getattr(
            message,
            "media_group_id",
            "",
        )
        or ""
    )

    if group_id:

        key = (
            str(
                message.chat_id
            ),
            group_id,
        )

        album_buffers.setdefault(
            key,
            []
        ).append(
            message
        )

        task = album_tasks.get(
            key
        )

        if (
            task is None
            or task.done()
        ):

            album_tasks[key] = (
                asyncio.create_task(
                    process_album_later(
                        key
                    )
                )
            )

        return

    movie = build_movie_from_messages(
        [message]
    )

    if not movie:
        return

    movie[
        "storage_channel"
    ] = str(
        message.chat_id
    )

    save_movie_record(
        movie
    )

    print(
        "Saved PRIVATE movie:",
        movie.get("title"),
        "Storage Chat:",
        message.chat_id,
        "Message:",
        message.message_id,
        "Link:",
        movie.get("post_link"),
        "ID:",
        movie.get("id"),
        "Total:",
        len(movie_database),
    )


# ============================================================
# MINI APP AUTH HELPERS
# ============================================================

def verify_telegram_webapp_init_data(
    init_data
):
    """
    Verify Telegram WebApp initData using the official
    HMAC-SHA256 WebAppData algorithm.

    Returns the Telegram user dict on success.
    Returns None on failure.
    """

    if not BOT_TOKEN:
        return None

    if not init_data:
        return None

    try:

        values = {}

        for item in init_data.split("&"):

            if "=" not in item:
                continue

            key, value = item.split(
                "=",
                1,
            )

            values[key] = value

        received_hash = values.pop(
            "hash",
            "",
        )

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={values[key]}"
            for key in sorted(values)
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(
                "utf-8"
            ),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(
                "utf-8"
            ),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash,
        ):
            print(
                "Mini App auth failed: invalid hash"
            )
            return None

        auth_date = int(
            values.get(
                "auth_date",
                "0",
            )
        )

        if not auth_date:
            return None

        if (
            abs(
                int(time.time())
                - auth_date
            )
            > WEBAPP_AUTH_MAX_AGE
        ):

            print(
                "Mini App auth failed: expired initData"
            )

            return None

        user_raw = values.get(
            "user",
            "",
        )

        if not user_raw:
            return None

        user = json.loads(
            unquote(
                user_raw
            )
        )

        if not isinstance(
            user,
            dict,
        ):
            return None

        if not user.get(
            "id"
        ):
            return None

        return user

    except Exception as e:

        print(
            "Mini App initData verify error:",
            e,
        )

        return None


def get_header_case_insensitive(
    handler,
    name,
):
    try:

        value = handler.headers.get(
            name
        )

        if value:
            return value.strip()

        target = name.lower()

        for key, value in handler.headers.items():

            if key.lower() == target:

                return str(
                    value
                ).strip()

    except Exception:
        pass

    return ""


def telegram_get_chat_member_sync(
    user_id
):
    """
    Synchronous Bot API membership check for the HTTP server.
    This is used by Mini App API requests.
    """

    if not BOT_TOKEN:
        return False

    try:

        payload = json.dumps(
            {
                "chat_id": MAIN_CHANNEL,
                "user_id": int(
                    user_id
                ),
            }
        ).encode("utf-8")

        request = Request(
            (
                "https://api.telegram.org/bot"
                + BOT_TOKEN
                + "/getChatMember"
            ),
            data=payload,
            headers={
                "Content-Type":
                    "application/json",
                "User-Agent":
                    "movie-myanmar-mini-app",
            },
            method="POST",
        )

        with urlopen(
            request,
            timeout=8,
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        if not result.get(
            "ok"
        ):
            print(
                "Mini App membership API error:",
                result,
            )
            return False

        member = result.get(
            "result",
            {},
        )

        status = member.get(
            "status",
            "",
        )

        return status in (
            "member",
            "administrator",
            "creator",
            "restricted",
        )

    except Exception as e:

        print(
            "Mini App membership check error:",
            e,
        )

        return False


def mini_app_authorize(
    handler
):
    """
    Protected Mini App API gate.

    The browser must send Telegram's initData in:
        X-Telegram-Init-Data

    Then:
      1. Telegram signature is verified.
      2. Telegram user ID is extracted.
      3. Current channel membership is checked again.

    This prevents a direct browser/API request from bypassing
    the Movie Myanmar join gate.
    """

    init_data = get_header_case_insensitive(
        handler,
        "X-Telegram-Init-Data",
    )

    if not init_data:

        return (
            False,
            None,
            "Mini App ကို Telegram ထဲကနေ ဖွင့်ပါ။",
        )

    user = (
        verify_telegram_webapp_init_data(
            init_data
        )
    )

    if not user:

        return (
            False,
            None,
            "Telegram authentication မအောင်မြင်ပါ။",
        )

    user_id = user.get(
        "id"
    )

    if not telegram_get_chat_member_sync(
        user_id
    ):

        return (
            False,
            user,
            "🔒 Movie Myanmar ကို Join လုပ်ပြီးမှ သုံးလို့ရမယ်နော်။",
        )

    return (
        True,
        user,
        "",
    )


# ============================================================
# MINI APP HTML
# ============================================================

MINI_APP_HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta
    name="viewport"
    content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"
>

<title>Movie Myanmar</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>
*{
    box-sizing:border-box
}

html,
body{
    margin:0;
    padding:0;
    width:100%;
    min-height:100%;
    background:#07070d;
    color:#f6f4ff;
    font-family:Arial,sans-serif;
    overflow-x:hidden
}

body{
    min-height:100vh
}

/* ==========================================================
   LOADING / WAKE SCREEN
   ========================================================== */

#splash{
    position:fixed;
    inset:0;
    z-index:9999;
    display:flex;
    align-items:center;
    justify-content:center;
    flex-direction:column;
    text-align:center;
    background:
        radial-gradient(
            circle at 50% 15%,
            #32106b 0,
            #120b22 34%,
            #05050a 78%,
            #020205 100%
        );
    transition:opacity .35s ease
}

#splash.hide{
    opacity:0;
    pointer-events:none
}

.logo{
    font-size:30px;
    font-weight:1000;
    letter-spacing:2px;
    text-shadow:
        0 0 18px #8d5cff88,
        0 0 42px #5af5ff44
}

.logo span{
    color:#6cf6ff
}

.splash-sub{
    margin-top:7px;
    color:#a9a1bd;
    font-size:12px
}

.spinner{
    width:44px;
    height:44px;
    margin-top:28px;
    border-radius:50%;
    border:3px solid #2c2440;
    border-top-color:#6cf6ff;
    border-right-color:#bb5cff;
    animation:spin .85s linear infinite
}

@keyframes spin{
    to{
        transform:rotate(360deg)
    }
}

.splash-text{
    margin-top:16px;
    color:#ddd7ea;
    font-size:13px
}

.splash-small{
    margin-top:8px;
    color:#777187;
    font-size:11px;
    max-width:270px;
    line-height:1.5
}

.retry-btn{
    display:none;
    margin-top:20px;
    border:0;
    border-radius:13px;
    padding:12px 20px;
    background:linear-gradient(
        135deg,
        #5af5ff,
        #bb5cff
    );
    color:#07070d;
    font-weight:900
}


/* ==========================================================
   APP
   ========================================================== */

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

.loader{
    width:100%;
    padding:30px 10px;
    text-align:center;
    color:#aaa
}

.api-status{
    display:none;
    padding:9px 14px;
    text-align:center;
    color:#a9a1bd;
    font-size:11px
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
    width:100%;
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
    padding:7px 11px;
    margin:10px
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
</style>
</head>

<body>

<!-- ========================================================
     LOADING SCREEN
     ======================================================== -->

<div id="splash">

    <div class="logo">
        MOVIE <span>MYANMAR</span>
    </div>

    <div class="splash-sub">
        Xiao Yu CyB • Cinema Mini App
    </div>

    <div
        id="spinner"
        class="spinner"
    ></div>

    <div
        id="splashText"
        class="splash-text"
    >
        Loading Movie Myanmar...
    </div>

    <div
        id="splashSmall"
        class="splash-small"
    >
        Movie database ကို ပြန်နိုးနေပါတယ်။
    </div>

    <button
        id="retryBtn"
        class="retry-btn"
        onclick="boot(true)"
    >
        🔄 Try Again
    </button>

</div>


<main
    id="mainApp"
    class="app"
    style="display:none"
>

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

<div
    id="apiStatus"
    class="api-status"
></div>

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

<div
    id="rail"
    class="rail"
>

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

<div
    id="genres"
    class="chips"
></div>

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

    try{
        tg.setHeaderColor('#07070d');
        tg.setBackgroundColor('#07070d');
    }catch(e){}
}

const $ =
    id => document.getElementById(id);

let movies = [];
let authorized = false;
let booting = false;


/* ==========================================================
   ESCAPE
   ========================================================== */

function esc(s){

    return String(
        s || ''
    ).replace(
        /[&<>"]/g,
        m => ({
            '&':'&amp;',
            '<':'&lt;',
            '>':'&gt;',
            '"':'&quot;'
        }[m])
    );
}


/* ==========================================================
   TELEGRAM INIT DATA
   ========================================================== */

function telegramInitData(){

    if(
        tg &&
        typeof tg.initData === 'string'
    ){
        return tg.initData;
    }

    return '';
}


/* ==========================================================
   CARD
   ========================================================== */

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

        ${
            poster
            ? `<img
                class="poster"
                src="${poster}"
                onerror="this.style.visibility='hidden'"
            >`
            : `<div class="poster"></div>`
        }

        <div class="ct">

            <div class="title">
                ${esc(m.title)}
            </div>

            <div class="meta">
                ${esc(m.year || '')}
                ${m.quality ? ' • ' + esc(m.quality) : ''}
            </div>

        </div>

    </article>
    `;
}


/* ==========================================================
   API
   ========================================================== */

async function api(
    url,
    options={}
){

    const headers =
        Object.assign(
            {},
            options.headers || {}
        );

    const initData =
        telegramInitData();

    if(initData){

        headers[
            'X-Telegram-Init-Data'
        ] = initData;

    }

    try{

        const response =
            await fetch(
                url,
                {
                    ...options,
                    headers,
                    cache:'no-store'
                }
            );

        if(
            response.status === 401
            || response.status === 403
        ){

            authorized = false;

            const data =
                await response
                    .json()
                    .catch(
                        () => ({})
                    );

            showGateError(
                data.message
                || 'Movie Myanmar ကို Join လုပ်ပြီးမှ သုံးလို့ရမယ်နော်။'
            );

            return {
                __auth_error:true
            };
        }

        if(!response.ok){

            return {
                __server_error:true,
                status:response.status
            };
        }

        const data =
            await response.json();

        authorized = true;

        return data;

    }catch(error){

        console.error(
            'API error:',
            error
        );

        return {
            __network_error:true
        };
    }
}


/* ==========================================================
   GATE ERROR
   ========================================================== */

function showGateError(
    message
){

    $('mainApp').style.display =
        'none';

    $('splash').classList.remove(
        'hide'
    );

    $('spinner').style.display =
        'none';

    $('retryBtn').style.display =
        'none';

    $('splashText').textContent =
        '🔒 Movie Myanmar Join လုပ်ပါ';

    $('splashSmall').textContent =
        message;

    setTimeout(
        () => {

            if(tg){

                try{
                    tg.close();
                }catch(e){}

            }

        },
        1800
    );
}


/* ==========================================================
   WAKE / RETRY
   ========================================================== */

function setSplash(
    text,
    small
){

    $('splashText').textContent =
        text;

    $('splashSmall').textContent =
        small;

    $('spinner').style.display =
        'block';

    $('retryBtn').style.display =
        'none';
}


async function boot(
    manual=false
){

    if(booting){
        return;
    }

    booting = true;

    $('mainApp').style.display =
        'none';

    $('splash').classList.remove(
        'hide'
    );

    setSplash(
        manual
        ? 'Waking Movie Myanmar...'
        : 'Loading Movie Myanmar...',
        'Movie database ကို ပြန်စစ်နေပါတယ်။'
    );

    const initData =
        telegramInitData();

    if(!initData){

        booting = false;

        $('spinner').style.display =
            'none';

        $('retryBtn').style.display =
            'none';

        $('splashText').textContent =
            '📱 Telegram App ထဲကနေ ဖွင့်ပါ';

        $('splashSmall').textContent =
            'Movie Myanmar Mini App ကို Telegram ထဲကနေ ဖွင့်မှ Join Gate စစ်နိုင်ပါတယ်။';

        return;
    }

    let latestData = null;

    for(
        let attempt = 1;
        attempt <= ${MINI_APP_RETRIES};
        attempt++
    ){

        if(
            attempt > 1
        ){

            setSplash(
                'Movie Myanmar is waking up...',
                'Render server နိုးလာဖို့ ခဏစောင့်နေပါတယ်။ '
                + attempt
                + '/'
                + ${MINI_APP_RETRIES}
            );

            await new Promise(
                resolve =>
                    setTimeout(
                        resolve,
                        ${MINI_APP_RETRY_DELAY}
                    )
            );
        }

        const data =
            await api(
                '/api/latest?limit=20'
            );

        if(
            data.movies
            && Array.isArray(
                data.movies
            )
        ){

            latestData = data;
            break;
        }

        if(
            data.__auth_error
        ){

            booting = false;
            return;
        }
    }

    if(
        !latestData
        || !Array.isArray(
            latestData.movies
        )
    ){

        booting = false;

        $('spinner').style.display =
            'none';

        $('retryBtn').style.display =
            'inline-block';

        $('splashText').textContent =
            '😴 Movie Myanmar နိုးမလာသေးဘူး';

        $('splashSmall').textContent =
            'Server wake-up ကြောင့် ခဏကြာနိုင်ပါတယ်။ '
            + 'Retry နှိပ်ပြီး ထပ်စမ်းပါ။';

        return;
    }

    movies =
        latestData.movies;

    $('rail').innerHTML =
        movies.length
        ? movies.map(card).join('')
        : '<div class="empty">No movies yet</div>';

    await showGenres(
        true
    );

    $('mainApp').style.display =
        'block';

    booting = false;

    $('splash').classList.add(
        'hide'
    );

    setTimeout(
        () => {
            $('splash').style.display =
                'none';
        },
        400
    );
}


/* ==========================================================
   LATEST
   ========================================================== */

async function loadLatest(){

    $('rail').innerHTML =
        '<div class="loader">Loading movies...</div>';

    const data =
        await api(
            '/api/latest?limit=20'
        );

    if(
        data.__auth_error
    ){
        return;
    }

    if(
        data.movies
        && Array.isArray(
            data.movies
        )
    ){

        movies =
            data.movies;

        $('rail').innerHTML =
            movies.length
            ? movies.map(card).join('')
            : '<div class="empty">No movies yet</div>';

        return;
    }

    $('rail').innerHTML =
        '<div class="loader">Movie Myanmar is waking up... 🔄</div>';

    setTimeout(
        loadLatest,
        1800
    );
}


/* ==========================================================
   SEARCH
   ========================================================== */

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

    if(
        data.__auth_error
    ){
        return;
    }

    if(
        data.movies
        && Array.isArray(
            data.movies
        )
    ){

        renderResults(
            'SEARCH RESULTS',
            data.movies
        );

    }else{

        renderResults(
            'SEARCH RESULTS',
            []
        );

    }
}


function renderResults(
    title,
    arr
){

    $('results').innerHTML = `

        <div class="sectionhead">

            <h2>
                ${esc(title)}
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


/* ==========================================================
   GENRES
   ========================================================== */

async function showGenres(
    silent=false
){

    const data =
        await api(
            '/api/genres'
        );

    if(
        data.__auth_error
    ){
        return;
    }

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

    if(!silent){

        $('genres').scrollIntoView({
            behavior:'smooth',
            block:'center'
        });

    }
}


async function genre(
    encodedGenre,
    button
){

    document
        .querySelectorAll(
            '.chip'
        )
        .forEach(
            x =>
                x.classList.remove(
                    'active'
                )
        );

    if(button){

        button.classList.add(
            'active'
        );
    }

    const data =
        await api(
            '/api/genre?name=' +
            encodedGenre
        );

    if(
        data.__auth_error
    ){
        return;
    }

    renderResults(
        '🎭 ' +
        decodeURIComponent(
            encodedGenre
        ),
        data.movies || []
    );
}


/* ==========================================================
   DETAIL
   ========================================================== */

async function detail(
    encodedId
){

    const movieId =
        decodeURIComponent(
            encodedId
        );

    const data =
        await api(
            '/api/movie?id=' +
            encodeURIComponent(
                movieId
            )
        );

    if(
        data.__auth_error
    ){
        return;
    }

    if(!data.title){
        return;
    }

    const poster =
        data.poster
        ? '/api/poster?id=' +
          encodeURIComponent(
              data.id
          )
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
            ? `<img src="${poster}" alt="">`
            : ''
        }

        <div class="mb">

            <h2>
                ${esc(data.title)}
            </h2>

            <div class="meta">
                ${esc(data.year || '')}
                ${data.quality ? ' • ' + esc(data.quality) : ''}
                ${data.audio ? ' • ' + esc(data.audio) : ''}
            </div>

            <p>
                ${esc(
                    data.description
                    || 'Movie Myanmar'
                )}
            </p>

            ${
                data.link
                ? `
                <a
                    href="${esc(data.link)}"
                    target="_blank"
                    rel="noopener"
                >
                    <button class="open">
                        🎬 Open Movie
                    </button>
                </a>
                `
                : ''
            }

        </div>
    `;

    $('overlay').style.display =
        'flex';
}


function closeModal(){

    $('overlay').style.display =
        'none';
}


/* ==========================================================
   AI
   ========================================================== */

function openAi(){

    if(
        !authorized
    ){

        boot(true);
        return;
    }

    if(
        tg &&
        typeof tg.sendData ===
            'function'
    ){

        tg.sendData(
            'ai'
        );

        setTimeout(
            () => {

                try{
                    tg.close();
                }catch(e){}

            },
            150
        );

    }else{

        alert(
            'Open Xiao Yu AI from the Telegram bot menu 😝'
        );

    }
}


/* ==========================================================
   SEARCH ENTER
   ========================================================== */

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


/* ==========================================================
   START
   ========================================================== */

boot(false);

</script>

</body>
</html>
'''

# Replace template placeholders in the HTML with numeric JS values.
MINI_APP_HTML = (
    MINI_APP_HTML
    .replace(
        "${MINI_APP_RETRIES}",
        str(MINI_APP_RETRIES),
    )
    .replace(
        "${MINI_APP_RETRY_DELAY}",
        str(MINI_APP_RETRY_DELAY),
    )
)


# ============================================================
# MINI APP SERIALIZER
# ============================================================

def mini_movie(movie):

    ensure_movie_id(
        movie
    )

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

def get_poster_bytes(
    movie_id
):

    movie = find_movie_by_id(
        movie_id
    )

    if not movie:
        return None, "image/jpeg"

    file_id = str(
        movie.get(
            "poster_file_id",
            "",
        )
        or ""
    )

    if not file_id:
        return None, "image/jpeg"

    if file_id in poster_cache:
        return poster_cache[
            file_id
        ]

    try:

        request = Request(
            (
                "https://api.telegram.org/bot"
                + BOT_TOKEN
                + "/getFile?file_id="
                + file_id
            )
        )

        response = urlopen(
            request,
            timeout=12,
        )

        data = json.loads(
            response.read().decode(
                "utf-8"
            )
        )

        path = (
            data
            .get(
                "result",
                {},
            )
            .get(
                "file_path"
            )
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

        content_type = (
            "image/jpeg"
        )

        lower_path = (
            path.lower()
        )

        if lower_path.endswith(
            ".png"
        ):

            content_type = (
                "image/png"
            )

        elif lower_path.endswith(
            ".webp"
        ):

            content_type = (
                "image/webp"
            )

        elif lower_path.endswith(
            ".gif"
        ):

            content_type = (
                "image/gif"
            )

        poster_cache[
            file_id
        ] = (
            raw,
            content_type,
        )

        return poster_cache[
            file_id
        ]

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
        *args,
    ):
        pass

    def send_bytes(
        self,
        data,
        content_type="text/plain; charset=utf-8",
        status=200,
    ):

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate"
        )

        self.send_header(
            "Pragma",
            "no-cache",
        )

        self.send_header(
            "Content-Length",
            str(len(data)),
        )

        self.end_headers()

        try:

            self.wfile.write(
                data
            )

        except Exception:
            pass

    def send_json(
        self,
        obj,
        status=200,
    ):

        self.send_bytes(
            json.dumps(
                obj,
                ensure_ascii=False,
            ).encode(
                "utf-8"
            ),
            "application/json; charset=utf-8",
            status,
        )

    def send_api_gate_error(
        self,
        message,
        status=403,
    ):

        self.send_json(
            {
                "ok": False,
                "authorized": False,
                "message": message,
            },
            status=status,
        )

    def do_GET(self):

        try:

            parsed = urlparse(
                self.path
            )

            path = parsed.path

            qs = parse_qs(
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
            # MINI APP PAGE
            # ------------------------------------------------
            # The HTML itself can be loaded by Telegram WebApp.
            # Protected movie data is NOT exposed here.
            # Every /api/* request requires verified Telegram
            # initData + current membership.
            # ------------------------------------------------

            if path in (
                "/app",
                "/miniapp",
            ):

                return self.send_bytes(
                    MINI_APP_HTML.encode(
                        "utf-8"
                    ),
                    "text/html; charset=utf-8",
                )

            # ------------------------------------------------
            # PROTECTED MINI APP API
            # ------------------------------------------------

            if path.startswith(
                "/api/"
            ):

                authorized, user, error_message = (
                    mini_app_authorize(
                        self
                    )
                )

                if not authorized:

                    return self.send_api_gate_error(
                        error_message
                        or "Access denied.",
                        401,
                    )

                # ------------------------------------------------
                # LATEST / ALL
                # ------------------------------------------------

                if path in (
                    "/api/movies",
                    "/api/latest",
                ):

                    try:

                        limit = int(
                            qs.get(
                                "limit",
                                ["20"],
                            )[0]
                        )

                    except Exception:
                        limit = 20

                    limit = max(
                        1,
                        min(
                            limit,
                            50,
                        ),
                    )

                    movies = latest(
                        limit
                    )

                    return self.send_json(
                        {
                            "ok": True,
                            "authorized": True,
                            "movies": [
                                mini_movie(
                                    movie
                                )
                                for movie
                                in movies
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

                    query = qs.get(
                        "q",
                        [""],
                    )[0]

                    results = (
                        movie_search(
                            query
                        )[:30]
                    )

                    return self.send_json(
                        {
                            "ok": True,
                            "authorized": True,
                            "movies": [
                                mini_movie(
                                    movie
                                )
                                for movie
                                in results
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
                            "ok": True,
                            "authorized": True,
                            "genres": list(
                                GENRE_KEYWORDS.keys()
                            ),
                        }
                    )

                # ------------------------------------------------
                # GENRE
                # ------------------------------------------------

                if path == "/api/genre":

                    genre = qs.get(
                        "name",
                        [""],
                    )[0]

                    if genre == "All":

                        results = latest(
                            50
                        )

                    else:

                        with movie_lock:

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
                            "ok": True,
                            "authorized": True,
                            "movies": [
                                mini_movie(
                                    movie
                                )
                                for movie
                                in results
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

                    movie_id = qs.get(
                        "id",
                        [""],
                    )[0]

                    movie = find_movie_by_id(
                        movie_id
                    )

                    return self.send_json(
                        (
                            {
                                "ok": True,
                                "authorized": True,
                                **mini_movie(
                                    movie
                                ),
                            }
                            if movie
                            else {
                                "ok": True,
                                "authorized": True,
                            }
                        )
                    )

                # ------------------------------------------------
                # POSTER
                # ------------------------------------------------

                if path == "/api/poster":

                    movie_id = qs.get(
                        "id",
                        [""],
                    )[0]

                    raw, content_type = (
                        get_poster_bytes(
                            movie_id
                        )
                    )

                    if not raw:

                        return self.send_bytes(
                            b"Poster not found",
                            "text/plain; charset=utf-8",
                            404,
                        )

                    return self.send_bytes(
                        raw,
                        content_type,
                        200,
                    )

                return self.send_bytes(
                    b"Not Found",
                    status=404,
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

            try:

                return self.send_bytes(
                    b"Server error",
                    status=500,
                )

            except Exception:
                return None

    def do_POST(self):

        global webhook_application
        global webhook_loop

        try:

            path = urlparse(
                self.path
            ).path

            if path != WEBHOOK_PATH:

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

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            if length <= 0:

                return self.send_bytes(
                    b"Bad Request",
                    status=400,
                )

            body = self.rfile.read(
                length
            )

            data = json.loads(
                body.decode(
                    "utf-8"
                )
            )

            if not webhook_application:

                return self.send_bytes(
                    b"Bot not ready",
                    status=503,
                )

            update = Update.de_json(
                data,
                webhook_application.bot,
            )

            if (
                update
                and webhook_loop
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

            try:

                return self.send_bytes(
                    b"Bad Request",
                    status=400,
                )

            except Exception:
                return None


# ============================================================
# HEALTH SERVER
# ============================================================

def start_health_server():

    try:

        server = HTTPServer(
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

    except Exception as e:

        print(
            "HTTP server error:",
            e,
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    print(
        "Telegram handler error:",
        context.error,
    )

    try:

        traceback.print_exception(
            type(context.error),
            context.error,
            context.error.__traceback__,
        )

    except Exception:
        pass


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

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # --------------------------------------------------------
    # CHANNEL POSTS
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            channel_post_handler,
        )
    )

    # --------------------------------------------------------
    # MINI APP DATA
    # --------------------------------------------------------

    try:

        web_app_data_filter = (
            filters.StatusUpdate.WEB_APP_DATA
        )

        application.add_handler(
            MessageHandler(
                web_app_data_filter,
                web_app_data_handler,
            )
        )

    except Exception as e:

        print(
            "WebAppData handler skipped:",
            e,
        )

    # --------------------------------------------------------
    # ADMIN UPLOADER
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "cancel_upload",
            cancel_upload_command,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            uploader_video_handler,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            uploader_photo_handler,
        ),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            uploader_video_handler,
        ),
        group=0,
    )

    # --------------------------------------------------------
    # NORMAL TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler,
        ),
        group=1,
    )

    application.add_error_handler(
        error_handler
    )

    webhook_application = (
        application
    )

    webhook_loop = (
        asyncio.get_running_loop()
    )

    # --------------------------------------------------------
    # START PTB
    # --------------------------------------------------------

    await application.initialize()
    await application.start()

    # --------------------------------------------------------
    # TELEGRAM MENU BUTTON
    # --------------------------------------------------------

    if (
        WebAppInfo
        and MenuButtonWebApp
    ):

        try:

            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🎬 Movie Myanmar",
                    web_app=WebAppInfo(
                        url=(
                            RENDER_URL
                            + "/app"
                        )
                    ),
                )
            )

            print(
                "Telegram Mini App menu button ready"
            )

        except Exception as e:

            print(
                "menu web app setup skipped:",
                e,
            )

    # --------------------------------------------------------
    # HEALTH SERVER
    # --------------------------------------------------------

    threading.Thread(
        target=start_health_server,
        daemon=True,
        name="movie-http-server",
    ).start()

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # STARTUP LOG
    # --------------------------------------------------------

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
        "Webhook:",
        webhook_url,
    )

    print(
        "Movie DB:",
        MOVIE_DB_FILE,
    )

    print(
        "Movie Storage Chat ID:",
        get_storage_channel_id()
        or "(auto-detect pending)",
    )

    print(
        "GitHub Movie Backup:",
        "ON"
        if github_enabled()
        else "OFF (set GITHUB_TOKEN)",
    )

    if github_enabled():

        print(
            "GitHub Movie File:",
            f"{GITHUB_REPO}/"
            f"{GITHUB_MOVIE_FILE}@"
            f"{GITHUB_BRANCH}",
        )

    print(
        "Movies:",
        len(movie_database),
    )

    print(
        "AI Model:",
        GEMINI_MODEL,
    )

    print(
        "Mini App Gate:",
        "Telegram initData + "
        "membership verification ON",
    )

    print(
        "Mini App Wake Retry:",
        MINI_APP_RETRIES,
        "attempts",
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # KEEP ALIVE
    # --------------------------------------------------------

    while True:

        await asyncio.sleep(
            3600
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
