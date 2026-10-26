# Movie Myanmar - single-file bot + embedded Mini App

import os
import asyncio
import base64
import hashlib
import json
import threading
import re
import traceback
import html
import hmac
import time
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode
from urllib.request import Request, urlopen

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
MAIN_CHANNEL_URL = "https://t.me/moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"

# Private channel used as the actual Movie Storage Channel.
# If left empty, the bot will automatically accept channel posts
# from channels that have no public username (private channels),
# while ignoring the public MAIN_CHANNEL.
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

PORT = int(
    os.getenv(
        "PORT",
        "10000",
    )
)

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

ADMIN_STATS_FILE = os.path.join(
    DATA_DIR,
    "admin_stats.json",
)

# Automatically learned private Movie Storage Channel ID.
# MOVIE_STORAGE_CHAT_ID from Render always takes precedence.
STORAGE_CHANNEL_FILE = os.path.join(
    DATA_DIR,
    "storage_channel.json",
)

# Admin movie uploader state.
# video -> poster -> caption -> preview -> publish
uploader_sessions = {}
album_buffers = {}
album_tasks = {}
published_album_ids = set()


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

        chat_id = str(
            data.get("chat_id", "")
        ).strip()
        return chat_id
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
        print("Storage channel save error:", e)


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
# GITHUB MOVIE BACKUP / PERSISTENT STORAGE
# ============================================================
# Render's local filesystem may be reset on redeploy/restart.
# Movie data is therefore backed up to a GitHub repository when
# these environment variables are configured on Render.
#
# IMPORTANT: Keep the repository PRIVATE if movie records contain
# private Telegram storage links/file IDs.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
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

GITHUB_API_BASE = "https://api.github.com"
github_sync_lock = threading.RLock()


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "movie-myanmar-bot",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def github_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO and GITHUB_MOVIE_FILE)


def github_get_movies():
    """Return (movies, sha) from the GitHub Contents API."""
    if not github_enabled():
        return [], None

    url = (
        f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/"
        f"{GITHUB_MOVIE_FILE}?ref={GITHUB_BRANCH}"
    )

    try:
        req = Request(
            url,
            headers=github_headers(),
            method="GET",
        )
        with urlopen(req, timeout=10) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )

        encoded = payload.get("content", "")
        encoded = encoded.replace("\n", "")
        raw = base64.b64decode(encoded).decode("utf-8")
        data = json.loads(raw)

        if not isinstance(data, list):
            data = []

        return data, payload.get("sha")

    except Exception as e:
        # 404 simply means the backup file does not exist yet.
        print("GitHub movie load skipped/error:", e)
        return [], None


def github_put_movies(movies):
    """Create/update the GitHub movie JSON file."""
    if not github_enabled():
        return False

    with github_sync_lock:
        try:
            # Fetch the latest SHA immediately before writing so that
            # repeated channel posts do not use a stale SHA.
            _, sha = github_get_movies()

            payload_text = json.dumps(
                movies,
                ensure_ascii=False,
                indent=2,
            ) + "\n"

            encoded = base64.b64encode(
                payload_text.encode("utf-8")
            ).decode("ascii")

            url = (
                f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/"
                f"{GITHUB_MOVIE_FILE}"
            )

            body = {
                "message": "Update Movie Myanmar movie database",
                "content": encoded,
                "branch": GITHUB_BRANCH,
            }

            if sha:
                body["sha"] = sha

            req = Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    **github_headers(),
                    "Content-Type": "application/json",
                },
                method="PUT",
            )

            with urlopen(req, timeout=15) as response:
                response.read()

            print(
                "GitHub movie backup saved:",
                len(movies),
                "movies",
            )
            return True

        except Exception as e:
            print("GitHub movie backup error:", e)
            return False


def github_background_save(snapshot):
    """Save movie data without blocking Telegram update handling."""
    if not github_enabled():
        return

    def worker():
        try:
            # Merge with whatever is currently in GitHub so an older
            # background worker can never intentionally erase newer data.
            remote_movies, _ = github_get_movies()
            merged = merge_movie_lists(
                remote_movies if isinstance(remote_movies, list) else [],
                snapshot if isinstance(snapshot, list) else [],
            )
            github_put_movies(merged)
        except Exception:
            print("GitHub background save error:")
            print(traceback.format_exc())

    threading.Thread(
        target=worker,
        daemon=True,
        name="github-movie-backup",
    ).start()


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
stats_lock = threading.RLock()

admin_stats = {
    "users": {},
    "searches": 0,
    "movie_views": {},
    "ai_requests": 0,
}


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
            os.fsync(f.fileno())

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
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


# ============================================================
# OLD DATA MIGRATION
# ============================================================

def merge_movie_lists(primary, secondary):
    """
    Merge two movie lists without duplicating stable records.
    Primary records win when the same ID exists.
    """

    result = []
    seen = set()

    for source in (
        primary,
        secondary,
    ):
        if not isinstance(source, list):
            continue

        for movie in source:
            if not isinstance(movie, dict):
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

            seen.add(movie_id)
            result.append(movie)

    return result


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

    # --------------------------------------------------------
    # MOVIES
    # --------------------------------------------------------

    if old_movie_file != current_movie_file:
        try:
            old_exists = os.path.exists(
                old_movie_file
            )

            new_exists = os.path.exists(
                MOVIE_DB_FILE
            )

            if old_exists:
                old_movies = load_json(
                    old_movie_file,
                    [],
                )

                new_movies = (
                    load_json(
                        MOVIE_DB_FILE,
                        [],
                    )
                    if new_exists
                    else []
                )

                if isinstance(
                    old_movies,
                    list,
                ):
                    if isinstance(
                        new_movies,
                        list,
                    ):
                        merged = merge_movie_lists(
                            new_movies,
                            old_movies,
                        )
                    else:
                        merged = old_movies

                    if merged:
                        save_json(
                            MOVIE_DB_FILE,
                            merged,
                        )

                        print(
                            "Movie data migration/merge:",
                            len(merged),
                        )

        except Exception:
            print(
                "Movie migration error:"
            )
            print(
                traceback.format_exc()
            )

    # --------------------------------------------------------
    # AI USAGE
    # --------------------------------------------------------

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

                for key, value in new_usage.items():
                    merged_usage[key] = value

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
        movie["id"] = generate_movie_id(
            chat_id,
            message_id,
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

            ensure_movie_id(movie)

            if movie.get(
                "id"
            ) != old_id:
                changed = True

            if not isinstance(
                movie.get("genres"),
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
# MOVIE DATABASE
# ============================================================

def load_movies():
    global movie_database

    local_movies = load_json(
        MOVIE_DB_FILE,
        [],
    )

    if not isinstance(local_movies, list):
        local_movies = []

    # Load the durable GitHub copy when configured, then merge both
    # sources. This is what keeps existing movies alive after a
    # Render restart/redeploy. Local records win on duplicate IDs.
    remote_movies = []
    if github_enabled():
        remote_movies, _ = github_get_movies()
        if not isinstance(remote_movies, list):
            remote_movies = []

    movie_database = merge_movie_lists(
        remote_movies,
        local_movies,
    )

    changed = normalize_movie_database()

    # Always write the merged result locally. If GitHub is enabled,
    # also refresh the durable copy so both sides contain the same
    # complete movie list.
    with movie_lock:
        snapshot = list(movie_database)

    save_json(
        MOVIE_DB_FILE,
        snapshot,
    )

    # IMPORTANT:
    # Do not write to GitHub during startup. The deploy repository
    # must never be updated just because Render restarted the bot.
    # Movie changes are backed up only by save_movies() after an
    # actual movie is added/updated.

    print(
        "Loaded movies:",
        len(movie_database),
        "from",
        MOVIE_DB_FILE,
        "GitHub backup:",
        "ON" if github_enabled() else "OFF",
    )


def save_movies():
    with movie_lock:
        snapshot = list(
            movie_database
        )

    # Local save happens immediately so the current process never
    # depends on GitHub being reachable.
    save_json(
        MOVIE_DB_FILE,
        snapshot,
    )

    # Durable backup happens in the background so a Telegram channel
    # post is not delayed by GitHub network latency.
    github_background_save(snapshot)


def load_ai_usage():
    global ai_usage

    x = load_json(
        AI_USAGE_FILE,
        {},
    )

    ai_usage = (
        x
        if isinstance(
            x,
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


def load_admin_stats():
    global admin_stats

    data = load_json(
        ADMIN_STATS_FILE,
        {},
    )

    if not isinstance(data, dict):
        data = {}

    users = data.get("users", {})
    movie_views = data.get("movie_views", {})

    admin_stats = {
        "users": users if isinstance(users, dict) else {},
        "searches": int(data.get("searches", 0) or 0),
        "movie_views": movie_views if isinstance(movie_views, dict) else {},
        "ai_requests": int(data.get("ai_requests", 0) or 0),
    }

    print(
        "Loaded admin stats:",
        len(admin_stats["users"]),
        "users",
    )


def save_admin_stats():
    with stats_lock:
        snapshot = {
            "users": dict(admin_stats.get("users", {})),
            "searches": int(admin_stats.get("searches", 0) or 0),
            "movie_views": dict(admin_stats.get("movie_views", {})),
            "ai_requests": int(admin_stats.get("ai_requests", 0) or 0),
        }

    save_json(
        ADMIN_STATS_FILE,
        snapshot,
    )


def record_user(uid):
    key = str(uid or "").strip()
    if not key:
        return

    with stats_lock:
        users = admin_stats.setdefault("users", {})
        if key in users:
            return
        now = int(time.time())
        users[key] = {
            "first_seen": now,
            "last_seen": now,
        }

    save_admin_stats()


def record_search():
    with stats_lock:
        admin_stats["searches"] = int(
            admin_stats.get("searches", 0) or 0
        ) + 1
    save_admin_stats()


def record_movie_view(movie_id):
    key = str(movie_id or "").strip()
    if not key:
        return

    with stats_lock:
        views = admin_stats.setdefault("movie_views", {})
        views[key] = int(views.get(key, 0) or 0) + 1
    save_admin_stats()


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

    with stats_lock:
        admin_stats["ai_requests"] = int(
            admin_stats.get("ai_requests", 0) or 0
        ) + 1

    save_ai_usage()
    save_admin_stats()


migrate_old_data()
load_movies()
load_ai_usage()
load_admin_stats()


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
# MOVIE LOOKUP
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
    qn = normalize_text(q)

    if not qn:
        return []

    record_search()

    year = extract_year(q)

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


def latest(limit=None):
    with movie_lock:
        movies = list(reversed(movie_database))

    if limit is None:
        return movies

    try:
        limit = int(limit)
    except Exception:
        return movies

    if limit <= 0:
        return []

    return movies[:limit]


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
                    url=MAIN_CHANNEL_URL,
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


def home_keyboard(user_id=None):
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

        rows.insert(1, [
            InlineKeyboardButton(
                "🏛️ Cinema Museum",
                web_app=WebAppInfo(
                    url=(
                        RENDER_URL
                        + "/museum"
                    )
                ),
            )
        ])

        if is_admin_user(user_id):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🛠 Admin Dashboard",
                        web_app=WebAppInfo(
                            url=(
                                RENDER_URL
                                + "/admin"
                            )
                        ),
                    )
                ]
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

        rows.append(row)

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
    # Admin is always allowed, even without joining the public gate channel.
    if is_admin_user(uid):
        return True

    try:
        status = await bot.get_chat_member(
            MAIN_CHANNEL,
            uid,
        )

        if status.status in (
            "member",
            "administrator",
            "creator",
        ):
            return True

        # Telegram can return restricted users. Only count them as joined
        # when Telegram explicitly says they are still a member.
        if status.status == "restricted":
            return bool(
                getattr(status, "is_member", False)
            )

        return False

    except Exception as e:
        print(
            "membership check error:",
            e,
        )
        return False


def telegram_get_chat_member_sync(uid):
    """Synchronous Telegram membership lookup for the HTTP Mini App server."""
    if is_admin_user(uid):
        return True

    if not BOT_TOKEN:
        return False

    try:
        query = urlencode(
            {
                "chat_id": MAIN_CHANNEL,
                "user_id": str(uid),
            }
        )

        request = Request(
            "https://api.telegram.org/bot"
            + BOT_TOKEN
            + "/getChatMember?"
            + query,
            headers={
                "User-Agent": "Movie-Myanmar-Mini-App",
            },
        )

        with urlopen(request, timeout=8) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )

        if not payload.get("ok"):
            return False

        member = payload.get("result") or {}
        member_status = member.get("status", "")

        if member_status in (
            "member",
            "administrator",
            "creator",
        ):
            return True

        if member_status == "restricted":
            return bool(member.get("is_member", False))

        return False

    except Exception as e:
        print(
            "sync membership check error:",
            e,
        )
        return False


def validate_telegram_webapp_init_data(init_data):
    """Validate Telegram Web App initData and return the real user ID."""
    if not init_data or not BOT_TOKEN:
        return None

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
        )

        data = dict(pairs)
        received_hash = data.pop("hash", "")

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(data.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            expected_hash,
            received_hash,
        ):
            return None

        auth_date = int(
            data.get("auth_date", "0") or 0
        )

        # Do not accept an ancient/replayed Mini App session.
        if not auth_date or (time.time() - auth_date) > 86400:
            return None

        user_data = json.loads(
            data.get("user", "{}")
        )

        uid = int(
            user_data.get("id", 0) or 0
        )

        return uid or None

    except Exception as e:
        print(
            "WebApp initData validation error:",
            e,
        )
        return None


def get_webapp_init_data(headers, qs):
    raw = str(
        headers.get(
            "X-Telegram-Init-Data",
            "",
        )
        or ""
    ).strip()

    if raw:
        return raw

    # Poster <img src> requests cannot attach custom headers, so the
    # Mini App sends initData as an encoded query parameter only there.
    values = qs.get("initData", [])
    if values:
        return str(values[0] or "").strip()

    return ""


def authorize_webapp_request(headers, qs):
    raw = get_webapp_init_data(
        headers,
        qs,
    )

    uid = validate_telegram_webapp_init_data(raw)

    if not uid:
        return False, None, "invalid_init_data"

    if is_admin_user(uid):
        record_user(uid)
        return True, uid, "admin"

    if not telegram_get_chat_member_sync(uid):
        return False, uid, "not_member"

    record_user(uid)
    return True, uid, "ok"


async def stop_carousel(chat_id):
    carousel_message_ids.pop(
        chat_id,
        None,
    )


# ============================================================
# HOME COMMAND
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
            reply_markup=home_keyboard(
                update.effective_user.id
                if update.effective_user
                else None
            ),
        )

    except Exception:
        await target.reply_text(
            home_caption(),
            parse_mode="HTML",
            reply_markup=home_keyboard(
                update.effective_user.id
                if update.effective_user
                else None
            ),
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

    record_user(uid)

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
        await query.answer("✅ Joined!", show_alert=False)

        # Remove the join prompt and immediately open the Home screen.
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text(
                    "✅ ဝင်ပြီးပြီ 😝"
                )
            except Exception:
                pass

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
        message = update.effective_message

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
        result = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
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
    n = normalize_text(text)

    return any(
        normalize_text(word)
        in n
        for word
        in MOVIE_INTENT_WORDS
    )


def is_channel_request(text):
    n = normalize_text(text)

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
# WEB APP DATA
# ============================================================

async def web_app_data_handler(
    update,
    context,
):
    message = update.effective_message

    if not message or not message.web_app_data:
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
        used = get_ai_usage(uid)

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

    # Admin uploader caption step must work without membership gating.
    if is_admin_user(uid):
        if await uploader_caption_handler(update, context):
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

    record_user(uid)

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

        answer = await ask_gemini(
            text
        )

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
        reply_markup=home_keyboard(uid),
    )


# ============================================================
# ADMIN MOVIE UPLOADER
# ============================================================

async def uploader_video_handler(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not is_admin_user(user.id):
        return False

    file_id = ""
    if message.video:
        file_id = message.video.file_id
    elif message.document:
        mime = str(message.document.mime_type or "").lower()
        name = str(message.document.file_name or "").lower()
        if mime.startswith("video/") or name.endswith((
            ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"
        )):
            file_id = message.document.file_id

    if not file_id:
        return False

    uid = str(user.id)
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


async def uploader_photo_handler(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not is_admin_user(user.id):
        return False

    uid = str(user.id)
    session = uploader_sessions.get(uid)
    if not session or session.get("state") != "poster":
        return False

    if not message.photo:
        return False

    session["poster_file_id"] = message.photo[-1].file_id
    session["state"] = "caption"

    await message.reply_text(
        "🖼️ Poster ရပါပြီ။\n\n"
        "📝 အခု Movie Caption ပို့ပေးပါ။\n"
        "ဥပမာ — Movie Name (2026) 1080p Myanmar Audio"
    )
    return True


async def uploader_caption_handler(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not is_admin_user(user.id):
        return False

    uid = str(user.id)
    session = uploader_sessions.get(uid)
    if not session or session.get("state") != "caption":
        return False

    caption = str(message.text or message.caption or "").strip()
    if not caption:
        await message.reply_text("📝 Caption လေး ပို့ပေးပါဦး။")
        return True

    session["caption"] = caption
    session["state"] = "preview"

    try:
        await context.bot.send_media_group(
            chat_id=user.id,
            media=[
                InputMediaPhoto(
                    media=session["poster_file_id"],
                ),
                InputMediaVideo(
                    media=session["video_file_id"],
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
        print("Uploader preview error:", e)
        await message.reply_text(
            "❌ Preview ပို့မရသေးဘူး။ ထပ်စမ်းကြည့်ပါ။"
        )

    return True


async def cancel_upload(update, context):
    query = update.callback_query
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await query.answer("Admin only", show_alert=True)
        return

    clear_uploader(user.id)
    await query.answer("Cancelled")
    try:
        await query.edit_message_text("❌ Movie upload cancelled.")
    except Exception:
        await context.bot.send_message(
            chat_id=user.id,
            text="❌ Movie upload cancelled."
        )


async def publish_upload(update, context):
    query = update.callback_query
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await query.answer("Admin only", show_alert=True)
        return

    uid = str(user.id)
    session = uploader_sessions.get(uid)
    if not session or session.get("state") != "preview":
        await query.answer("Upload session မရှိတော့ဘူး။", show_alert=True)
        return

    storage_id = get_storage_channel_id()
    if not storage_id:
        await query.answer("Storage Channel ID မသိသေးဘူး။", show_alert=True)
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ Private Movie Storage Channel ID ကို auto-detect မလုပ်ရသေးဘူး။\n\n"
                "Bot ကို Storage Channel ထဲမှာ Admin ထားပြီး၊ အဲဒီ Channel ထဲကို test message တစ်ခု post လုပ်ပေးပါ။\n"
                "ပြီးရင် ဒီ movie ကို ပြန် upload လုပ်ပါ။"
            ),
        )
        return

    try:
        sent = await context.bot.send_media_group(
            chat_id=int(storage_id),
            media=[
                InputMediaPhoto(
                    media=session["poster_file_id"],
                ),
                InputMediaVideo(
                    media=session["video_file_id"],
                    caption=session["caption"],
                ),
            ],
        )

        sent = sorted(sent, key=lambda m: m.message_id)
        movie = build_movie_from_messages(
            sent,
            session["caption"],
        )
        if not movie:
            raise RuntimeError("Could not build movie record from published album")

        movie["storage_channel"] = str(storage_id)
        save_movie_record(movie)

        group_id = str(
            getattr(sent[0], "media_group_id", "") or ""
        )
        if group_id:
            published_album_ids.add(
                (str(storage_id), group_id)
            )

        clear_uploader(user.id)
        await query.answer("Published 🎬")
        try:
            await query.edit_message_text(
                "✅ <b>PUBLISHED</b>\n\n"
                + html.escape(str(movie.get("title", "Movie")))
                + "\n\nMini App ထဲမှာလည်း ထည့်ပြီးပါပြီ။",
                parse_mode="HTML",
            )
        except Exception:
            await context.bot.send_message(
                chat_id=user.id,
                text="✅ Movie published successfully."
            )

        print(
            "Admin published movie:",
            movie.get("title"),
            "Storage:",
            storage_id,
            "Messages:",
            movie.get("storage_message_ids"),
        )

    except Exception as e:
        print("Uploader publish error:", e)
        await query.answer("Publish မအောင်မြင်သေးဘူး။", show_alert=True)
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "❌ Publish မအောင်မြင်သေးဘူး။\n\n"
                "Storage Channel ထဲမှာ Bot က post/media ပို့ခွင့်ရှိမရှိ စစ်ပေးပါ။\n"
                f"Error: {e}"
            ),
        )


async def cancel_upload_command(update, context):
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        return
    clear_uploader(user.id)
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

    # Upload controls are admin-only and must remain usable even when
    # the admin is not a member of the public gate channel.
    if data == "uploader_publish":
        return await publish_upload(update, context)

    if data == "uploader_cancel":
        return await cancel_upload(update, context)

    if data == "check_join":
        return await check_join(
            update,
            context,
        )

    # Every normal bot feature callback is re-checked. This means a user
    # who leaves the gate channel later is locked again immediately.
    if not await is_member(
        context.bot,
        query.from_user.id,
    ):
        await query.answer(
            "🔒 Movie Myanmar Channel ကို Join အရင်လုပ်ပါ။",
            show_alert=True,
        )

        try:
            await query.message.reply_text(
                "🎬 Movie Myanmar ကို Join လုပ်ပြီးမှ သုံးလို့ရမယ်နော် 😝",
                reply_markup=join_keyboard(),
            )
        except Exception:
            pass

        return

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

def is_movie_storage_channel(message):
    """Return True only for the configured/learned private storage channel."""
    if not message or not message.chat:
        return False

    chat_id = str(message.chat_id)

    # Explicit Render environment variable wins.
    if MOVIE_STORAGE_CHAT_ID:
        return chat_id == MOVIE_STORAGE_CHAT_ID

    # If we already learned the channel, never accept another
    # private channel by accident.
    known = get_storage_channel_id()
    if known:
        return chat_id == known

    username = (
        message.chat.username
        or ""
    ).strip().lower()

    expected_main = MAIN_CHANNEL.lstrip("@").lower()

    if username == expected_main:
        return False

    # First private channel post becomes the storage channel.
    # The bot only receives channel_post updates from channels
    # where it has access/admin rights.
    return not username


def build_storage_post_link(message):
    chat_id = str(message.chat_id)
    message_id = message.message_id

    if chat_id.startswith("-100") and message_id:
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


def build_movie_from_messages(messages, caption_override=None):
    """Build one canonical movie record from a Telegram media group."""
    messages = [m for m in messages if m]
    if not messages:
        return None

    messages.sort(key=lambda m: m.message_id)

    video_message = next(
        (
            m for m in messages
            if getattr(m, "video", None)
        ),
        None,
    )

    photo_message = next(
        (
            m for m in messages
            if getattr(m, "photo", None)
        ),
        None,
    )

    # Some clients may send the movie as a video document.
    if video_message is None:
        video_message = next(
            (
                m for m in messages
                if (
                    getattr(m, "document", None)
                    and str(
                        getattr(m.document, "mime_type", "")
                        or ""
                    ).lower().startswith("video/")
                )
            ),
            None,
        )

    caption = str(caption_override or "").strip()
    if not caption:
        for m in messages:
            candidate = str(
                getattr(m, "caption", "")
                or getattr(m, "text", "")
                or ""
            ).strip()
            if candidate:
                caption = candidate
                break

    if video_message is None:
        # A poster-only channel post is not a complete movie.
        return None

    source = video_message
    text = caption or "Untitled Movie"
    lines = [
        x.strip()
        for x in text.splitlines()
        if x.strip()
    ]
    title = lines[0] if lines else "Untitled Movie"

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

    poster_file_id = ""
    if photo_message and photo_message.photo:
        poster_file_id = photo_message.photo[-1].file_id

    movie = {
        "title": title,
        "year": year,
        "quality": (
            quality_match.group(1).upper()
            if quality_match else ""
        ),
        "audio": (
            audio_match.group(0)
            if audio_match else ""
        ),
        "original_name": title,
        "genres": detect_genres(text),
        "description": text,
        "post_link": build_storage_post_link(source),
        "chat_id": str(source.chat_id),
        "message_id": source.message_id,
        "poster_file_id": poster_file_id,
        "media_group_id": str(
            getattr(source, "media_group_id", "") or ""
        ),
        "storage_message_ids": [
            m.message_id for m in messages
        ],
    }

    if getattr(source, "video", None):
        movie["video_file_id"] = source.video.file_id
    elif getattr(source, "document", None):
        movie["video_file_id"] = source.document.file_id

    movie["id"] = generate_movie_id(
        source.chat_id,
        source.message_id,
    )

    return movie


def movie_already_saved(movie):
    if not movie:
        return False

    chat_id = str(movie.get("chat_id", ""))
    message_id = movie.get("message_id")
    group_id = str(movie.get("media_group_id", ""))
    movie_id = str(movie.get("id", ""))

    with movie_lock:
        for old in movie_database:
            if movie_id and str(old.get("id", "")) == movie_id:
                return True
            if (
                chat_id
                and str(old.get("chat_id", "")) == chat_id
                and message_id
                and old.get("message_id") == message_id
            ):
                return True
            if (
                group_id
                and str(old.get("media_group_id", "")) == group_id
                and str(old.get("storage_channel", "")) == chat_id
            ):
                return True
    return False


def save_movie_record(movie):
    if not movie:
        return False

    replaced = False
    with movie_lock:
        for i, old in enumerate(movie_database):
            same = (
                str(old.get("id", ""))
                == str(movie.get("id", ""))
            )
            if not same:
                same = (
                    str(old.get("chat_id", ""))
                    == str(movie.get("chat_id", ""))
                    and old.get("message_id")
                    == movie.get("message_id")
                )
            if same:
                movie_database[i] = movie
                replaced = True
                break

        if not replaced:
            movie_database.append(movie)

    save_movies()
    invalidate_carousel()
    return not replaced


async def save_album_as_movie(messages, caption=None):
    if not messages:
        return

    messages = sorted(
        [m for m in messages if m],
        key=lambda m: m.message_id,
    )

    group_id = str(
        getattr(messages[0], "media_group_id", "") or ""
    )
    key = (
        str(messages[0].chat_id),
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

    movie["storage_channel"] = str(
        messages[0].chat_id
    )

    if movie_already_saved(movie):
        published_album_ids.add(key)
        return

    save_movie_record(movie)
    published_album_ids.add(key)

    print(
        "Saved PRIVATE movie album:",
        movie.get("title"),
        "Storage Chat:",
        messages[0].chat_id,
        "Messages:",
        movie.get("storage_message_ids"),
        "Link:",
        movie.get("post_link"),
        "ID:",
        movie.get("id"),
        "Total:",
        len(movie_database),
    )


async def process_album_later(key):
    try:
        await asyncio.sleep(1.2)
        messages = album_buffers.pop(key, [])
        album_tasks.pop(key, None)
        if messages:
            await save_album_as_movie(messages)
    except Exception as e:
        print("Album processing error:", e)


async def channel_post_handler(update, context):
    message = update.channel_post

    if not message or not is_movie_storage_channel(message):
        return

    global AUTO_STORAGE_CHAT_ID

    # Learn and persist the first valid private storage channel.
    if not MOVIE_STORAGE_CHAT_ID:
        discovered = str(message.chat_id)
        if not AUTO_STORAGE_CHAT_ID:
            AUTO_STORAGE_CHAT_ID = discovered
            save_storage_channel_id(discovered)
            print(
                "Auto-detected Movie Storage Channel:",
                discovered,
            )

    group_id = str(
        getattr(message, "media_group_id", "") or ""
    )

    if group_id:
        key = (
            str(message.chat_id),
            group_id,
        )
        album_buffers.setdefault(key, []).append(message)

        task = album_tasks.get(key)
        if task is None or task.done():
            album_tasks[key] = asyncio.create_task(
                process_album_later(key)
            )
        return

    # Single-post fallback. This also supports older stored movies.
    movie = build_movie_from_messages([message])
    if not movie:
        return

    movie["storage_channel"] = str(message.chat_id)
    save_movie_record(movie)

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
# MINI APP
# ============================================================

MUSEUM_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover"><title>Movie Myanmar — Cinema Museum</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#080604;color:#f0dfba;font-family:Georgia,serif;touch-action:none}button{font:inherit}.stage{position:fixed;inset:0;background:#080604;overflow:hidden}.scene{position:absolute;inset:-3%;background-position:center;background-size:cover;background-repeat:no-repeat;transform:scale(1.04);transition:background-image .5s ease,transform .7s ease,filter .7s ease;filter:saturate(.94) contrast(1.04)}.scene:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(5,4,3,.16),rgba(5,4,3,.12) 50%,rgba(4,3,2,.58));pointer-events:none}.vignette{position:absolute;inset:0;background:radial-gradient(circle at 50% 48%,transparent 35%,rgba(0,0,0,.55) 100%);pointer-events:none}.grain{position:absolute;inset:0;opacity:.08;background-image:radial-gradient(#fff 0.5px,transparent .6px);background-size:5px 5px;mix-blend-mode:soft-light;pointer-events:none}.header{position:absolute;top:0;left:0;right:0;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;z-index:10;background:linear-gradient(rgba(5,4,3,.68),transparent)}.title{font-size:18px;letter-spacing:2px;text-shadow:0 2px 10px #000}.hint{font-size:10px;letter-spacing:1.2px;opacity:.78}.rooms{position:absolute;left:12px;right:12px;bottom:12px;display:flex;gap:7px;overflow-x:auto;z-index:10;padding:6px 0;scrollbar-width:none}.rooms::-webkit-scrollbar{display:none}.rooms button{flex:0 0 auto;border:1px solid rgba(220,178,88,.5);background:rgba(20,14,9,.78);color:#f1dfb5;border-radius:4px;padding:9px 11px;font-size:10px;letter-spacing:.8px;backdrop-filter:blur(8px);box-shadow:0 5px 20px #0008}.rooms button.active{background:#8d6326;color:#fff4d6}.hotspot{position:absolute;z-index:8;border:1px solid rgba(242,206,135,.68);background:linear-gradient(180deg,rgba(49,31,15,.9),rgba(13,9,6,.88));color:#f4dfad;padding:9px 11px;border-radius:4px;font-size:10px;letter-spacing:1px;box-shadow:0 5px 22px #0009;animation:glow 2.8s ease-in-out infinite}.hotspot:hover{background:#9a6d2b}.h1{left:8%;top:47%}.h2{right:8%;top:47%}.h3{left:45%;bottom:20%}.h4{right:7%;bottom:28%}@keyframes glow{50%{box-shadow:0 0 22px rgba(219,166,66,.45),0 5px 22px #0009}}.badge{position:absolute;left:16px;bottom:72px;z-index:9;background:rgba(12,8,5,.72);border-left:3px solid #b98a3a;padding:10px 12px;max-width:72%;backdrop-filter:blur(7px)}.badge strong{display:block;font-size:13px;letter-spacing:1.5px}.badge span{font-size:10px;opacity:.8}.loading,.lock,.modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;background:rgba(3,2,1,.78);backdrop-filter:blur(8px)}.box,.panel{width:min(88vw,460px);background:linear-gradient(145deg,#2a1d12,#0d0906);border:1px solid #a77a35;box-shadow:0 20px 70px #000; padding:26px;border-radius:8px;text-align:center}.box h1{margin:0 0 10px;color:#e7c77e;letter-spacing:2px}.box p{opacity:.78;font-size:13px;line-height:1.6}.box button,.panel button{border:1px solid #c08c3b;background:#8d6227;color:#fff1cc;padding:10px 18px;border-radius:4px}.hidden{display:none!important}.panel{text-align:left;max-height:82vh;overflow:auto}.panel img{width:100%;max-height:55vh;object-fit:contain;background:#080604;border-radius:4px}.panel h2{color:#e6c477}.posterWall{position:absolute;left:5%;right:5%;top:18%;bottom:20%;display:grid;grid-template-columns:repeat(4,1fr);gap:12px;z-index:6;align-items:center}.frame{padding:5px;background:linear-gradient(135deg,#b88839,#5b3d18);box-shadow:0 10px 28px #0009}.frame img{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;background:#15100c}.empty{grid-column:1/-1;text-align:center;background:rgba(9,6,4,.72);padding:18px;border:1px solid #a77a35}.exhibitDots{position:absolute;left:50%;transform:translateX(-50%);bottom:23%;z-index:7;display:flex;gap:8px}.dot{width:7px;height:7px;border-radius:50%;background:#d5a34a;box-shadow:0 0 10px #d5a34a}.roomName{position:absolute;top:14%;left:50%;transform:translateX(-50%);z-index:7;background:rgba(7,4,2,.68);border:1px solid rgba(210,169,88,.55);padding:8px 16px;letter-spacing:2px;font-size:12px}.arrow{position:absolute;z-index:8;top:50%;transform:translateY(-50%);width:38px;height:52px;border:1px solid #c49a4e;background:#120c08aa;color:#f3dba6;border-radius:4px;font-size:22px}.leftArrow{left:10px}.rightArrow{right:10px}@media(max-width:650px){.posterWall{grid-template-columns:repeat(2,1fr);top:20%;bottom:23%;gap:8px}.hint{display:none}.title{font-size:15px}.badge{bottom:67px;max-width:82%}.hotspot{font-size:9px;padding:8px}.h1{left:4%;top:48%}.h2{right:4%;top:48%}}
</style></head><body><div class="stage"><div id="scene" class="scene"></div><div class="vignette"></div><div class="grain"></div><div id="roomName" class="roomName">MAIN LOBBY</div><div id="hotspots"></div><div id="badge" class="badge"><strong>MOVIE MYANMAR</strong><span>CINEMA MUSEUM • WALK THROUGH THE COLLECTION</span></div><button id="prev" class="arrow leftArrow">‹</button><button id="next" class="arrow rightArrow">›</button></div><div class="header"><div id="title" class="title">MAIN LOBBY</div><div class="hint">DRAG • LOOK AROUND</div></div><div id="rooms" class="rooms"></div><div id="loading" class="loading"><div class="box"><h1>CINEMA MUSEUM</h1><p>Entering Movie Myanmar Cinema Museum…</p></div></div><div id="lock" class="lock hidden"><div class="box"><h1>MEMBERS ONLY</h1><p id="lockmsg">Movie Myanmar Channel ကို Join လုပ်ပြီးမှ Museum ထဲဝင်လို့ရမယ်နော် 😝</p><button id="join">JOIN CHANNEL</button></div></div><div id="modal" class="modal hidden"><div id="panel" class="panel"></div></div><script>
const ASSETS = {"lobby": "data:image/jpeg;base64,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", "cinema": "data:image/jpeg;base64,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", "exhibition": "data:image/jpeg;base64,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", "awards": "data:image/jpeg;base64,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", "studio": "data:image/jpeg;base64,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", "cyber": "data:image/jpeg;base64,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", "art": "data:image/jpeg;base64,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", "history": "data:image/jpeg;base64,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"};
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}const INIT=tg?.initData||'';const MAIN_CHANNEL='https://t.me/moviemyanmar567';
const rooms={lobby:['MAIN LOBBY','lobby','Museum entrance • Cyber Hero • film history'],cinema:['MAIN CINEMA','cinema','Classic cinema • screen • projector'],exhibition:['MOVIE EXHIBITION','exhibition','Movie Myanmar poster collection'],awards:['AWARDS HALL','awards','Trophies • achievements • milestones'],studio:['CREATIVE STUDIO','studio','Lin Myat • Xiao Yu • creator room'],cyber:['CYBER HALL','cyber','Cyber Hero • technology • future cinema'],art:['ART GALLERY','art','Movie artwork • Xiao Yu artwork'],history:['FILM PROPS / HISTORY','history','Cameras • reels • projector • props']};
let keys=Object.keys(rooms),idx=0,movies=[];const $=id=>document.getElementById(id);function api(path){return fetch(path,{headers:{'X-Telegram-Init-Data':INIT}}).then(r=>r.json())}function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function roomKey(){return keys[idx]}function setRoom(k){idx=Math.max(0,keys.indexOf(k));let r=rooms[k];$('scene').style.backgroundImage=`url("${ASSETS[r[1]]}")`;$('scene').style.transform='scale(1.04)';$('title').textContent=r[0];$('roomName').textContent=r[0];$('badge').innerHTML='<strong>'+r[0]+'</strong><span>'+r[2]+'</span>';document.querySelectorAll('.rooms button').forEach(b=>b.classList.toggle('active',b.dataset.room===k));renderHotspots(k);if(k==='exhibition')loadMovies();}
function renderHotspots(k){let html='';if(k==='lobby'){html='<button class="hotspot h1" onclick="setRoom(\'exhibition\')">MOVIE EXHIBITION →</button><button class="hotspot h2" onclick="setRoom(\'cinema\')">← MAIN CINEMA</button><button class="hotspot h3" onclick="setRoom(\'studio\')">CREATIVE STUDIO</button><button class="hotspot h4" onclick="setRoom(\'awards\')">AWARDS HALL</button>'}else if(k==='cinema'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'MAIN CINEMA\',\'Classic theatre room with a large screen, red curtain, cinema seating and projector beam.\')">VIEW CINEMA</button>'}else if(k==='exhibition'){html='<div id="posterWall" class="posterWall"><div class="empty">Loading Movie Myanmar collection…</div></div><button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button>'}else if(k==='awards'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'AWARDS HALL\',\'A glass-case hall for Movie Myanmar achievements, milestones and future awards.\')">VIEW COLLECTION</button>'}else if(k==='studio'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'CREATIVE STUDIO\',\'The creator room for Lin Myat and Xiao Yu — books, cameras, cinema notes and warm old-money lighting.\')">CREATIVE STUDIO</button>'}else if(k==='cyber'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'CYBER HALL\',\'Cyber Hero, cinema technology and futuristic exhibits.\')">CYBER HERO</button>'}else if(k==='art'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'ART GALLERY\',\'Framed Movie Myanmar and Xiao Yu artwork displayed like a real gallery.\')">VIEW ART</button>'}else if(k==='history'){html='<button class="hotspot h1" onclick="setRoom(\'lobby\')">← MAIN LOBBY</button><button class="hotspot h2" onclick="showInfo(\'FILM PROPS / HISTORY\',\'Vintage cameras, film reels, projector, clapperboard and cinema history artifacts.\')">VIEW PROPS</button>'}$('hotspots').innerHTML=html}
function buildRooms(){keys.forEach(k=>{let b=document.createElement('button');b.dataset.room=k;b.textContent=rooms[k][0];b.onclick=()=>setRoom(k);$('rooms').appendChild(b)})}
function showInfo(title,text,img){$('panel').innerHTML='<button style="float:right" onclick="closeModal()">✕</button><h2>'+esc(title)+'</h2><p style="line-height:1.7;opacity:.82">'+esc(text)+'</p>'+(img?'<img src="'+img+'">':'');$('modal').classList.remove('hidden')};function closeModal(){$('modal').classList.add('hidden')}
async function loadMovies(){let wall=$('posterWall');if(!wall)return;try{let d=await api('/api/search?q=');movies=d.movies||[];if(!movies.length){wall.innerHTML='<div class="empty">No movies in the collection yet.</div>';return}wall.innerHTML=movies.slice(0,8).map(m=>'<button class="frame" onclick="openMovie(\''+encodeURIComponent(m.id)+'\')"><img src="/api/poster?id='+encodeURIComponent(m.id)+'" onerror="this.style.visibility=\'hidden\'" alt=""></button>').join('')}catch(e){wall.innerHTML='<div class="empty">Collection is loading…</div>'}}
async function openMovie(id){try{let m=await api('/api/movie?id='+decodeURIComponent(id));$('panel').innerHTML='<button style="float:right" onclick="closeModal()">✕</button><h2>'+esc(m.title||'Movie')+'</h2>'+(m.poster?'<img src="/api/poster?id='+encodeURIComponent(m.id)+'">':'')+'<p style="line-height:1.7;opacity:.82">'+esc(m.description||'Movie Myanmar collection exhibit.')+'</p>'+(m.link?'<a href="'+m.link+'" target="_blank" style="display:inline-block;padding:10px 16px;background:#8d6227;color:#fff1cc;text-decoration:none;border-radius:4px">OPEN MOVIE</a>':'');$('modal').classList.remove('hidden')}catch(e){showInfo('MOVIE EXHIBIT','Unable to open this exhibit right now.')}}
let sx=0,sy=0,drag=false;document.addEventListener('pointerdown',e=>{if(e.target.closest('button,.modal'))return;drag=true;sx=e.clientX;sy=e.clientY});document.addEventListener('pointermove',e=>{if(!drag)return;let dx=e.clientX-sx,dy=e.clientY-sy;sx=e.clientX;sy=e.clientY;$('scene').style.transform=`scale(1.06) translate(${dx*.15}px,${dy*.08}px)`});document.addEventListener('pointerup',()=>{drag=false;$('scene').style.transform='scale(1.04)'});$('prev').onclick=()=>setRoom(keys[(idx-1+keys.length)%keys.length]);$('next').onclick=()=>setRoom(keys[(idx+1)%keys.length]);
async function start(){try{if(!INIT)throw {message:'Telegram ထဲကနေ Museum ကိုဖွင့်ပါ'};let a=await api('/api/auth');if(!a.authorized)throw a;$('loading').classList.add('hidden');buildRooms();setRoom('lobby')}catch(e){$('loading').classList.add('hidden');$('lock').classList.remove('hidden');$('lockmsg').textContent=e.message||'Movie Myanmar Channel ကို Join လုပ်ပြီးမှ Museum ထဲဝင်လို့ရမယ်နော် 😝';$('join').onclick=()=>tg?.openTelegramLink?tg.openTelegramLink(MAIN_CHANNEL):location.href=MAIN_CHANNEL}}start();
</script></body></html>'''

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

.loader{
    text-align:center;
    padding:20px;
    color:#aaa
}

.splash{
    position:fixed;
    inset:0;
    z-index:100;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:24px;
    background:
        radial-gradient(circle at 50% 20%, #35156b 0, #0b0913 42%, #05050a 100%);
    text-align:center;
}

.splash-card{
    width:100%;
    max-width:390px;
    padding:28px 22px;
    border:1px solid #3b2b59;
    border-radius:24px;
    background:rgba(12,10,21,.86);
    box-shadow:0 20px 70px #0009;
}

.splash-logo{
    font-size:29px;
    font-weight:900;
    letter-spacing:1.4px;
}

.splash-logo span{
    color:#6cf6ff;
}

.splash-sub{
    color:#aaa3bd;
    font-size:12px;
    margin-top:7px;
}

.spinner{
    width:34px;
    height:34px;
    margin:25px auto 15px;
    border:3px solid #312747;
    border-top-color:#6cf6ff;
    border-right-color:#bb5cff;
    border-radius:50%;
    animation:spin .8s linear infinite;
}

@keyframes spin{to{transform:rotate(360deg)}}

.splash-text{
    color:#e9e3f7;
    font-size:13px;
    line-height:1.6;
}

.gate-buttons{
    display:grid;
    gap:10px;
    margin-top:20px;
}

.gate-buttons button{
    width:100%;
    padding:13px;
    border:0;
    border-radius:13px;
    font-weight:900;
    background:linear-gradient(135deg,#5af5ff,#bb5cff);
    color:#07070d;
}

.gate-buttons button.secondary{
    background:#211b31;
    color:#fff;
    border:1px solid #3d3157;
}
</style>
</head>

<body>

<main id="appRoot" class="app" style="display:none">

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

<div id="splash" class="splash">
    <div class="splash-card">
        <div class="splash-logo">MOVIE <span>MYANMAR</span></div>
        <div class="splash-sub">Xiao Yu CyB • Cinema Mini App</div>
        <div class="spinner"></div>
        <div id="splashText" class="splash-text">Loading Movie Myanmar...</div>
    </div>
</div>

<div id="gate" class="splash" style="display:none">
    <div class="splash-card">
        <div class="splash-logo">MOVIE <span>MYANMAR</span></div>
        <div class="splash-sub">Xiao Yu CyB • Cinema Mini App</div>
        <div id="gateText" class="splash-text" style="margin-top:18px">
            Channel ကို Join ပြီးမှ Movie Myanmar သုံးလို့ရမယ်နော် 😝
        </div>
        <div class="gate-buttons">
            <button onclick="joinChannel()">🎬 Join Movie Myanmar</button>
            <button class="secondary" onclick="checkAccess()">✅ I Joined</button>
        </div>
    </div>
</div>

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

const tg = window.Telegram?.WebApp;

if(tg){
    tg.ready();
    tg.expand();
}

const $ = id => document.getElementById(id);

const MAIN_CHANNEL_URL = 'https://t.me/moviemyanmar567';

let movies = [];
let booting = false;
let accessTimer = null;

function esc(s){
    return String(s || '').replace(
        /[&<>\"]/g,
        m => ({
            '&':'&amp;',
            '<':'&lt;',
            '>':'&gt;',
            '"':'&quot;'
        }[m])
    );
}

function sleep(ms){
    return new Promise(resolve => setTimeout(resolve, ms));
}

function showSplash(text){
    $('gate').style.display = 'none';
    $('appRoot').style.display = 'none';
    $('splash').style.display = 'flex';
    $('splashText').textContent = text || 'Loading Movie Myanmar...';
}

function showGate(text){
    $('appRoot').style.display = 'none';
    $('splash').style.display = 'none';
    $('gate').style.display = 'flex';
    $('gateText').textContent = text || 'Channel ကို Join ပြီးမှ Movie Myanmar သုံးလို့ရမယ်နော် 😝';
}

function unlockApp(){
    $('splash').style.display = 'none';
    $('gate').style.display = 'none';
    $('appRoot').style.display = 'block';
}

function lockApp(text){
    if(accessTimer){
        clearInterval(accessTimer);
        accessTimer = null;
    }
    closeModal();
    showGate(text || 'Channel ကို Join ပြီးမှ Movie Myanmar သုံးလို့ရမယ်နော် 😝');
}

function posterUrl(id){
    const initData = tg?.initData || '';
    return '/api/poster?id=' +
        encodeURIComponent(id) +
        '&initData=' +
        encodeURIComponent(initData);
}

async function api(url, options={}){
    try{
        const headers = Object.assign({}, options.headers || {});
        if(tg?.initData){
            headers['X-Telegram-Init-Data'] = tg.initData;
        }

        const response = await fetch(
            url,
            Object.assign({}, options, {
                cache:'no-store',
                headers:headers
            })
        );

        const data = await response.json().catch(() => ({}));

        if(response.status === 401 || response.status === 403){
            if(data.reason === 'not_member'){
                lockApp(data.message);
            }
        }

        if(!response.ok){
            return Object.assign({}, data, {
                __error:true,
                __status:response.status
            });
        }

        return data;

    }catch(error){
        console.error(error);
        return {
            __error:true,
            __network:true
        };
    }
}

function card(m){
    const poster = m.poster ? posterUrl(m.id) : '';

    return `
    <article
        class="card"
        onclick="detail('${encodeURIComponent(m.id)}')"
    >
        ${
            poster
            ? `<img class="poster" src="${poster}" onerror="this.style.visibility='hidden'">`
            : `<div class="poster"></div>`
        }
        <div class="ct">
            <div class="title">${esc(m.title)}</div>
            <div class="meta">
                ${esc(m.year || '')}
                ${m.quality ? ' • ' + esc(m.quality) : ''}
            </div>
        </div>
    </article>
    `;
}

async function loadLatest(){
    $('rail').innerHTML = '<div class="loader">Loading movies...</div>';

    const data = await api('/api/latest');

    if(data.__error){
        return false;
    }

    movies = data.movies || [];

    $('rail').innerHTML = movies.length
        ? movies.map(card).join('')
        : '<div class="empty">No movies yet</div>';

    return true;
}

async function searchMovies(){
    const query = $('q').value.trim();
    if(!query) return;

    const data = await api(
        '/api/search?q=' + encodeURIComponent(query)
    );

    if(data.__error) return;

    renderResults('SEARCH RESULTS', data.movies || []);
}

function renderResults(title, arr){
    $('results').innerHTML = `
        <div class="sectionhead">
            <h2>${esc(title)}</h2>
            <span class="muted">${arr.length} found</span>
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
    const data = await api('/api/genres');

    if(data.__error){
        return false;
    }

    $('genres').innerHTML = (data.genres || [])
        .map(genre => `
            <button
                class="chip"
                onclick="genre('${encodeURIComponent(genre)}', this)"
            >${esc(genre)}</button>
        `)
        .join('');

    return true;
}

async function genre(encodedGenre, button){
    document.querySelectorAll('.chip').forEach(
        x => x.classList.remove('active')
    );

    if(button) button.classList.add('active');

    const data = await api(
        '/api/genre?name=' + encodedGenre
    );

    if(data.__error) return;

    renderResults(
        '🎭 ' + decodeURIComponent(encodedGenre),
        data.movies || []
    );
}

async function detail(encodedId){
    const movieId = decodeURIComponent(encodedId);

    const data = await api(
        '/api/movie?id=' + encodeURIComponent(movieId)
    );

    if(data.__error || !data.title) return;

    const poster = data.poster ? posterUrl(data.id) : '';

    $('modal').innerHTML = `
        <button class="close" onclick="closeModal()">✕</button>
        ${poster ? `<img src="${poster}" alt="">` : ''}
        <div class="mb">
            <h2>${esc(data.title)}</h2>
            <div class="meta">
                ${esc(data.year || '')}
                ${data.quality ? ' • ' + esc(data.quality) : ''}
                ${data.audio ? ' • ' + esc(data.audio) : ''}
            </div>
            <p>${esc(data.description || 'Movie Myanmar')}</p>
            ${
                data.link
                ? `<a href="${esc(data.link)}" target="_blank" rel="noopener"><button class="open">🎬 Open Movie</button></a>`
                : ''
            }
        </div>
    `;

    $('overlay').style.display = 'flex';
}

function closeModal(){
    $('overlay').style.display = 'none';
}

function openAi(){
    if(tg && typeof tg.sendData === 'function'){
        tg.sendData('ai');
        setTimeout(() => tg.close(), 150);
    }else{
        alert('Open Xiao Yu AI from the Telegram bot menu 😝');
    }
}

function joinChannel(){
    try{
        if(tg && typeof tg.openTelegramLink === 'function'){
            tg.openTelegramLink(MAIN_CHANNEL_URL);
        }else{
            window.open(MAIN_CHANNEL_URL, '_blank');
        }
    }catch(e){
        window.open(MAIN_CHANNEL_URL, '_blank');
    }
}

async function checkAccess(){
    await boot(true);
}

function startAccessWatch(){
    if(accessTimer) clearInterval(accessTimer);

    accessTimer = setInterval(async () => {
        const auth = await api('/api/auth');
        if(auth.__error) return;
        if(!auth.authorized){
            lockApp(
                auth.message ||
                'Channel ကို Join ပြီးမှ Movie Myanmar သုံးလို့ရမယ်နော် 😝'
            );
        }
    }, 20000);
}

async function boot(force=false){
    if(booting && !force) return;
    booting = true;

    try{
        if(!tg || !tg.initData){
            showGate('Telegram ထဲကနေ Movie Myanmar App ကို ဖွင့်ပါ။');
            return;
        }

        showSplash('Loading Movie Myanmar...');

        // Keep retrying instead of ever displaying "No movies yet" while
        // Render is still waking or the data endpoint is temporarily busy.
        while(true){
            const auth = await api('/api/auth');

            if(auth.authorized){
                showSplash('Movie Myanmar is waking up...');

                const latestOk = await loadLatest();
                const genresOk = await showGenres();

                if(latestOk && genresOk){
                    unlockApp();
                    startAccessWatch();
                    return;
                }

                showSplash('Movie Myanmar is waking up... Please wait...');
                await sleep(2500);
                continue;
            }

            if(auth.reason === 'not_member'){
                showGate(
                    auth.message ||
                    'Channel ကို Join ပြီးမှ Movie Myanmar သုံးလို့ရမယ်နော် 😝'
                );
                return;
            }

            if(auth.reason === 'invalid_init_data'){
                showGate('Telegram ထဲကနေ Movie Myanmar App ကို ပြန်ဖွင့်ပါ။');
                return;
            }

            showSplash('Movie Myanmar is waking up...');
            await sleep(2500);
        }
    }finally{
        booting = false;
    }
}

$('q').addEventListener('keydown', e => {
    if(e.key === 'Enter') searchMovies();
});

boot();
</script>

</body>
</html>
'''


# ============================================================
# ADMIN DASHBOARD
# ============================================================

ADMIN_DASHBOARD_HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Movie Myanmar Admin</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#07070d;color:#f6f4ff;font-family:Arial,sans-serif}
.wrap{min-height:100vh;padding:18px 14px 30px;background:radial-gradient(circle at 20% 0,#22104a 0,#0b0913 38%,#05050a 100%)}
.top{padding:8px 2px 18px}.brand{font-size:25px;font-weight:900}.brand span{color:#6cf6ff}.sub{color:#aaa3bd;font-size:12px;margin-top:5px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.stat{background:#100e18;border:1px solid #302846;border-radius:16px;padding:15px}.label{font-size:11px;color:#9d96ad}.value{font-size:25px;font-weight:900;margin-top:7px}
.section{margin-top:18px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.head h2{font-size:16px;margin:0}.muted{color:#8f899f;font-size:11px}
.list{display:grid;gap:8px}.row{display:flex;justify-content:space-between;gap:10px;background:#100e18;border:1px solid #29233a;border-radius:13px;padding:12px}.title{font-weight:800;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.count{font-weight:900;color:#6cf6ff;white-space:nowrap}.loading{text-align:center;padding:35px;color:#aaa3bd}.err{text-align:center;padding:35px;color:#ff9b9b}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand">MOVIE <span>MYANMAR</span></div>
    <div class="sub">🛠 Admin Dashboard • Xiao Yu CyB</div>
  </div>
  <div id="content" class="loading">Loading dashboard...</div>
</div>
<script>
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}
const $=id=>document.getElementById(id);
function esc(s){return String(s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]))}
async function api(url){
  try{
    const headers={};
    if(tg?.initData) headers['X-Telegram-Init-Data']=tg.initData;
    const r=await fetch(url,{cache:'no-store',headers});
    const d=await r.json().catch(()=>({}));
    if(!r.ok) return Object.assign({},d,{__error:true});
    return d;
  }catch(e){return {__error:true}}
}
async function boot(){
  const d=await api('/api/admin');
  if(d.__error){$('content').innerHTML='<div class="err">🔒 Admin only<br><br>'+esc(d.message||'Access denied')+'</div>';return}
  const top=(d.top_movies||[]).map(x=>`<div class="row"><div class="title">${esc(x.title)}</div><div class="count">${x.views}</div></div>`).join('');
  $('content').innerHTML=`
    <div class="grid">
      <div class="stat"><div class="label">TOTAL MOVIES</div><div class="value">${d.total_movies}</div></div>
      <div class="stat"><div class="label">USERS</div><div class="value">${d.total_users}</div></div>
      <div class="stat"><div class="label">SEARCHES</div><div class="value">${d.total_searches}</div></div>
      <div class="stat"><div class="label">AI REQUESTS</div><div class="value">${d.total_ai_requests}</div></div>
    </div>
    <section class="section">
      <div class="head"><h2>🔥 Most Viewed</h2><span class="muted">Mini App</span></div>
      <div class="list">${top||'<div class="loading">No movie views yet</div>'}</div>
    </section>
    <section class="section">
      <div class="head"><h2>🎬 Recent Movies</h2><span class="muted">Latest 10</span></div>
      <div class="list">${(d.recent_movies||[]).map(x=>`<div class="row"><div class="title">${esc(x.title)}${x.year?' ('+esc(x.year)+')':''}</div></div>`).join('')}</div>
    </section>`;
}
boot();
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

        content_type = "image/jpeg"

        lower_path = path.lower()

        if lower_path.endswith(
            ".png"
        ):
            content_type = "image/png"

        elif lower_path.endswith(
            ".webp"
        ):
            content_type = "image/webp"

        elif lower_path.endswith(
            ".gif"
        ):
            content_type = "image/gif"

        poster_cache[file_id] = (
            raw,
            content_type,
        )

        return poster_cache[file_id]

    except Exception as e:
        print(
            "poster error:",
            e,
        )

        return None, "image/jpeg"


# ============================================================
# MINI APP AUTH / MEMBERSHIP GATE
# ============================================================

# The HTML shell at /app is public, but every data endpoint is protected
# by Telegram's signed initData plus a live membership check. This prevents
# non-members from using Search / Latest / Genres / movie links / posters.

def webapp_auth_response(headers, qs):
    allowed, uid, reason = authorize_webapp_request(
        headers,
        qs,
    )

    if allowed:
        return {
            "ok": True,
            "authorized": True,
            "admin": bool(is_admin_user(uid)),
            "user_id": uid,
        }, 200

    if reason == "not_member":
        return {
            "ok": False,
            "authorized": False,
            "reason": reason,
            "message": "Movie Myanmar Channel ကို Join လုပ်ပြီးမှ သုံးလို့ရမယ်နော် 😝",
            "join_url": MAIN_CHANNEL_URL,
        }, 403

    return {
        "ok": False,
        "authorized": False,
        "reason": reason,
        "message": "Telegram Mini App session မမှန်ပါ။ Telegram ထဲကနေ ပြန်ဖွင့်ပါ။",
    }, 401


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
            "no-store",
        )

        self.send_header(
            "Content-Length",
            str(len(data)),
        )

        self.end_headers()

        self.wfile.write(
            data
        )

    def send_json(
        self,
        obj,
        status=200,
    ):
        self.send_bytes(
            json.dumps(
                obj,
                ensure_ascii=False,
            ).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
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
            # MINI APP AUTH
            # ------------------------------------------------

            if path == "/api/auth":
                payload, status_code = webapp_auth_response(
                    self.headers,
                    qs,
                )
                return self.send_json(
                    payload,
                    status_code,
                )

            # Every other /api/* route requires valid Telegram initData
            # and a live membership check.
            if path.startswith("/api/"):
                allowed, uid, reason = authorize_webapp_request(
                    self.headers,
                    qs,
                )

                if not allowed:
                    payload, status_code = webapp_auth_response(
                        self.headers,
                        qs,
                    )
                    return self.send_json(
                        payload,
                        status_code,
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

            if path == "/museum":
                return self.send_bytes(
                    MUSEUM_HTML.encode(
                        "utf-8"
                    ),
                    "text/html; charset=utf-8",
                )

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

            if path == "/admin":
                return self.send_bytes(
                    ADMIN_DASHBOARD_HTML.encode(
                        "utf-8"
                    ),
                    "text/html; charset=utf-8",
                )

            # ------------------------------------------------
            # ADMIN DASHBOARD DATA
            # ------------------------------------------------

            if path == "/api/admin":
                if not is_admin_user(uid):
                    return self.send_json(
                        {
                            "ok": False,
                            "message": "Admin only.",
                        },
                        403,
                    )

                with movie_lock:
                    total_movies = len(movie_database)
                    recent_movies = [
                        mini_movie(movie)
                        for movie in list(reversed(movie_database))[:10]
                    ]

                with stats_lock:
                    total_users = len(admin_stats.get("users", {}))
                    total_searches = int(admin_stats.get("searches", 0) or 0)
                    total_ai_requests = int(admin_stats.get("ai_requests", 0) or 0)
                    view_map = dict(admin_stats.get("movie_views", {}))

                top_movies = []
                for movie_id, views in sorted(
                    view_map.items(),
                    key=lambda item: int(item[1] or 0),
                    reverse=True,
                )[:10]:
                    movie = find_movie_by_id(movie_id)
                    if not movie:
                        continue
                    top_movies.append(
                        {
                            "id": str(movie_id),
                            "title": movie.get("title", "Movie"),
                            "views": int(views or 0),
                        }
                    )

                return self.send_json(
                    {
                        "ok": True,
                        "total_movies": total_movies,
                        "total_users": total_users,
                        "total_searches": total_searches,
                        "total_ai_requests": total_ai_requests,
                        "top_movies": top_movies,
                        "recent_movies": recent_movies,
                    }
                )

            # ------------------------------------------------
            # ALL / LATEST MOVIES
            # ------------------------------------------------

            if path in (
                "/api/movies",
                "/api/latest",
            ):
                raw_limit = qs.get(
                    "limit",
                    [""],
                )[0].strip()

                if raw_limit:
                    try:
                        limit = max(1, int(raw_limit))
                    except Exception:
                        limit = None
                else:
                    limit = None

                movies = latest(limit)

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
                query = qs.get(
                    "q",
                    [""],
                )[0]

                results = movie_search(
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
                genre = qs.get(
                    "name",
                    [""],
                )[0]

                if genre == "All":
                    results = latest()

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
                        ]

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
                movie_id = qs.get(
                    "id",
                    [""],
                )[0]

                movie = find_movie_by_id(
                    movie_id
                )

                if movie:
                    record_movie_view(movie_id)

                return self.send_json(
                    mini_movie(movie)
                    if movie
                    else {}
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

    # Start the HTTP health server immediately. Render expects the
    # service port to be listening while the Telegram API startup
    # calls are still running. Starting it first prevents Render from
    # treating a slow Telegram/network startup as an early exit.
    threading.Thread(
        target=start_health_server,
        daemon=True,
        name="movie-http-server",
    ).start()

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

    # Telegram may receive large movies as Documents (MKV etc.).
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
        get_storage_channel_id() or "(auto-detect pending)",
    )

    print(
        "GitHub Movie Backup:",
        "ON" if github_enabled() else "OFF (set GITHUB_TOKEN)",
    )

    if github_enabled():
        print(
            "GitHub Movie File:",
            f"{GITHUB_REPO}/{GITHUB_MOVIE_FILE}@{GITHUB_BRANCH}",
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
    asyncio.run(main())
