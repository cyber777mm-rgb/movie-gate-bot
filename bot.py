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
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Movie Myanmar — Cinema Museum</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#050506;color:#f7ead0;font-family:Georgia,"Times New Roman",serif}button{font:inherit;color:inherit}.hide{display:none!important}
#app{position:fixed;inset:0;overflow:hidden;background:#080706}
.scene{position:absolute;inset:0;transition:transform .8s cubic-bezier(.2,.8,.2,1),opacity .6s}.scene.walk{transform:scale(1.16);opacity:0}
.grain{position:absolute;inset:0;pointer-events:none;opacity:.09;background-image:radial-gradient(#fff 1px,transparent 1px);background-size:5px 5px;mix-blend-mode:screen}
.dust{position:absolute;inset:0;pointer-events:none;background-image:radial-gradient(circle,rgba(255,230,180,.7) 0 1px,transparent 2px);background-size:73px 91px;opacity:.16;animation:dust 18s linear infinite}@keyframes dust{to{background-position:300px -260px}}
.top{position:absolute;z-index:30;top:0;left:0;right:0;padding:calc(12px + env(safe-area-inset-top)) 16px 8px;display:flex;justify-content:space-between;align-items:center;background:linear-gradient(#050506cc,transparent)}.brand{font-weight:900;letter-spacing:2px;font-size:12px}.place{font-size:10px;color:#c6b28c;letter-spacing:1px}
.gate{position:absolute;z-index:50;inset:0;display:flex;align-items:flex-end;justify-content:center;padding:30px 20px 45px;background:linear-gradient(180deg,#05050522,#050505ee 68%,#050505)}.gatebox{width:min(440px,100%);text-align:center}.gatebox h1{font-size:34px;margin:0 0 5px;letter-spacing:4px}.gatebox p{margin:0 0 20px;color:#c8b99f;font-size:12px}.enter{border:1px solid #d8b878;background:linear-gradient(180deg,#a98445,#5b3d18);padding:14px 28px;border-radius:3px;font-weight:900;letter-spacing:2px;box-shadow:0 0 30px #d2a54a33}.enter:active{transform:scale(.97)}
.entrance{background:radial-gradient(ellipse at 50% 35%,#6e4a24 0,#21170d 32%,#070707 72%)}.building{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;perspective:900px}.doorframe{width:74%;height:62%;border:7px solid #80602d;box-shadow:0 0 0 2px #e0c078 inset,0 40px 100px #000;background:linear-gradient(180deg,#15100c,#020202);transform:perspective(700px) rotateX(3deg)}.door{position:absolute;left:50%;top:19%;width:39%;height:52%;transform:translateX(-50%);border:2px solid #b58a45;background:radial-gradient(circle at 50% 35%,#342718,#050505 70%);box-shadow:inset 0 0 40px #000,0 0 30px #c29a4d22}.door:before{content:"";position:absolute;inset:8%;border:1px solid #6e552e}.lamp{position:absolute;width:7px;height:7px;border-radius:50%;background:#ffe9a0;box-shadow:0 0 22px #ffd66d;animation:blink 3s infinite}.lamp.l1{top:25%;left:31%}.lamp.l2{top:25%;right:31%}@keyframes blink{50%{opacity:.45;box-shadow:0 0 8px #ffd66d}}
.hall{background:radial-gradient(ellipse at 50% 22%,#3b2a18 0,#16110d 40%,#050505 78%);perspective:1000px}.room{position:absolute;inset:10% 0 0;transform-style:preserve-3d}.back{position:absolute;left:18%;right:18%;top:13%;bottom:16%;background:linear-gradient(180deg,#20170f,#0c0907);border:1px solid #654c29;box-shadow:0 0 80px #000}.wall{position:absolute;top:7%;bottom:10%;width:45%;background:linear-gradient(180deg,#21180f,#0a0807);border:1px solid #5c4527}.wall.left{left:-8%;transform-origin:right;transform:rotateY(25deg)}.wall.right{right:-8%;transform-origin:left;transform:rotateY(-25deg)}.floor{position:absolute;left:7%;right:7%;bottom:0;height:35%;background:repeating-linear-gradient(90deg,#24190e 0 14%,#120d08 14% 15%);transform:perspective(500px) rotateX(58deg);transform-origin:bottom;box-shadow:0 -30px 50px #000 inset}.ceiling{position:absolute;left:7%;right:7%;top:0;height:22%;background:linear-gradient(#17110b,#0b0806);transform:perspective(500px) rotateX(-55deg);transform-origin:top}.neon{position:absolute;top:12%;left:50%;width:2px;height:65%;background:#b99148;box-shadow:0 0 20px #d6b56d;opacity:.35}
.hero{position:absolute;z-index:8;left:50%;top:24%;transform:translateX(-50%);width:90px;height:160px;filter:drop-shadow(0 0 20px #57dfff66);animation:heroGlow 3s infinite}.hero .head{position:absolute;left:29px;top:0;width:34px;height:40px;border-radius:45%;background:linear-gradient(145deg,#d8dbe2,#444956);border:1px solid #aeb5c5}.hero .eyes{position:absolute;left:34px;top:18px;width:24px;height:4px;background:#62efff;box-shadow:0 0 12px #62efff}.hero .body{position:absolute;left:8px;top:40px;width:74px;height:92px;background:linear-gradient(145deg,#252a35,#07090c);clip-path:polygon(25% 0,75% 0,100% 100%,0 100%);border:1px solid #7e8795}.hero .core{position:absolute;left:38px;top:65px;width:15px;height:15px;border-radius:50%;background:#63eaff;box-shadow:0 0 25px #63eaff}.hero .cape{position:absolute;left:5px;top:45px;width:80px;height:105px;background:linear-gradient(160deg,#171017,#3b1029);clip-path:polygon(18% 0,82% 0,100% 100%,50% 84%,0 100%);z-index:-1}@keyframes heroGlow{50%{filter:drop-shadow(0 0 34px #57dfffaa);transform:translateX(-50%) translateY(-3px)}}
.sign{position:absolute;z-index:12;left:50%;top:11%;transform:translateX(-50%);padding:7px 15px;border:1px solid #7c6030;background:#0b0907dd;letter-spacing:2px;font-size:11px;color:#e8d19b;white-space:nowrap}.doorway{position:absolute;z-index:10;top:26%;width:100px;height:135px;border:3px solid #76582e;background:#050505;box-shadow:inset 0 0 25px #000}.doorway.d1{left:8%}.doorway.d2{right:8%}.doorway span{position:absolute;bottom:-25px;left:50%;transform:translateX(-50%);font-size:9px;white-space:nowrap;color:#c9b993}.controls{position:absolute;z-index:35;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));display:flex;justify-content:space-between;gap:8px}.ctrl{flex:1;border:1px solid #6f552d;background:#090806dd;padding:11px 8px;border-radius:3px;font-size:10px;letter-spacing:.5px}.ctrl:active{background:#241b10}
.roomScene{background:radial-gradient(ellipse at 50% 15%,#4a341d 0,#19120c 38%,#050505 80%)}.roomScene .back{left:5%;right:5%;top:10%;bottom:20%;}.roomScene .wall.left{left:-14%;width:55%;}.roomScene .wall.right{right:-14%;width:55%}.roomScene .floor{left:0;right:0;height:38%}.roomScene .ceiling{left:0;right:0}.creatorRoom .back{left:6%;right:6%;top:10%;bottom:18%;background:radial-gradient(ellipse at 50% 45%,#3a2819 0,#18100b 58%,#090705 100%);}.creatorRoom .wall.left{left:-4%;width:34%;transform:rotateY(18deg)}.creatorRoom .wall.right{right:-4%;width:34%;transform:rotateY(-18deg)}.creatorRoom .floor{left:2%;right:2%;height:38%;background:linear-gradient(180deg,#2b1c10,#120b07 80%);}.creatorRoom .creatorStage{left:50%;top:27%;width:88%;max-width:620px;height:360px;transform:translateX(-50%);}.creatorRoom .chair{left:43%;bottom:12px;width:190px;height:190px;border-radius:82px 82px 24px 24px;background:linear-gradient(145deg,#875a38,#2b160f);}.creatorRoom .lin{left:43%;bottom:66px;width:132px;height:195px;}.creatorRoom .linFace{left:28px;width:76px;height:88px;border-radius:46%;}.creatorRoom .suit{left:5px;top:82px;width:122px;height:105px;}.creatorRoom .shirt{left:51px;top:91px;width:26px;height:82px;}.creatorRoom .tie{left:60px;top:94px;width:8px;height:68px;}.creatorRoom .xiaoGuide{right:7%;bottom:28px;width:150px;}.creatorRoom .xiaoGuide img{width:118px;height:170px;border-radius:58px 58px 18px 18px;object-fit:cover;object-position:center top;}.creatorRoom .label{font-size:9px;padding:5px 4px}.creatorRoom .roomTitle{top:13%;}.creatorRoom .roomTitle h2{font-size:24px}.creatorRoom .roomTitle p{font-size:11px} @media(max-width:390px){.creatorRoom .creatorStage{width:94%;}.creatorRoom .chair{left:42%;width:165px;height:170px}.creatorRoom .lin{left:42%;transform:translateX(-50%) scale(.9);transform-origin:bottom}.creatorRoom .xiaoGuide{right:1%;transform:scale(.86);transform-origin:bottom right}}.roomTitle{position:absolute;z-index:20;top:13%;left:50%;transform:translateX(-50%);text-align:center}.roomTitle h2{margin:0;font-size:23px;letter-spacing:2px}.roomTitle p{margin:5px 0;color:#bcae92;font-size:10px}
.frames{position:absolute;z-index:9;inset:20% 3% 24%;display:flex;justify-content:space-around;align-items:flex-start;gap:10px;perspective:900px}.frame{width:24%;max-width:105px;background:#0c0906;border:7px solid #9a7438;padding:3px;box-shadow:0 12px 28px #000;transform:rotateY(-8deg);transition:.25s;position:relative}.frame:nth-child(even){transform:rotateY(8deg) translateY(20px)}.frame:after{content:"";position:absolute;inset:-2px;box-shadow:0 0 15px #e4c36a55}.frame img{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;background:#17110b}.plaque{font-family:Arial,sans-serif;font-size:8px;text-align:center;color:#e8d19b;padding:5px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.frame:active{transform:translateY(-5px) scale(1.04)}
.wingDoors{position:absolute;z-index:12;inset:23% 4% 25%;display:grid;grid-template-columns:repeat(2,1fr);gap:13px}.wingDoor{position:relative;border:3px solid #795b31;background:linear-gradient(145deg,#17100a,#040403);box-shadow:inset 0 0 30px #000,0 15px 35px #000;display:flex;align-items:flex-end;justify-content:center;padding:12px 6px}.wingDoor:before{content:"";position:absolute;inset:8px;border:1px solid #5d4525}.wingDoor b{font-size:12px;letter-spacing:1px;text-shadow:0 2px 5px #000}.wingDoor small{position:absolute;bottom:1px;font-size:8px;color:#bbaa8b}
.creatorStage{position:absolute;z-index:12;left:50%;top:25%;transform:translateX(-50%);width:320px;max-width:82%;height:260px}.chair{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);width:155px;height:150px;border-radius:70px 70px 18px 18px;background:linear-gradient(145deg,#714c31,#24130e);border:2px solid #9d7540;box-shadow:0 20px 35px #000}.lin{position:absolute;left:50%;bottom:56px;transform:translateX(-50%);width:110px;height:165px;animation:breathe 4s ease-in-out infinite}.linFace{position:absolute;left:23px;top:0;width:64px;height:76px;border-radius:48%;background-size:cover;background-position:center 22%;border:2px solid #d7b878;box-shadow:0 8px 20px #000;overflow:hidden}.linFace:after{content:"";position:absolute;left:18px;right:18px;top:28px;height:2px;background:#ffffff22;animation:blinkFace 5s infinite}@keyframes blinkFace{0%,94%,100%{opacity:0}95%{opacity:1}}.suit{position:absolute;left:4px;top:72px;width:102px;height:92px;background:linear-gradient(145deg,#191a1f,#050506);clip-path:polygon(25% 0,75% 0,100% 100%,0 100%);border:1px solid #70644e}.shirt{position:absolute;left:43px;top:80px;width:23px;height:75px;background:#eee8dc;clip-path:polygon(50% 0,100% 100%,0 100%)}.tie{position:absolute;left:51px;top:82px;width:7px;height:62px;background:#161018}.xiaoGuide{position:absolute;right:1%;bottom:18px;width:84px;text-align:center;animation:breathe 3.5s ease-in-out infinite}.xiaoGuide img{width:72px;height:94px;object-fit:cover;object-position:center top;border-radius:38px 38px 12px 12px;border:2px solid #d7b878;box-shadow:0 12px 25px #000}.label{font-family:Arial,sans-serif;font-size:8px;color:#f0d9a2;background:#120d09;border:1px solid #70562e;padding:4px 2px;margin-top:4px}@keyframes breathe{50%{transform:translateX(-50%) translateY(-3px)}}
.modal{position:absolute;z-index:60;inset:0;background:#000b;display:none;align-items:flex-end;justify-content:center;padding:16px}.panel{width:min(480px,100%);max-height:82vh;overflow:auto;background:linear-gradient(180deg,#241a10,#0c0907);border:1px solid #8c6b35;box-shadow:0 25px 70px #000;padding:18px;border-radius:4px}.panel img.mainPoster{display:block;width:min(190px,55%);margin:0 auto 14px;aspect-ratio:2/3;object-fit:cover;border:5px solid #a17b3d}.panel h3{margin:0 0 6px;font-size:22px}.panel p{color:#c8baa2;font-size:12px;line-height:1.6}.close{float:right;border:0;background:none;font-size:22px}.openMovie{display:block;text-align:center;text-decoration:none;border:1px solid #d4b36b;background:#513719;padding:12px;margin-top:14px;font-size:11px;letter-spacing:1px}.guideBox{border-left:2px solid #d5b36a;padding-left:12px;color:#d7c9ae;font-size:12px;line-height:1.65}
.toast{position:absolute;z-index:80;left:50%;bottom:95px;transform:translateX(-50%);background:#090806ee;border:1px solid #7f6335;padding:9px 13px;font:11px Arial,sans-serif;white-space:nowrap;opacity:0;transition:.3s}.toast.show{opacity:1}

/* FINAL MUSEUM ARCHITECTURE OVERRIDE */
.hall,.roomScene{perspective:none;background:radial-gradient(ellipse at 50% 18%,#4b321b 0,#1b1109 38%,#050403 82%)}
.room{position:absolute;inset:0;transform-style:preserve-3d;overflow:hidden}
.back{left:11%!important;right:11%!important;top:16%!important;bottom:17%!important;background:linear-gradient(180deg,#2a1b0f,#0b0704)!important;border:2px solid #76552b!important;box-shadow:inset 0 0 70px #000,0 20px 55px #000!important;z-index:2}
.wall{top:13%!important;bottom:16%!important;width:38%!important;background:linear-gradient(180deg,#2b1c10,#0d0805)!important;border:2px solid #72542d!important;z-index:4;box-shadow:inset 0 0 50px #000!important}
.wall.left{left:-1%!important;transform:none!important;clip-path:polygon(0 0,100% 8%,100% 92%,0 100%)}
.wall.right{right:-1%!important;transform:none!important;clip-path:polygon(0 8%,100% 0,100% 100%,0 92%)}
.floor{left:-8%!important;right:-8%!important;bottom:-5%!important;height:34%!important;z-index:6!important}
.ceiling{left:-8%!important;right:-8%!important;top:-4%!important;height:22%!important;z-index:7!important}
.roomTitle{z-index:25!important;top:11%!important}.roomTitle h2{font-size:21px!important;letter-spacing:3px!important}.roomTitle p{font-size:9px!important;letter-spacing:1px}
.sign{top:11%!important;z-index:26!important}
.doorway{z-index:22!important;top:30%!important;width:96px!important;height:145px!important;background:linear-gradient(145deg,#28190d,#050403)!important;box-shadow:inset 0 0 35px #000,0 20px 35px #000!important}
.doorway.d1{left:7%!important}.doorway.d2{right:7%!important}
.hero{z-index:23!important;top:29%!important;width:100px!important;height:175px!important}
.frames{z-index:22!important;inset:25% 14% 23%!important;display:grid!important;grid-template-columns:repeat(3,1fr)!important;align-items:center!important;gap:14px!important;perspective:900px!important}
.frame{width:auto!important;max-width:none!important;border-width:6px!important;background:#0a0704!important;box-shadow:0 15px 35px #000,0 0 0 1px #cda65b66!important}
.frame img{aspect-ratio:2/3!important}.plaque{font-size:7px!important;background:#0a0704dd}
.wingDoors{z-index:22!important;inset:29% 10% 24%!important;grid-template-columns:repeat(2,1fr)!important;gap:22px!important}
.wingDoor{background:linear-gradient(145deg,#2a1a0d,#050403)!important;box-shadow:inset 0 0 40px #000,0 20px 40px #000!important;border-width:4px!important}
.controls{z-index:40!important}
.creatorRoom .creatorStage{z-index:22!important;left:50%!important;top:27%!important;width:90%!important;max-width:none!important;height:370px!important}
.creatorRoom .chair{left:42%!important;width:175px!important;height:180px!important}.creatorRoom .lin{left:42%!important}.creatorRoom .xiaoGuide{right:3%!important}
@media(max-width:390px){.back{left:12%!important;right:12%!important}.wall{width:36%!important}.frames{inset:25% 11% 24%!important;gap:9px!important}.frame{border-width:5px!important}.wingDoors{inset:30% 7% 24%!important;gap:13px!important}.wingDoor b{font-size:10px!important}.creatorRoom .creatorStage{width:96%!important}.creatorRoom .chair{left:39%!important;width:155px!important}.creatorRoom .lin{left:39%!important}.creatorRoom .xiaoGuide{right:0!important}}
</style>
</head>
<body>
<div id="app">
  <div class="scene entrance" id="entrance">
    <div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">CINEMA MUSEUM</div></div>
    <div class="building"><div class="doorframe"></div><div class="door"></div><i class="lamp l1"></i><i class="lamp l2"></i></div><div class="dust"></div><div class="grain"></div>
    <div class="gate"><div class="gatebox"><h1>CINEMA MUSEUM</h1><p>ရုပ်ရှင်တွေကို ကြည့်ရုံမဟုတ်ဘဲ — အခန်းတွေထဲ ဝင်လည်ကြည့်မယ်။</p><button class="enter" onclick="enterMuseum()">ENTER MUSEUM</button></div></div>
  </div>
  <div class="scene hall hide" id="lobby">
    <div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">MAIN LOBBY</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="neon"></div><div class="sign">THE CINEMA COLLECTION</div>
      <div class="hero"><div class="head"></div><div class="eyes"></div><div class="cape"></div><div class="body"></div><div class="core"></div></div>
      <div class="doorway d1" onclick="openRoom('wings')"><span>MOVIE EXHIBITION</span></div><div class="doorway d2" onclick="openRoom('creator')"><span>CREATIVE STUDIO</span></div><div class="doorway d1" style="left:27%;top:62%;transform:scale(.78)" onclick="openRoom('awardsRoom')"><span>AWARDS HALL</span></div><div class="doorway d2" style="right:27%;top:62%;transform:scale(.78)" onclick="openRoom('cyberRoom')"><span>CYBER HALL</span></div>
    </div><div class="dust"></div><div class="grain"></div>
    <div class="controls"><button class="ctrl" onclick="openRoom('wings')">🎬 MOVIE EXHIBITION</button><button class="ctrl" onclick="guide()">👩🏻 Xiao Yu</button><button class="ctrl" onclick="openRoom('creator')">🪑 CREATIVE STUDIO</button></div>
  </div>
  <div class="scene hall hide" id="wings">
    <div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">MOVIE EXHIBITION</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="sign">MOVIE WINGS</div><div class="wingDoors">
      <button class="wingDoor" onclick="openGenre('Action')"><b>⚔ ACTION</b><small>တိုက်ခိုက်မှု</small></button><button class="wingDoor" onclick="openGenre('Comedy')"><b>😂 COMEDY</b><small>ဟာသ</small></button><button class="wingDoor" onclick="openGenre('Romance')"><b>❤️ ROMANCE</b><small>အချစ်</small></button><button class="wingDoor" onclick="openGenre('Horror')"><b>👻 HORROR</b><small>ကြောက်စရာ</small></button><button class="wingDoor" onclick="openGenre('Thriller')"><b>🔪 THRILLER</b><small>သည်းထိတ်ရင်ဖို</small></button><button class="wingDoor" onclick="openGenre('Drama')"><b>🎭 DRAMA</b><small>ဒရာမာ</small></button><button class="wingDoor" onclick="openGenre('Fantasy')"><b>✨ FANTASY</b><small>စိတ်ကူးယဉ်</small></button><button class="wingDoor" onclick="openGenre('All')"><b>🎞 ALL MOVIES</b><small>ကားအားလုံး</small></button>
    </div></div><div class="dust"></div><div class="grain"></div><div class="controls"><button class="ctrl" onclick="back('lobby')">← MAIN LOBBY</button><button class="ctrl" onclick="openRoom('creator')">CREATIVE STUDIO →</button><button class="ctrl" onclick="guide()">Xiao Yu</button></div>
  </div>
  <div class="scene roomScene hide" id="genreRoom"><div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place" id="genrePlace">MOVIE ROOM</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="roomTitle"><h2 id="genreTitle">ACTION</h2><p>FILMS MOUNTED ON THE WALLS</p></div><div class="frames" id="frames"></div></div><div class="dust"></div><div class="grain"></div><div class="controls"><button class="ctrl" onclick="back('wings')">← EXHIBITION</button><button class="ctrl" onclick="back('lobby')">MAIN LOBBY</button><button class="ctrl" onclick="guide()">👩🏻 Guide</button></div></div>
  <div class="scene roomScene creatorRoom hide" id="creator"><div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">CREATIVE STUDIO</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="roomTitle"><h2>CREATIVE STUDIO</h2><p>ကိုကို လင်းမြတ် • Xiao Yu</p></div><div class="creatorStage"><div class="chair"></div><div class="lin"><div class="linFace"></div><div class="suit"></div><div class="shirt"></div><div class="tie"></div></div><div class="xiaoGuide"><img src="https://raw.githubusercontent.com/cyber777mm-rgb/movie-gate-bot/main/ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"><div class="label">XIAO YU • GUIDE</div></div></div></div><div class="dust"></div><div class="grain"></div><div class="controls"><button class="ctrl" onclick="back('lobby')">← MAIN LOBBY</button><button class="ctrl" onclick="guide()">Xiao Yu ပြောမယ်</button><button class="ctrl" onclick="openRoom('wings')">MOVIE EXHIBITION</button></div></div>

  <div class="scene hall hide" id="awardsRoom"><div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">AWARDS HALL</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="roomTitle"><h2>AWARDS HALL</h2><p>CINEMA MOMENTS • HONORS • COLLECTION</p></div><div class="frames" id="awardFrames"><div class="frame"><div style="aspect-ratio:1/1;background:radial-gradient(circle,#d7b45e,#4d3518 45%,#090705 70%);display:grid;place-items:center;font-size:34px">★</div><div class="plaque">CINEMA COLLECTION</div></div><div class="frame"><div style="aspect-ratio:1/1;background:linear-gradient(145deg,#e2c67b,#4a3216);display:grid;place-items:center;font-size:32px">🏆</div><div class="plaque">HONOR WALL</div></div><div class="frame"><div style="aspect-ratio:1/1;background:linear-gradient(145deg,#321b0f,#0b0704);display:grid;place-items:center;font-size:28px">🎞</div><div class="plaque">FILM HISTORY</div></div></div></div><div class="dust"></div><div class="controls"><button class="ctrl" onclick="back('lobby')">← MAIN LOBBY</button><button class="ctrl" onclick="openRoom('creator')">CREATIVE STUDIO</button><button class="ctrl" onclick="guide()">XIAO YU GUIDE</button></div></div>
  <div class="scene hall hide" id="cyberRoom"><div class="top"><div class="brand">MOVIE MYANMAR</div><div class="place">CYBER HALL</div></div><div class="room"><div class="back"></div><div class="wall left"></div><div class="wall right"></div><div class="floor"></div><div class="ceiling"></div><div class="roomTitle"><h2>CYBER HALL</h2><p>THE ORIGINAL MUSEUM GUARDIAN</p></div><div class="hero"><div class="head"></div><div class="eyes"></div><div class="cape"></div><div class="body"></div><div class="core"></div></div><div class="sign">CYBER • ORIGINAL CHARACTER</div></div><div class="dust"></div><div class="controls"><button class="ctrl" onclick="back('lobby')">← MAIN LOBBY</button><button class="ctrl" onclick="openRoom('wings')">MOVIE EXHIBITION</button><button class="ctrl" onclick="guide()">XIAO YU GUIDE</button></div></div>
  <div class="modal" id="modal" onclick="if(event.target===this)closeModal()"><div class="panel" id="panel"></div></div><div class="toast" id="toast"></div>
</div>
<script>
const tg=window.Telegram?.WebApp; if(tg){tg.ready();tg.expand();}
const INIT=tg?.initData||'';
const linFace='data:image/jpeg;base64,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';
document.querySelector('.linFace').style.backgroundImage=`url("${linFace}")`;
function $(id){return document.getElementById(id)}
function apiUrl(path){return path+(path.includes('?')?'&':'?')+'initData='+encodeURIComponent(INIT)}
async function api(path){const h={};if(INIT)h['X-Telegram-Init-Data']=INIT;const r=await fetch(apiUrl(path),{cache:'no-store',headers:h});if(!r.ok)throw new Error(r.status);return r.json()}
function show(id){document.querySelectorAll('.scene').forEach(x=>x.classList.add('hide'));$(id).classList.remove('hide');window.scrollTo(0,0)}
function enterMuseum(){ $('entrance').classList.add('walk');setTimeout(()=>show('lobby'),550);}
function openRoom(id){document.querySelector('.scene:not(.hide)')?.classList.add('walk');setTimeout(()=>{show(id);document.querySelectorAll('.scene').forEach(x=>x.classList.remove('walk'))},520)}
function back(id){show(id)}
async function openGenre(name){show('genreRoom');$('genreTitle').textContent=name.toUpperCase();$('genrePlace').textContent=name+' ROOM';const box=$('frames');box.innerHTML='<div style="color:#bcae92;font:12px Arial;text-align:center;width:100%;padding-top:120px">နရံပေါ်က exhibits တွေကို တင်နေပါတယ်…</div>';try{const d=await api('/api/genre?name='+encodeURIComponent(name));const movies=(d.movies||[]).slice(0,12);box.innerHTML=movies.length?movies.map(m=>{const src=m.poster?apiUrl('/api/poster?id='+encodeURIComponent(m.id)):'';return `<button class="frame" onclick="movie('${encodeURIComponent(m.id)}')">${src?`<img src="${src}" loading="lazy">`:'<div style="aspect-ratio:2/3;background:#17110b"></div>'}<div class="plaque">${esc(m.title)}</div></button>`}).join(''):'<div style="color:#bcae92;font:12px Arial;text-align:center;width:100%;padding-top:120px">ဒီအခန်းမှာ ကားမရှိသေးဘူး</div>'}catch(e){toast('Museum data ကို ဖွင့်မရသေးဘူး');}}
async function movie(id){try{const d=await api('/api/movie?id='+decodeURIComponent(id));const p=d.poster?apiUrl('/api/poster?id='+encodeURIComponent(d.id)):'';$('panel').innerHTML=`<button class="close" onclick="closeModal()">×</button>${p?`<img class="mainPoster" src="${p}">`:''}<h3>${esc(d.title||'Movie')}</h3><p>${esc([d.year,d.quality,d.audio].filter(Boolean).join(' • '))}</p><p>${esc(d.description||'Movie Myanmar Cinema Museum exhibit')}</p>${d.link?`<a class="openMovie" href="${esc(d.link)}" target="_blank" rel="noopener">🎬 OPEN MOVIE</a>`:''}`;$('modal').style.display='flex'}catch(e){toast('Movie ကို မဖွင့်နိုင်သေးဘူး')}}
function guide(){$('panel').innerHTML='<button class="close" onclick="closeModal()">×</button><h3>👩🏻 Xiao Yu</h3><div class="guideBox">ဟေး 😝 Xiao Yu ပါ။ ဒီ Museum မှာ အခန်းတစ်ခန်းချင်းစီ ဝင်လည်လို့ရတယ်။ MOVIE EXHIBITIONထဲက WALL-MOUNTED POSTER ကိုနှိပ်ရင် ကားအကြောင်းကြည့်ပြီး <b>OPEN MOVIE</b> နဲ့ ဆက်သွားလို့ရတယ်နော်။</div>';$('modal').style.display='flex'}
function closeModal(){$('modal').style.display='none'}function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}function toast(t){$('toast').textContent=t;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),1800)}
if(!INIT){toast('Telegram ထဲကနေ Museum ကိုဖွင့်ပါ')}
</script>
</body></html>'''

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
