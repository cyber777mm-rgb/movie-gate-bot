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

MUSEUM_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Movie Myanmar — Cinema Museum</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#090706;color:#f2dfb5;font-family:Georgia,serif;touch-action:manipulation}button{font:inherit;color:inherit}.app{position:fixed;inset:0;background:#090706;overflow:hidden}.room{position:absolute;inset:0;overflow:hidden;background:#120d09;transition:opacity .35s,transform .6s}.room.enter{animation:roomEnter .65s ease both}@keyframes roomEnter{from{opacity:.35;transform:scale(1.06)}to{opacity:1;transform:scale(1)}}
.arch{position:absolute;inset:0;perspective:900px;overflow:hidden;background:radial-gradient(circle at 50% 22%,rgba(255,213,130,.12),transparent 34%),#110b08}.ceiling{position:absolute;left:5%;right:5%;top:-10%;height:42%;background:linear-gradient(180deg,#24160f,#3a2415 55%,#1b100b);transform:perspective(600px) rotateX(-18deg);transform-origin:top;box-shadow:inset 0 -35px 70px #000}.backwall{position:absolute;left:7%;right:7%;top:10%;height:64%;background:linear-gradient(90deg,#25150d,#4a2d1a 18%,#321d12 50%,#4a2d1a 82%,#25150d);border:1px solid #8e642f;box-shadow:inset 0 0 70px #0009,0 18px 50px #000}.floor{position:absolute;left:-10%;right:-10%;bottom:-26%;height:52%;background:repeating-linear-gradient(90deg,#1a100c 0 42px,#24160f 42px 84px);transform:perspective(600px) rotateX(58deg);transform-origin:bottom;box-shadow:inset 0 50px 90px #000}.floor:after{content:"";position:absolute;inset:0;background:radial-gradient(ellipse at 50% 0,rgba(221,173,76,.18),transparent 55%)}
.light{position:absolute;width:130px;height:130px;border-radius:50%;background:radial-gradient(circle,rgba(255,220,139,.7),rgba(255,187,61,.18) 25%,transparent 68%);filter:blur(3px);animation:pulse 4s ease-in-out infinite}.light.a{top:8%;left:15%}.light.b{top:7%;right:15%;animation-delay:1.2s}.light.c{top:28%;left:45%;width:180px;height:180px;opacity:.45}@keyframes pulse{50%{opacity:.65;transform:scale(1.08)}}
.goldLine{position:absolute;left:10%;right:10%;top:13%;height:2px;background:linear-gradient(90deg,transparent,#d4a64e,transparent);box-shadow:0 0 18px #d4a64e}.plaque{position:absolute;left:50%;top:17%;transform:translateX(-50%);padding:10px 22px;border:1px solid #c69a4b;background:#120b07dd;box-shadow:0 8px 30px #000;color:#e9c879;letter-spacing:2.5px;font-size:12px;text-align:center;white-space:nowrap}.sub{display:block;font-size:8px;letter-spacing:1.5px;color:#bfa77b;margin-top:5px}
.columns{position:absolute;top:8%;bottom:25%;width:38px;background:linear-gradient(90deg,#160c08,#a47432 45%,#2a180d 75%,#0d0805);border:1px solid #b7863b;box-shadow:0 0 20px #000}.columns.l{left:8%}.columns.r{right:8%}.columns:after{content:"";position:absolute;left:-7px;right:-7px;top:0;height:18px;background:#b98a3d;box-shadow:0 8px 16px #000}
.exhibit{position:absolute;top:29%;left:50%;transform:translateX(-50%);width:min(72vw,430px);height:36%;background:linear-gradient(145deg,#21140d,#0c0805);border:6px solid #a8782f;box-shadow:0 20px 50px #000,0 0 0 2px #e0bc70 inset;display:flex;align-items:center;justify-content:center;text-align:center;overflow:hidden}.exhibit:before{content:"";position:absolute;inset:14px;border:1px solid #c79a4d55;background:radial-gradient(circle at 50% 30%,rgba(231,195,116,.22),transparent 42%)}.exhibitText{position:relative;z-index:2;text-shadow:0 3px 12px #000}.exhibitText strong{display:block;font-size:22px;letter-spacing:3px;color:#f1d48c}.exhibitText span{display:block;margin-top:9px;font-size:10px;letter-spacing:2px;color:#c9b18a}.exhibitText em{display:block;margin-top:18px;font-size:11px;color:#aee5ff;opacity:.8}
.sideEx{position:absolute;top:33%;width:18%;max-width:120px;height:27%;border:3px solid #9f7534;background:linear-gradient(160deg,#2b1a10,#0d0906);box-shadow:0 15px 35px #000}.sideEx.l{left:17%}.sideEx.r{right:17%}.sideEx:after{content:"";position:absolute;inset:9px;border:1px solid #d2a85d66}
.trophy{position:absolute;bottom:18%;left:50%;transform:translateX(-50%);width:62px;height:72px;border:2px solid #d4a64e;border-radius:18px 18px 7px 7px;background:linear-gradient(90deg,#6e4b1e,#e1bf68,#6a461a);box-shadow:0 0 22px #d6aa4d55}.trophy:before,.trophy:after{content:"";position:absolute;top:10px;width:25px;height:38px;border:2px solid #c99b48}.trophy:before{left:-24px;border-right:0;border-radius:18px 0 0 18px}.trophy:after{right:-24px;border-left:0;border-radius:0 18px 18px 0}.base{position:absolute;bottom:12%;left:50%;transform:translateX(-50%);width:92px;height:12px;background:#b18338;box-shadow:0 0 16px #cda14e55}
.cinemaScreen{position:absolute;left:16%;right:16%;top:25%;height:34%;background:#090b0c;border:7px solid #9f7433;box-shadow:0 0 60px rgba(125,197,255,.13),0 25px 45px #000}.cinemaScreen:after{content:"MOVIE MYANMAR";position:absolute;inset:0;display:grid;place-items:center;color:#d6b66d;font-size:18px;letter-spacing:4px;text-shadow:0 0 18px #d6b66d66}.curtain{position:absolute;top:20%;bottom:31%;width:18%;background:repeating-linear-gradient(90deg,#25080d,#5c1722 10px,#26090e 20px);box-shadow:inset 0 0 30px #000}.curtain.l{left:4%}.curtain.r{right:4%}.projector{position:absolute;left:50%;top:8%;width:14px;height:14px;background:#fff0bd;border-radius:50%;box-shadow:0 0 25px 10px #fff0bd66}.beam{position:absolute;left:50%;top:12%;width:48%;height:55%;transform:translateX(-50%);background:linear-gradient(180deg,rgba(255,242,197,.16),rgba(255,242,197,0));clip-path:polygon(45% 0,55% 0,100% 100%,0 100%);animation:beam 5s ease-in-out infinite}@keyframes beam{50%{opacity:.55;transform:translateX(-50%) rotate(2deg)}}
.posterGrid{position:absolute;left:9%;right:9%;top:25%;bottom:28%;display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.posterFrame{background:linear-gradient(135deg,#d1a451,#654319);padding:4px;box-shadow:0 10px 25px #000}.posterFrame img{width:100%;height:100%;object-fit:cover;display:block;background:#17100b}.posterFrame button{width:100%;height:100%;padding:0;border:0;background:none;display:block}.posterEmpty{grid-column:1/-1;display:grid;place-items:center;border:1px solid #a87b35;background:#110b07cc;color:#d8c095}
.creatorPortrait{position:absolute;left:50%;top:25%;transform:translateX(-50%);width:min(52vw,260px);height:42%;background:linear-gradient(145deg,#d9b35d,#6d481c 13%,#19100a 15%,#0c0805 85%,#6d481c 87%,#d9b35d);border:6px solid #bc8d3b;box-shadow:0 22px 50px #000,0 0 30px #d7aa4d22}.creatorPortrait:before{content:"";position:absolute;inset:18px;background:radial-gradient(circle at 50% 28%,#8d6c4b 0 10%,transparent 11%),radial-gradient(ellipse at 50% 73%,#15100c 0 29%,transparent 30%),linear-gradient(180deg,#392619,#0f0a06 72%);border:1px solid #c99b4b55}.creatorName{position:absolute;bottom:18px;left:0;right:0;text-align:center;z-index:2}.creatorName strong{display:block;letter-spacing:4px;color:#f0d38b;font-size:18px}.creatorName span{display:block;font-size:8px;letter-spacing:2px;color:#d4c19b;margin-top:5px}
.cyber{position:absolute;left:50%;top:25%;transform:translateX(-50%);width:min(48vw,230px);height:42%;background:linear-gradient(145deg,#111820,#070a0e);border:2px solid #56d8ff;box-shadow:0 0 25px #42d9ff55,0 22px 50px #000}.cyber:before{content:"CYBER HERO";position:absolute;left:0;right:0;bottom:17%;text-align:center;color:#72e7ff;letter-spacing:3px;font-size:13px;text-shadow:0 0 12px #56d8ff}.cyber:after{content:"";position:absolute;left:10%;right:10%;top:12%;height:50%;border:1px solid #8e5bff;background:linear-gradient(145deg,#172433,#090d15);clip-path:polygon(50% 0,85% 20%,100% 75%,70% 100%,30% 100%,0 75%,15% 20%);box-shadow:0 0 30px #7d55ff66;animation:cyberGlow 3.5s ease-in-out infinite}@keyframes cyberGlow{50%{filter:brightness(1.3);box-shadow:0 0 50px #56d8ff88}}
.artWall{position:absolute;left:10%;right:10%;top:25%;bottom:27%;display:flex;gap:14px;justify-content:center;align-items:center}.artPiece{width:27%;height:70%;border:5px solid #b98735;background:linear-gradient(135deg,#332116,#0b0805);box-shadow:0 18px 35px #000;position:relative}.artPiece:after{content:"CINEMA";position:absolute;inset:10px;border:1px solid #d4aa5d55;display:grid;place-items:end center;padding-bottom:10px;font-size:8px;letter-spacing:2px;color:#d8bd82}
.props{position:absolute;left:12%;right:12%;top:28%;bottom:28%;display:flex;align-items:flex-end;justify-content:space-around}.prop{width:20%;height:42%;border:2px solid #bb8c3c;background:linear-gradient(145deg,#3b2819,#0d0906);box-shadow:0 14px 30px #000;position:relative}.prop:before{content:"";position:absolute;left:20%;right:20%;top:18%;height:24%;border:2px solid #d0a34f;border-radius:50%}.prop span{position:absolute;bottom:10px;left:0;right:0;text-align:center;font-size:7px;letter-spacing:1px;color:#d9c090}
.xiao{position:absolute;right:10%;bottom:27%;width:72px;height:150px;animation:guide 3s ease-in-out infinite;filter:drop-shadow(0 12px 15px #000)}.xiao .head{position:absolute;left:21px;top:0;width:31px;height:38px;border-radius:50%;background:linear-gradient(145deg,#f0c7a8,#8d5b46)}.xiao .hair{position:absolute;left:14px;top:-5px;width:45px;height:42px;border-radius:50% 50% 35% 35%;background:#17100d}.xiao .body{position:absolute;left:10px;top:35px;width:52px;height:82px;border-radius:17px 17px 9px 9px;background:linear-gradient(145deg,#15151c,#561827);border:1px solid #c19a52}.xiao .arm{position:absolute;top:50px;width:13px;height:58px;border-radius:9px;background:#17131a;border:1px solid #b98a42}.xiao .arm.l{left:2px;transform:rotate(12deg)}.xiao .arm.r{right:2px;transform:rotate(-18deg)}.xiao .tag{position:absolute;top:124px;left:-5px;right:-5px;text-align:center;font-size:8px;letter-spacing:1px;color:#f0d28b;background:#0b0806cc;padding:5px;border:1px solid #a77a35}@keyframes guide{50%{transform:translateY(-5px)}}
.bottom{position:absolute;left:0;right:0;bottom:0;z-index:20;padding:9px 10px calc(10px + env(safe-area-inset-bottom));background:linear-gradient(transparent,#070504 30%);display:flex;gap:7px;overflow-x:auto}.bottom::-webkit-scrollbar{display:none}.roomBtn{flex:0 0 auto;padding:9px 12px;border:1px solid #87612e;background:#100b08e8;border-radius:5px;font-size:9px;letter-spacing:1px}.roomBtn.active{background:#9b6c27;border-color:#e1bc69;color:#fff2cf}.top{position:absolute;left:0;right:0;top:0;z-index:20;padding:14px 15px;background:linear-gradient(#070504d9,transparent);display:flex;justify-content:space-between;align-items:flex-start}.top h1{font-size:15px;letter-spacing:2px;margin:0;color:#f1d38a}.top p{font-size:8px;letter-spacing:1.3px;margin:5px 0 0;color:#bda77c}.roomTitle{text-align:right;font-size:11px;letter-spacing:1.5px}.roomTitle span{display:block;font-size:8px;color:#a99678;margin-top:4px}.action{position:absolute;left:50%;transform:translateX(-50%);bottom:82px;z-index:21;padding:11px 18px;border:1px solid #d0a24e;background:#120b07e8;color:#f1d38a;letter-spacing:1.2px;font-size:10px;border-radius:4px;box-shadow:0 0 22px #d0a24e33}.modal,.gate{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;background:#050403d9;backdrop-filter:blur(10px)}.card{width:min(88vw,450px);max-height:82vh;overflow:auto;padding:25px;background:linear-gradient(145deg,#2a1b10,#0d0906);border:1px solid #b2833a;box-shadow:0 25px 80px #000;border-radius:8px}.card h2{color:#e8c979;letter-spacing:2px;margin-top:0}.card p{font-size:13px;line-height:1.8;color:#d5c4a4}.card img{width:100%;max-height:52vh;object-fit:contain;background:#080604}.card button,.card a{display:inline-block;margin-top:10px;padding:10px 15px;border:1px solid #c08f40;background:#8d6227;color:#fff1cb;text-decoration:none;border-radius:4px}.close{float:right;margin:0!important;background:transparent!important;border:0!important;font-size:20px!important}.hidden{display:none!important}.gate .card{text-align:center}.gate h1{letter-spacing:3px;color:#e7c77c}.join{width:100%;margin-top:15px!important}
@media(max-width:650px){.columns{width:25px}.columns.l{left:5%}.columns.r{right:5%}.sideEx.l{left:7%}.sideEx.r{right:7%}.posterGrid{grid-template-columns:repeat(2,1fr);left:7%;right:7%;gap:8px}.posterFrame:nth-child(n+5){display:none}.plaque{font-size:10px}.xiao{right:5%}.action{bottom:84px}}
</style></head>
<body>
<div class="app"><div id="room" class="room"></div><div class="top"><div><h1>MOVIE MYANMAR</h1><p>CINEMA MUSEUM • A LIVING COLLECTION</p></div><div class="roomTitle"><b id="roomTitle">MAIN LOBBY</b><span id="roomSub">WELCOME TO THE MUSEUM</span></div></div><div id="bottom" class="bottom"></div><button id="action" class="action hidden"></button></div>
<div id="loading" class="gate"><div class="card"><h1>CINEMA MUSEUM</h1><p>Preparing the museum…</p></div></div>
<div id="lock" class="gate hidden"><div class="card"><h1>MEMBERS ONLY</h1><p id="lockmsg">Movie Myanmar Channel ကို Join လုပ်ပြီးမှ Museum ထဲဝင်လို့ရမယ်နော် 😝</p><button id="join" class="join">JOIN CHANNEL</button></div></div>
<div id="modal" class="modal hidden"><div id="panel" class="card"></div></div>
<script>
const tg=window.Telegram?.WebApp||null;const MAIN_CHANNEL='https://t.me/moviemyanmar567';if(tg){try{tg.ready();tg.expand()}catch(e){}}
const ROOMS={
 lobby:{name:'MAIN LOBBY',sub:'ENTRANCE • BLACK PEARL • CINEMA HISTORY',kind:'lobby'},
 cinema:{name:'MAIN CINEMA',sub:'SCREEN • CURTAIN • PROJECTOR',kind:'cinema'},
 exhibition:{name:'MOVIE EXHIBITION',sub:'MOVIE MYANMAR COLLECTION',kind:'exhibition'},
 action:{name:'ACTION',sub:'ADRENALINE • HEROES • MOMENTS',kind:'art'},
 comedy:{name:'COMEDY',sub:'LAUGHTER • CHARACTERS • CLASSICS',kind:'art'},
 romance:{name:'ROMANCE',sub:'LOVE • STORIES • MEMORIES',kind:'art'},
 horror:{name:'HORROR',sub:'SHADOWS • FEAR • NIGHT CINEMA',kind:'art'},
 thriller:{name:'THRILLER',sub:'SUSPENSE • MYSTERY • TENSION',kind:'art'},
 drama:{name:'DRAMA',sub:'STORIES • EMOTION • HUMANITY',kind:'art'},
 awards:{name:'AWARDS HALL',sub:'TROPHIES • MILESTONES • ACHIEVEMENTS',kind:'awards'},
 studio:{name:'CREATIVE STUDIO',sub:'LIN MYAT • XIAO YU • CREATION',kind:'studio'},
 cyber:{name:'CYBER HALL',sub:'CYBER HERO • FUTURE CINEMA',kind:'cyber'},
 xiaoyu:{name:'XIAO YU GUIDE',sub:'YOUR CINEMA MUSEUM GUIDE',kind:'guide'},
 art:{name:'ART GALLERY',sub:'POSTERS • CONCEPT ART • CINEMA ART',kind:'art'},
 history:{name:'FILM HISTORY',sub:'CAMERAS • REELS • PROJECTORS',kind:'history'}
};
const ORDER=['lobby','cinema','exhibition','action','comedy','romance','horror','thriller','drama','awards','studio','cyber','xiaoyu','art','history'];let current='lobby',movies=[];
const $=id=>document.getElementById(id);function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function initData(){return String(window.Telegram?.WebApp?.initData||tg?.initData||'').trim()}
async function api(path){const init=initData();const headers={'Cache-Control':'no-cache'};if(init)headers['X-Telegram-Init-Data']=init;const r=await fetch(path,{headers,cache:'no-store'});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw d;return d}
function roomShell(extra=''){return `<div class="arch"><div class="ceiling"></div><div class="backwall"></div><div class="floor"></div><div class="light a"></div><div class="light b"></div><div class="light c"></div><div class="goldLine"></div><div class="columns l"></div><div class="columns r"></div>${extra}</div>`}
function renderRoom(k){current=k;const r=ROOMS[k];let h='';
 if(k==='lobby')h=roomShell(`<div class="plaque">MAIN LOBBY<span class="sub">WELCOME TO MOVIE MYANMAR CINEMA MUSEUM</span></div><div class="exhibit"><div class="exhibitText"><strong>BLACK PEARL</strong><span>THE CINEMA COLLECTION</span><em>✦ SPECIAL EXHIBIT ✦</em></div></div><div class="sideEx l"></div><div class="sideEx r"></div><button class="action" onclick="openInfo('MAIN LOBBY','Welcome to the Cinema Museum. Explore the cinema, exhibition, creator room, awards and cyber halls.')">ENTER EXHIBIT</button>`);
 else if(k==='cinema')h=roomShell(`<div class="curtain l"></div><div class="curtain r"></div><div class="cinemaScreen"></div><div class="projector"></div><div class="beam"></div><div class="plaque">MAIN CINEMA<span class="sub">THE HEART OF MOVIE MYANMAR</span></div><button class="action" onclick="openInfo('MAIN CINEMA','A classic cinema room with a large screen, velvet curtains and a moving projector beam.')">VIEW CINEMA</button>`);
 else if(k==='exhibition')h=roomShell(`<div class="plaque">MOVIE EXHIBITION<span class="sub">THE MOVIE MYANMAR COLLECTION</span></div><div id="posterGrid" class="posterGrid"><div class="posterEmpty">Loading collection…</div></div>`);
 else if(k==='awards')h=roomShell(`<div class="plaque">AWARDS HALL<span class="sub">ACHIEVEMENTS • MILESTONES • FUTURE</span></div><div class="exhibit"><div class="trophy"></div><div class="base"></div><div class="exhibitText"><strong>THE AWARDS</strong><span>HONOURING THE JOURNEY</span></div></div><button class="action" onclick="openInfo('AWARDS HALL','A ceremonial hall dedicated to Movie Myanmar milestones, achievements and future dreams.')">VIEW COLLECTION</button>`);
 else if(k==='studio')h=roomShell(`<div class="plaque">CREATIVE STUDIO<span class="sub">WHERE THE IDEA BECAME REAL</span></div><button class="creatorPortrait" onclick="showCreator()"><div class="creatorName"><strong>LIN MYAT</strong><span>FOUNDER • CREATOR</span></div></button><div class="xiao"><div class="head"></div><div class="hair"></div><div class="body"></div><div class="arm l"></div><div class="arm r"></div><div class="tag">XIAO YU</div></div><button class="action" onclick="openInfo('XIAO YU GUIDE','Xiao Yu is the living museum guide. She welcomes visitors and explains the story behind each room.')">TALK TO XIAO YU</button>`);
 else if(k==='cyber')h=roomShell(`<div class="plaque">CYBER HALL<span class="sub">FUTURE CINEMA • CYBER HERO</span></div><div class="cyber"></div><button class="action" onclick="openInfo('CYBER HERO','An original Movie Myanmar cyber hero exhibit — built as the museum guardian, not a copy of any existing superhero.')">VIEW CYBER HERO</button>`);
 else if(k==='xiaoyu')h=roomShell(`<div class="plaque">XIAO YU GUIDE<span class="sub">YOUR PERSONAL CINEMA MUSEUM GUIDE</span></div><div class="xiao" style="right:50%;transform:translateX(50%) scale(1.35)"><div class="head"></div><div class="hair"></div><div class="body"></div><div class="arm l"></div><div class="arm r"></div><div class="tag">XIAO YU • 小雨</div></div><button class="action" onclick="openInfo('XIAO YU','Hello! I am Xiao Yu CyB. I will guide you through Movie Myanmar Cinema Museum. Start with the Main Lobby and explore any room you like.')">TALK TO XIAO YU</button>`);
 else if(k==='history')h=roomShell(`<div class="plaque">FILM HISTORY<span class="sub">FROM FILM REEL TO DIGITAL CINEMA</span></div><div class="props"><div class="prop"><span>CAMERA</span></div><div class="prop"><span>FILM REEL</span></div><div class="prop"><span>PROJECTOR</span></div><div class="prop"><span>CLAPPER</span></div></div><button class="action" onclick="openInfo('FILM HISTORY','Vintage cameras, film reels, projector technology and cinema props presented as museum artifacts.')">VIEW HISTORY</button>`);
 else if(k==='art')h=roomShell(`<div class="plaque">${esc(r.name)}<span class="sub">${esc(r.sub)}</span></div><div class="artWall"><div class="artPiece"></div><div class="artPiece"></div><div class="artPiece"></div></div><button class="action" onclick="openInfo('${esc(r.name)}','A dedicated cinema art room with framed pieces, spotlighting and warm museum lighting.')">VIEW ART</button>`);
 $('room').classList.remove('enter');void $('room').offsetWidth;$('room').classList.add('enter');$('room').innerHTML=h;$('roomTitle').textContent=r.name;$('roomSub').textContent=r.sub;renderButtons();if(k==='exhibition')loadMovies();}
function renderButtons(){$('bottom').innerHTML=ORDER.map(k=>`<button class="roomBtn ${k===current?'active':''}" onclick="go('${k}')">${ROOMS[k].name}</button>`).join('')}
function go(k){if(!ROOMS[k])return;renderRoom(k)}
function openInfo(title,text){$('panel').innerHTML=`<button class="close" onclick="closeModal()">×</button><h2>${esc(title)}</h2><p>${esc(text)}</p><button onclick="closeModal()">BACK TO MUSEUM</button>`;$('modal').classList.remove('hidden')}
function showCreator(){$('panel').innerHTML=`<button class="close" onclick="closeModal()">×</button><div style="text-align:center"><div style="font-size:10px;letter-spacing:3px;color:#b98a3a">THE CREATOR</div><h2>LIN MYAT</h2><div style="font-size:10px;letter-spacing:1px;color:#cbb58c">FOUNDER • MOVIE MYANMAR • CINEMA MUSEUM • XIAO YU</div></div><p>Movie Myanmar, the Cinema Museum and Xiao Yu began from one idea: create a different movie experience for Myanmar movie lovers.</p><p>ဒီ Museum ရဲ့ concept နဲ့ Movie Myanmar / Xiao Yu ကို စတင်ဖန်တီးခဲ့သူက <b>ကိုကို လင်းမြတ်</b> ဖြစ်ပါတယ်။</p><button onclick="closeModal()">BACK TO MUSEUM</button>`;$('modal').classList.remove('hidden')}
function closeModal(){$('modal').classList.add('hidden')}
async function loadMovies(){const wall=$('posterGrid');if(!wall)return;try{const d=await api('/api/search?q=');movies=Array.isArray(d.movies)?d.movies:[];if(!movies.length){wall.innerHTML='<div class="posterEmpty">No movies in the collection yet.</div>';return}wall.innerHTML=movies.slice(0,8).map(m=>`<div class="posterFrame"><button onclick="openMovie('${encodeURIComponent(m.id)}')"><img src="/api/poster?id=${encodeURIComponent(m.id)}" alt=""></button></div>`).join('')}catch(e){wall.innerHTML='<div class="posterEmpty">Collection is temporarily unavailable.</div>'}}
async function openMovie(id){try{const m=await api('/api/movie?id='+decodeURIComponent(id));$('panel').innerHTML=`<button class="close" onclick="closeModal()">×</button><h2>${esc(m.title||'Movie Exhibit')}</h2>${m.poster?`<img src="/api/poster?id=${encodeURIComponent(m.id)}">`:''}<p>${esc(m.description||'Movie Myanmar collection exhibit.')}</p>${m.link?`<a href="${esc(m.link)}" target="_blank">OPEN MOVIE</a>`:''}`;$('modal').classList.remove('hidden')}catch(e){openInfo('MOVIE EXHIBIT','Unable to open this exhibit right now.')}}
let accessTimer=null;function lockMuseum(msg){clearInterval(accessTimer);$('loading').classList.add('hidden');$('modal').classList.add('hidden');$('lockmsg').textContent=msg||'Movie Myanmar Channel ကို Join လုပ်ပြီးမှ Museum ထဲဝင်လို့ရမယ်နော် 😝';$('lock').classList.remove('hidden');$('join').onclick=()=>tg?.openTelegramLink?tg.openTelegramLink(MAIN_CHANNEL):location.href=MAIN_CHANNEL}
async function waitAuth(){for(let i=0;i<16;i++){try{const init=initData();if(init){const a=await api('/api/auth');if(a.authorized)return a;if(a.reason==='not_member')return a}}catch(e){}await new Promise(r=>setTimeout(r,350))}throw {message:'Telegram ထဲကနေ Museum ကို ပြန်ဖွင့်ပြီး ထပ်စမ်းပါ။'}}
function watchMembership(){clearInterval(accessTimer);accessTimer=setInterval(async()=>{try{const a=await api('/api/auth');if(!a.authorized)lockMuseum(a.message)}catch(e){}},15000)}
async function start(){try{const a=await waitAuth();if(!a.authorized){lockMuseum(a.message);return}$('loading').classList.add('hidden');renderRoom('lobby');watchMembership()}catch(e){lockMuseum(e.message)}}start();
</script></body></html>'''


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
