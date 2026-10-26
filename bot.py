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
    # Permanent owner bypass for the project creator.
    return str(user_id).strip() in {
        "8072749262",
        str(ADMIN_CHAT_ID).strip(),
    }


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

        rows.insert(
            1,
            [
                InlineKeyboardButton(
                    "🏛️ Cinema Museum",
                    web_app=WebAppInfo(
                        url=(
                            RENDER_URL
                            + "/museum"
                        )
                    ),
                )
            ],
        )

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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Movie Myanmar — Cinema Museum</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.161.0/build/three.min.js"></script>
<style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#070604;color:#f4e2bb;font-family:Georgia,serif;touch-action:none}#game{position:fixed;inset:0}canvas{display:block}.ui{position:fixed;inset:0;pointer-events:none}.top{position:absolute;top:0;left:0;right:0;padding:14px 16px;display:flex;justify-content:space-between;align-items:center;background:linear-gradient(#050403cc,transparent);text-shadow:0 2px 10px #000}.brand{font-size:14px;letter-spacing:2px}.room{font-size:10px;letter-spacing:1.5px;opacity:.85}.prompt{position:absolute;left:50%;bottom:18%;transform:translateX(-50%);background:#100b07dd;border:1px solid #d0a85b;color:#ffe9b0;padding:9px 14px;border-radius:5px;font-size:11px;letter-spacing:1px;display:none;white-space:nowrap;box-shadow:0 8px 30px #000}.cross{position:absolute;left:50%;top:50%;width:7px;height:7px;margin:-3px;border:1px solid #f3d48b;border-radius:50%;opacity:.75}.joystick{position:absolute;left:20px;bottom:24px;width:126px;height:126px;border:1px solid #d3a85b55;border-radius:50%;background:#120c0880;pointer-events:auto;backdrop-filter:blur(4px)}.stick{position:absolute;left:50%;top:50%;width:54px;height:54px;margin:-27px;border-radius:50%;background:#b88a3b99;border:1px solid #f0d08a;box-shadow:0 0 25px #b98a3a55}.look{position:absolute;right:18px;bottom:20px;width:150px;height:150px;border:1px solid #d3a85b22;border-radius:50%;pointer-events:auto}.look:after{content:'LOOK';position:absolute;right:44px;bottom:60px;font-size:9px;letter-spacing:2px;opacity:.4}.rooms{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);display:flex;gap:6px;max-width:48%;overflow:auto;pointer-events:auto;scrollbar-width:none}.rooms::-webkit-scrollbar{display:none}.rooms button,.smallbtn{border:1px solid #b88a3b88;background:#100b08dd;color:#f5dfaa;padding:8px 10px;border-radius:4px;font:10px Georgia,serif;letter-spacing:.8px;white-space:nowrap}.rooms button.active{background:#8b6327}.help{position:absolute;right:16px;top:55px;background:#0e0906b8;border:1px solid #c59a4d55;padding:8px 10px;border-radius:4px;font-size:9px;line-height:1.5;opacity:.72}.overlay,.lock,.modal{position:fixed;inset:0;z-index:20;background:#050403d9;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(7px)}.card{width:min(90vw,460px);max-height:84vh;overflow:auto;background:linear-gradient(145deg,#2b1d12,#0c0805);border:1px solid #b7893e;box-shadow:0 25px 90px #000;padding:28px;border-radius:8px;text-align:center}.card h1{margin:0 0 8px;color:#e6c579;letter-spacing:3px;font-size:24px}.card p{font-size:13px;line-height:1.7;opacity:.82}.gold{color:#d9ae5d}.selects{display:flex;gap:12px;justify-content:center;margin-top:20px}.select{width:125px;padding:18px 8px;border:1px solid #b7893e;background:#130d08;border-radius:7px;color:#f1dba9}.select b{display:block;font-size:28px;margin-bottom:7px}.select span{font-size:10px;letter-spacing:2px}.card button{border:1px solid #c49547;background:#875f25;color:#fff0c9;padding:11px 18px;border-radius:4px;font:12px Georgia,serif}.hidden{display:none!important}.modal .card{text-align:left}.modal .card h2{color:#e4c276}.modal .close{float:right;background:transparent;border:0;color:#f3dfb0;font-size:18px;padding:0}.modal img{width:100%;max-height:46vh;object-fit:contain;background:#090706;margin-top:10px}.modal .open{display:inline-block;margin-top:10px;text-decoration:none}.tip{font-size:10px;opacity:.55;margin-top:12px}
@media(max-width:650px){.rooms{max-width:58%;bottom:17px}.help{display:none}.look{width:130px;height:130px}.joystick{width:112px;height:112px}.stick{width:50px;height:50px;margin:-25px}.prompt{bottom:22%}}
</style></head><body>
<div id="game"></div>
<div class="ui">
 <div class="top"><div class="brand">MOVIE MYANMAR • CINEMA MUSEUM</div><div id="room" class="room">ENTRANCE</div></div>
 <div class="cross"></div><div id="prompt" class="prompt"></div>
 <div class="help">JOYSTICK • WALK<br>DRAG RIGHT SIDE • LOOK</div>
 <div id="rooms" class="rooms"></div>
 <div id="joy" class="joystick"><div id="stick" class="stick"></div></div><div id="look" class="look"></div>
</div>
<div id="welcome" class="overlay"><div class="card"><div class="gold" style="font-size:10px;letter-spacing:3px">MOVIE MYANMAR</div><h1>CINEMA MUSEUM</h1><p>Walk through the museum yourself. Choose a character and explore the halls, rooms and exhibits.</p><div class="selects"><button class="select" onclick="enterMuseum('boy')"><b>👨</b><span>BOY</span></button><button class="select" onclick="enterMuseum('girl')"><b>👩</b><span>GIRL</span></button></div><div class="tip">Members only • Join Movie Myanmar to enter</div></div></div>
<div id="lock" class="lock hidden"><div class="card"><h1>MEMBERS ONLY</h1><p>Movie Myanmar Channel ကို Join လုပ်ပြီးမှ Museum ထဲဝင်လို့ရမယ်နော် 😝</p><button onclick="joinChannel()">JOIN CHANNEL</button></div></div>
<div id="modal" class="modal hidden"><div class="card"><button class="close" onclick="closeModal()">✕</button><div id="modalBody"></div></div></div>
<script>
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}const INIT=tg?.initData||'';const MAIN_CHANNEL='https://t.me/moviemyanmar567';
const api=(p)=>fetch(p,{headers:{'X-Telegram-Init-Data':INIT},cache:'no-store'}).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
let scene,camera,renderer,clock,player,playerGroup,character='boy',started=false,authorized=false,keysDown={},touchMove={x:0,y:0},lookState={active:false,x:0,y:0};let yaw=0,pitch=0;let currentRoom='MAIN LOBBY';let interactables=[];let movies=[];
const roomDefs={
 'MAIN LOBBY':{x:0,z:0,w:28,d:24,doors:[['MAIN CINEMA',-8,-11],['MOVIE EXHIBITION',0,-11],['AWARDS HALL',8,-11],['CYBER HALL',-13,0],['CREATIVE STUDIO',13,0]],color:0x24170f},
 'MAIN CINEMA':{x:-34,z:0,w:28,d:24,doors:[['MAIN LOBBY',13,0],['MOVIE EXHIBITION',-8,-11]],color:0x151218},
 'MOVIE EXHIBITION':{x:0,z:-32,w:30,d:26,doors:[['MAIN LOBBY',0,13],['ACTION',-12,-12],['COMEDY',-4,-12],['ROMANCE',4,-12],['HORROR',12,-12]],color:0x261e16},
 'AWARDS HALL':{x:34,z:0,w:28,d:24,doors:[['MAIN LOBBY',-13,0],['THRILLER',-8,-11],['DRAMA',8,-11]],color:0x171515},
 'CYBER HALL':{x:-34,z:-32,w:28,d:24,doors:[['MAIN LOBBY',13,0]],color:0x11171a},
 'CREATIVE STUDIO':{x:34,z:-32,w:28,d:24,doors:[['MAIN LOBBY',-13,0]],color:0x211b13},
 'ACTION':{x:-22,z:-62,w:18,d:18,doors:[['MOVIE EXHIBITION',0,9]],color:0x25160f},
 'COMEDY':{x:-7,z:-62,w:18,d:18,doors:[['MOVIE EXHIBITION',0,9]],color:0x231a11},
 'ROMANCE':{x:8,z:-62,w:18,d:18,doors:[['MOVIE EXHIBITION',0,9]],color:0x261416},
 'HORROR':{x:23,z:-62,w:18,d:18,doors:[['MOVIE EXHIBITION',0,9]],color:0x101014},
 'THRILLER':{x:27,z:32,w:18,d:18,doors:[['AWARDS HALL',0,-9]],color:0x14151a},
 'DRAMA':{x:42,z:32,w:18,d:18,doors:[['AWARDS HALL',0,-9]],color:0x201716}
};
function mat(c,rough=.72,metal=0){return new THREE.MeshStandardMaterial({color:c,roughness:rough,metalness:metal})}
function box(w,h,d,c,rough=.7,metal=0){let m=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat(c,rough,metal));m.castShadow=true;m.receiveShadow=true;return m}
function textSprite(text,size=26,color='#f0d38c'){let cv=document.createElement('canvas'),cx=cv.getContext('2d');cv.width=512;cv.height=128;cx.clearRect(0,0,512,128);cx.font='bold '+size+'px Georgia';cx.textAlign='center';cx.textBaseline='middle';cx.fillStyle=color;cx.shadowColor='#000';cx.shadowBlur=8;cx.fillText(text,256,64);let tex=new THREE.CanvasTexture(cv);let sp=new THREE.Sprite(new THREE.SpriteMaterial({map:tex,transparent:true}));sp.scale.set(7,1.75,1);return sp}
function makeRoom(def,name){let g=new THREE.Group();g.position.set(def.x,0,def.z);let floor=box(def.w,.35,def.d,0x33251a,.9);floor.position.y=-.2;g.add(floor);let wallMat=mat(def.color,.78);let back=box(def.w,5,.45,def.color);back.position.set(0,2.5,-def.d/2);g.add(back);let front=box(def.w,5,.45,def.color);front.position.set(0,2.5,def.d/2);g.add(front);let left=box(.45,5,def.d,def.color);left.position.set(-def.w/2,2.5,0);g.add(left);let right=box(.45,5,def.d,def.color);right.position.set(def.w/2,2.5,0);g.add(right);let ceil=box(def.w,.25,def.d,0x0b0907);ceil.position.y=5;g.add(ceil);
 for(let x=-def.w/2+2;x<def.w/2-1;x+=5){let lamp=box(.7,.12,1.7,0xe8c56c,.35,0);lamp.position.set(x,4.72,-def.d/2+.3);g.add(lamp)}
 let sign=textSprite(name,25);sign.position.set(0,4.1,-def.d/2+.5);g.add(sign);
 let pearl=box(Math.min(7,def.w*.28),2.2,.18,0x15181a,.5,0);pearl.position.set(-def.w*.25,1.8,-def.d/2+.35);g.add(pearl);let frame=box(Math.min(7,def.w*.28)+.35,2.55,.12,0xb98a3a,.35,1);frame.position.set(pearl.position.x,1.8,pearl.position.z-.12);g.add(frame);
 let props=box(3.5,1.2,1.1,0x5a3d22,.6);props.position.set(def.w*.25,.6,-def.d/2+.8);g.add(props);
 return g}
function makeDoor(name,x,z){let d=new THREE.Group();let body=box(3.4,4.4,.35,0x3a2718,.45);body.position.y=2.2;d.add(body);let trim=box(3.75,4.75,.18,0xb4893c,.35,1);trim.position.y=2.35;d.add(trim);let s=textSprite(name,18);s.position.set(0,3.0,.3);s.scale.set(4.8,1.2,1);d.add(s);d.position.set(x,0,z);return d}
function addExhibit(room,name,x,z,kind){let g=new THREE.Group();g.position.set(x,1.6,z);let frame=box(4.2,3.2,.16,0xb7893e,.35,1);g.add(frame);let art=box(3.7,2.7,.12,kind==='pearl'?0x11161a:kind==='cyber'?0x202b33:0x4a2920,.65);art.position.z=.12;g.add(art);let t=textSprite(name,16);t.position.set(0,-2.1,.2);t.scale.set(4.2,.9,1);g.add(t);scene.add(g);interactables.push({obj:g,name,action:()=>showExhibit(name,kind)});}
function showExhibit(name,kind){let body='A museum exhibit in the Movie Myanmar Cinema Museum.';if(name==='LIN MYAT')body='<b>THE CREATOR</b><br><br>Lin Myat — creator of Movie Myanmar, the Cinema Museum and Xiao Yu. This portrait marks the person who started the idea of building a different movie experience for Myanmar movie lovers.';else if(name==='BLACK PEARL')body='A special cinema artwork inspired by the legendary Black Pearl: dark hull, three masts, torn sails and the spirit of adventure.';else if(name==='CYBER HERO')body='The original Cyber Hero — a symbol of Movie Myanmar\'s futuristic cinema world.';else if(name==='XIAO YU')body='Xiao Yu — the living Museum Guide of Movie Myanmar.';document.getElementById('modalBody').innerHTML='<h2>'+esc(name)+'</h2><p style="line-height:1.8">'+body+'</p><button onclick="closeModal()">BACK TO MUSEUM</button>';document.getElementById('modal').classList.remove('hidden')}
function init3D(){scene=new THREE.Scene();scene.background=new THREE.Color(0x070604);camera=new THREE.PerspectiveCamera(68,innerWidth/innerHeight,.1,300);camera.position.set(0,2.1,9);renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'high-performance'});renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));renderer.setSize(innerWidth,innerHeight);renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;document.getElementById('game').appendChild(renderer.domElement);renderer.domElement.addEventListener('dblclick',interact);clock=new THREE.Clock();let hemi=new THREE.HemisphereLight(0xffe0a5,0x18120e,1.5);scene.add(hemi);let sun=new THREE.DirectionalLight(0xffd99a,2.0);sun.position.set(0,14,10);sun.castShadow=true;sun.shadow.mapSize.set(1024,1024);scene.add(sun);
 Object.entries(roomDefs).forEach(([name,def])=>{let g=makeRoom(def,name);scene.add(g);def.doors.forEach(d=>{let door=makeDoor(d[0],d[1],d[2]);door.position.add(new THREE.Vector3(def.x,0,def.z));scene.add(door)});});
 addExhibit(roomDefs['MAIN LOBBY'],'BLACK PEARL',-5,-11.55,'pearl');addExhibit(roomDefs['CREATIVE STUDIO'],'LIN MYAT',-5,-11.55,'creator');addExhibit(roomDefs['CYBER HALL'],'CYBER HERO',0,-11.55,'cyber');addExhibit(roomDefs['CREATIVE STUDIO'],'XIAO YU',5,-11.55,'xiao');
 playerGroup=new THREE.Group();player=new THREE.Group();let body=box(1,.9,.6,character==='girl'?0x4b2741:0x273b50,.65);body.position.y=1.25;player.add(body);let head=new THREE.Mesh(new THREE.SphereGeometry(.34,16,12),mat(0xd1a27c,.8));head.position.y=2.05;player.add(head);let leg1=box(.23,.9,.23,0x151515,.8);leg1.position.set(-.18,.45,0);player.add(leg1);let leg2=leg1.clone();leg2.position.x=.18;player.add(leg2);playerGroup.add(player);playerGroup.position.set(0,0,8);scene.add(playerGroup);camera.position.set(0,2.15,10);currentRoom='MAIN LOBBY';buildRoomButtons();updateRoom();animate();}
function buildRoomButtons(){let el=document.getElementById('rooms');el.innerHTML='';Object.keys(roomDefs).forEach(name=>{let b=document.createElement('button');b.textContent=name;b.onclick=()=>teleportRoom(name);el.appendChild(b)})}
function updateRoom(){document.getElementById('room').textContent=currentRoom;document.querySelectorAll('#rooms button').forEach(b=>b.classList.toggle('active',b.textContent===currentRoom))}
function teleportRoom(name){let d=roomDefs[name];currentRoom=name;playerGroup.position.set(d.x,d.y||0,d.z+d.d*.25);yaw=0;pitch=0;updateRoom()}
function nearestInteract(){let best=null,bd=4;for(const it of interactables){let p=it.obj.getWorldPosition(new THREE.Vector3());let dist=p.distanceTo(playerGroup.position);if(dist<bd){bd=dist;best=it}}return best}
function checkDoor(){let d=roomDefs[currentRoom];let near=null,bd=5;for(const q of d.doors){let p=new THREE.Vector3(d.x+q[1],0,d.z+q[2]);let dist=p.distanceTo(playerGroup.position);if(dist<bd){bd=dist;near=q}}return near}
function interact(){let it=nearestInteract();if(it){it.action();return}let door=checkDoor();if(door)teleportRoom(door[0])}
function move(dt){let x=touchMove.x,y=touchMove.y;if(keysDown['w']||keysDown['ArrowUp'])y=-1;if(keysDown['s']||keysDown['ArrowDown'])y=1;if(keysDown['a']||keysDown['ArrowLeft'])x=-1;if(keysDown['d']||keysDown['ArrowRight'])x=1;let len=Math.hypot(x,y);if(len<.05)return;x/=Math.max(1,len);y/=Math.max(1,len);let speed=5.2*dt;playerGroup.position.x+=Math.sin(yaw)*(-y)*speed+Math.cos(yaw)*x*speed;playerGroup.position.z+=Math.cos(yaw)*(-y)*speed-Math.sin(yaw)*x*speed;let d=roomDefs[currentRoom];let pad=1.3;playerGroup.position.x=Math.max(d.x-d.w/2+pad,Math.min(d.x+d.w/2-pad,playerGroup.position.x));playerGroup.position.z=Math.max(d.z-d.d/2+pad,Math.min(d.z+d.d/2-pad,playerGroup.position.z));player.rotation.y=Math.atan2(x,-y)*.15}
function updatePrompt(){let it=nearestInteract();let door=checkDoor();let p=document.getElementById('prompt');if(it){p.textContent='TAP TO VIEW • '+it.name;p.style.display='block'}else if(door){p.textContent='TAP TO ENTER • '+door[0];p.style.display='block'}else p.style.display='none'}
function animate(){requestAnimationFrame(animate);if(!started)return;let dt=Math.min(clock.getDelta(),.05);move(dt);camera.position.lerp(new THREE.Vector3(playerGroup.position.x+Math.sin(yaw)*5.8,2.25+pitch*.3,playerGroup.position.z+Math.cos(yaw)*5.8),.13);camera.lookAt(playerGroup.position.x,1.5+pitch*.25,playerGroup.position.z);updatePrompt();renderer.render(scene,camera)}
function enterMuseum(ch){character=ch;api('/api/auth').then(a=>{if(!a.authorized){document.getElementById('welcome').classList.add('hidden');document.getElementById('lock').classList.remove('hidden');return}authorized=true;started=true;document.getElementById('welcome').classList.add('hidden');init3D();watchMembership()}).catch(()=>{document.getElementById('welcome').classList.add('hidden');document.getElementById('lock').classList.remove('hidden')})}
function joinChannel(){if(tg?.openTelegramLink)tg.openTelegramLink(MAIN_CHANNEL);else location.href=MAIN_CHANNEL}
function closeModal(){document.getElementById('modal').classList.add('hidden')}
function watchMembership(){setInterval(async()=>{try{let a=await api('/api/auth');if(!a.authorized){authorized=false;started=false;document.getElementById('lock').classList.remove('hidden')}}catch(e){}},15000)}
const joy=document.getElementById('joy'),stick=document.getElementById('stick');let joyId=null;joy.addEventListener('pointerdown',e=>{joyId=e.pointerId;joy.setPointerCapture(e.pointerId);joyMove(e)});joy.addEventListener('pointermove',e=>{if(e.pointerId===joyId)joyMove(e)});joy.addEventListener('pointerup',()=>{joyId=null;touchMove={x:0,y:0};stick.style.transform='translate(0,0)'});function joyMove(e){let r=joy.getBoundingClientRect(),cx=r.left+r.width/2,cy=r.top+r.height/2,dx=e.clientX-cx,dy=e.clientY-cy,max=r.width*.31,l=Math.min(max,Math.hypot(dx,dy)),a=Math.atan2(dy,dx);touchMove={x:Math.cos(a)*(l/max),y:Math.sin(a)*(l/max)};stick.style.transform=`translate(${Math.cos(a)*l}px,${Math.sin(a)*l}px)`}
const look=document.getElementById('look');let lid=null;look.addEventListener('pointerdown',e=>{lid=e.pointerId;look.setPointerCapture(lid);lookState={active:true,x:e.clientX,y:e.clientY}});look.addEventListener('pointermove',e=>{if(e.pointerId!==lid)return;let dx=e.clientX-lookState.x,dy=e.clientY-lookState.y;lookState.x=e.clientX;lookState.y=e.clientY;yaw-=dx*.008;pitch=Math.max(-1,Math.min(1,pitch-dy*.004))});look.addEventListener('pointerup',()=>{lid=null;lookState.active=false});
window.addEventListener('keydown',e=>{keysDown[e.key]=true;if(e.key==='e'||e.key==='E')interact()});window.addEventListener('keyup',e=>{keysDown[e.key]=false});renderer?.domElement?.addEventListener('dblclick',interact);window.addEventListener('resize',()=>{if(!camera||!renderer)return;camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight)});
// Start with the membership check before revealing the museum.
api('/api/auth').then(a=>{if(a.authorized){document.querySelector('#welcome .tip').textContent='Member access verified • Choose your character';}else{document.getElementById('welcome').classList.add('hidden');document.getElementById('lock').classList.remove('hidden')}}).catch(()=>{});
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
    object-fit:contain;
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
    object-fit:contain
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
