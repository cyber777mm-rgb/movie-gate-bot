# Movie Myanmar - single-file bot + embedded Mini App
import os, asyncio, hashlib, json, threading, re, traceback, io, html
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from google import genai
from PIL import Image, ImageOps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

try:
    from telegram import WebAppInfo, MenuButtonWebApp
except ImportError:
    WebAppInfo = MenuButtonWebApp = None

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MAIN_CHANNEL = "@moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"
RENDER_URL = "https://movie-gate-bot.onrender.com"
WEBHOOK_PATH = "/telegram-webhook"
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8072749262").strip()
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
MOVIE_DB_FILE = "movies.json"
AI_USAGE_FILE = "ai_usage.json"
FREE_AI_LIMIT = 20
GEMINI_MODEL = "gemini-3.8-flash"
WELCOME_IMAGE = "https://raw.githubusercontent.com/cyber777mm-rgb/movie-gate-bot/main/ChatGPT%20Image%20Oct%2018%2C%202026%2C%2007_47_33%20PM.png"

movie_database = []
ai_usage = {}
user_search_mode, user_ai_mode, user_last_results = {}, {}, {}
webhook_application = None
webhook_loop = None
poster_cache = {}

GENRE_KEYWORDS = {
    "Action": ["action", "အက်ရှင်"], "Comedy": ["comedy", "ဟာသ"],
    "Romance": ["romance", "romantic", "အချစ်"], "Horror": ["horror", "ကြောက်", "သရဲ"],
    "Thriller": ["thriller"], "Drama": ["drama", "ဒရာမာ"], "Fantasy": ["fantasy"]
}
CHANNEL_REQUEST_WORDS = ["channel link", "channel", "ချန်နယ်", "ချန်နယ်လင့်", "movie channel", "channel ပေး", "channel ပး"]
MOVIE_INTENT_WORDS = ["movie", "film", "ကား", "ဇာတ်ကား", "ရုပ်ရှင်", "ရှာပေး", "ရှာပေးပါ", "ကားပေး"]

CAROUSEL_LIMIT, CAROUSEL_VISIBLE = 5, 3
carousel_message_ids, carousel_gif_cache = {}, {"signature": None, "bytes": None}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            x = json.load(f)
        return x
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save error:", e)


def load_movies():
    global movie_database
    x = load_json(MOVIE_DB_FILE, [])
    movie_database = x if isinstance(x, list) else []


def save_movies(): save_json(MOVIE_DB_FILE, movie_database)

def load_ai_usage():
    global ai_usage
    x = load_json(AI_USAGE_FILE, {})
    ai_usage = x if isinstance(x, dict) else {}


def save_ai_usage(): save_json(AI_USAGE_FILE, ai_usage)

def get_ai_usage(uid): return int(ai_usage.get(str(uid), 0))

def increase_ai_usage(uid):
    ai_usage[str(uid)] = get_ai_usage(uid) + 1
    save_ai_usage()

load_movies(); load_ai_usage()

gemini_client = None
if GEMINI_API_KEY:
    try: gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception: pass


def normalize_text(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w\u1000-\u109f]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def extract_year(s):
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s or "")
    return m.group(1) if m else ""


def detect_genres(s):
    n = normalize_text(s)
    return [g for g, words in GENRE_KEYWORDS.items() if any(normalize_text(w) in n for w in words)]


def parse_movie_post(message):
    text = message.caption or message.text or ""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    title = lines[0] if lines else "Untitled Movie"
    year = extract_year(text)
    q = re.search(r"\b(2160p|4k|1080p|720p|480p)\b", text, re.I)
    a = re.search(r"(Burmese|Myanmar|English|Dual|Multi)[ -]?Audio", text, re.I)
    poster = message.photo[-1].file_id if message.photo else ""
    return {
        "title": title, "year": year, "quality": q.group(1).upper() if q else "",
        "audio": a.group(0) if a else "", "original_name": title,
        "genres": detect_genres(text), "description": text, "post_link": "",
        "chat_id": str(message.chat_id), "message_id": message.message_id,
        "poster_file_id": poster,
    }


def build_movie_link(movie):
    chat, mid = str(movie.get("chat_id", "")), movie.get("message_id")
    if chat.startswith("-100") and mid:
        return f"https://t.me/c/{chat[4:]}/{mid}"
    return movie.get("post_link") or MOVIE_CHANNEL_URL


def movie_search(q):
    qn = normalize_text(q)
    if not qn: return []
    y = extract_year(q)
    scored = []
    for m in movie_database:
        hay = normalize_text(" ".join([m.get("title", ""), m.get("original_name", ""), m.get("year", ""), " ".join(m.get("genres", []))]))
        score = 0
        if qn == normalize_text(m.get("title", "")): score += 100
        if qn in hay: score += 60
        if y and y == m.get("year"): score += 40
        score += int(SequenceMatcher(None, qn, normalize_text(m.get("title", ""))).ratio() * 30)
        if score >= 25: scored.append((score, m))
    return [m for _, m in sorted(scored, key=lambda x: x[0], reverse=True)]


def latest(limit=10): return list(reversed(movie_database[-limit:]))

def invalidate_carousel(): carousel_gif_cache.update(signature=None, bytes=None)

def movie_identity(m): return f"{m.get('chat_id')}:{m.get('message_id')}"


def join_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Join Movie Myanmar", url=MOVIE_CHANNEL_URL)], [InlineKeyboardButton("✅ I Joined", callback_data="check_join")]])


def home_keyboard():
    rows = [[InlineKeyboardButton("🔎 Search Movie", callback_data="search"), InlineKeyboardButton("🎭 Categories", callback_data="categories")],
            [InlineKeyboardButton("🔥 Latest Movies", callback_data="latest"), InlineKeyboardButton("🤖 Xiao Yu AI", callback_data="ai")],
            [InlineKeyboardButton("⏭️ Next Movie", callback_data="next_poster"), InlineKeyboardButton("📺 Movie Channel", url=MOVIE_CHANNEL_URL)],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")]]
    if WebAppInfo:
        rows.insert(0, [InlineKeyboardButton("✨ Open Movie App", web_app=WebAppInfo(url=RENDER_URL + "/app"))])
    return InlineKeyboardMarkup(rows)


def category_keyboard():
    gs = ["Action", "Comedy", "Romance", "Horror", "Thriller", "Drama", "Fantasy"]
    rows = [[InlineKeyboardButton("🎬 All Movies", callback_data="genre:All")]]
    for i in range(0, len(gs), 2): rows.append([InlineKeyboardButton(gs[i], callback_data="genre:"+gs[i]), *([InlineKeyboardButton(gs[i+1], callback_data="genre:"+gs[i+1])] if i+1 < len(gs) else [])])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def home_caption():
    return "🎬 <b>Movie Myanmar</b>\n\nဟေး 😝 Xiao Yu CyB ပါ။\nကားရှာမယ်၊ နောက်ဆုံးကားတွေကြည့်မယ်၊ AI နဲ့လည်း စကားပြောလို့ရတယ်။\n\n👇 အောက်က Menu ကနေရွေးနော်။"


async def is_member(bot, uid):
    try:
        s = await bot.get_chat_member(MAIN_CHANNEL, uid)
        return s.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False


async def stop_carousel(chat_id):
    carousel_message_ids.pop(chat_id, None)


async def show_home(update, context):
    q = update.callback_query
    if q: await q.answer()
    target = q.message if q else update.effective_message
    try:
        await target.reply_photo(WELCOME_IMAGE, caption=home_caption(), parse_mode="HTML", reply_markup=home_keyboard())
    except Exception:
        await target.reply_text(home_caption(), parse_mode="HTML", reply_markup=home_keyboard())


async def start_command(update, context):
    uid = update.effective_user.id
    if not await is_member(context.bot, uid):
        await update.effective_message.reply_text("🎬 Movie Myanmar ထဲဝင်ပြီးမှ သုံးလို့ရမယ်နော် 😝", reply_markup=join_keyboard())
        return
    user_search_mode.pop(uid, None); user_ai_mode.pop(uid, None)
    await show_home(update, context)


async def check_join(update, context):
    q = update.callback_query; await q.answer()
    if await is_member(context.bot, q.from_user.id):
        await q.edit_message_text("✅ ဝင်ပြီးပြီဟေ့ 😝 ခဏ... Movie Myanmar ဖွင့်ပေးမယ်။")
        await asyncio.sleep(1); await show_home(update, context)
    else:
        await q.answer("အရင် Join လုပ်ပါဦး 😒", show_alert=True)


async def start_search(update, context):
    q = update.callback_query
    if q: await q.answer(); msg = q.message
    else: msg = update.effective_message
    user_search_mode[update.effective_user.id] = True
    await msg.reply_text("🔎 Movie နာမည်ရေးပေးနော်။\nဥပမာ — Avatar 2009")


async def show_movie_list(message, movies, title="🎬 Movies"):
    if not movies:
        await message.reply_text("😒 မတွေ့သေးဘူးဟ။ Movie နာမည်နည်းနည်းတိတိကျကျ ရေးကြည့်။")
        return
    buttons = []
    for m in movies[:15]:
        label = m.get("title", "Movie")[:48]
        if m.get("year"): label += f" ({m['year']})"
        buttons.append([InlineKeyboardButton("🎬 " + label, url=build_movie_link(m))])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await message.reply_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def show_latest_movies(update, context):
    q = update.callback_query
    if q: await q.answer(); msg = q.message
    else: msg = update.effective_message
    await show_movie_list(msg, latest(15), "🔥 <b>Latest Movies</b>")


async def show_categories(update, context):
    q = update.callback_query; await q.answer()
    await q.message.reply_text("🎭 Genre ရွေးနော်", reply_markup=category_keyboard())


async def show_genre(update, context, genre):
    q = update.callback_query; await q.answer()
    if genre == "All": movies = latest(15)
    else: movies = [m for m in reversed(movie_database) if genre in m.get("genres", [])][:15]
    await show_movie_list(q.message, movies, f"🎭 <b>{genre}</b>")


async def show_help(update, context):
    q = update.callback_query; await q.answer()
    await q.message.reply_text("🔎 Search Movie — ကားနာမည်ရှာ\n🔥 Latest — နောက်ဆုံးတင်ထားတဲ့ကား\n🎭 Categories — Genre နဲ့ရွေး\n🤖 Xiao Yu — AI Chat (Free 20 requests)\n✨ Movie App — Neon Mini App")


async def start_ai(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; used = get_ai_usage(uid)
    if used >= FREE_AI_LIMIT:
        await q.message.reply_text("ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\nငါ့ကို Movie မေး 😝🎬\nကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။")
        return
    user_ai_mode[uid] = True
    await q.message.reply_text(f"🤖 Xiao Yu CyB ပါ 😝\nဘာမေးမလဲ?\n\nFree AI ကျန်တာ — {FREE_AI_LIMIT-used} ခါ")


async def ask_gemini(text):
    if not gemini_client: return None
    prompt = ("You are Xiao Yu CyB, a playful cute Burmese female movie assistant. "
              "Reply in natural Burmese, concise and friendly. Help with movies and general chat. "
              "Do not claim you can access unavailable information. User: " + text)
    try:
        r = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return getattr(r, "text", None)
    except Exception as e:
        print("Gemini error:", e); return None


async def send_admin_request(context, update, query):
    u = update.effective_user
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, f"🔔 <b>Movie Request</b>\n\n🎬 Requested: <b>{html.escape(query)}</b>\n👤 User: @{html.escape(u.username or 'NoUsername')}\n🆔 ID: <code>{u.id}</code>", parse_mode="HTML")
    except Exception as e: print("admin request error:", e)


def has_movie_intent(t):
    n = normalize_text(t)
    return any(normalize_text(w) in n for w in MOVIE_INTENT_WORDS)


def is_channel_request(t):
    n = normalize_text(t)
    return any(normalize_text(w) in n for w in CHANNEL_REQUEST_WORDS)


async def handle_special_reply(msg, text):
    n = normalize_text(text)
    if any(x in n for x in ["မင်္ဂလာပါ", "ဟယ်လို", "hello", "hi"]):
        await msg.reply_text("ဟေး 😝 Xiao Yu CyB ပါ။ Movie ရှာမလား?")
        return True
    if "နေကောင်း" in n:
        await msg.reply_text("ကောင်းတယ်ဟ 😝 Movie တွေစောင့်ပေးနေတာ။ မင်းကော?")
        return True
    return False


async def message_handler(update, context):
    msg = update.effective_message; uid = update.effective_user.id; text = msg.text or ""
    if not await is_member(context.bot, uid):
        await msg.reply_text("🎬 Movie Myanmar ကို Join လုပ်ပြီးမှ သုံးလို့ရမယ်နော်။", reply_markup=join_keyboard()); return
    if text.lower() in ("home", "menu", "/home"):
        user_search_mode.pop(uid, None); user_ai_mode.pop(uid, None); await show_home(update, context); return
    if is_channel_request(text):
        await msg.reply_text("📺 Movie Myanmar Channel", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Open Channel", url=MOVIE_CHANNEL_URL)]])); return
    if user_search_mode.get(uid):
        user_search_mode.pop(uid, None)
        results = movie_search(text)
        if results: await show_movie_list(msg, results, "🔎 <b>Search Results</b>")
        else:
            await send_admin_request(context, update, text)
            await msg.reply_text(f"😒 <b>{html.escape(text)}</b> ကို Movie Myanmar ထဲမှာ မတွေ့သေးဘူးဟာ။\n\nRequest ပို့ထားပေးတယ်။", parse_mode="HTML")
        return
    if user_ai_mode.get(uid):
        if get_ai_usage(uid) >= FREE_AI_LIMIT:
            user_ai_mode.pop(uid, None); await msg.reply_text("ဟဲ့ 😒 AI quota ကုန်သွားပြီနော်။\n\nငါ့ကို Movie မေး 😝🎬\nကားရှာပေးတာတော့ မရပ်ဘူးဟဲ့။"); return
        ans = await ask_gemini(text)
        if ans:
            increase_ai_usage(uid); await msg.reply_text(ans + f"\n\n🤖 AI ကျန် — {FREE_AI_LIMIT-get_ai_usage(uid)} ခါ")
        else: await msg.reply_text("😵 AI ခဏအလုပ်များနေတယ်။ Movie နာမည်နဲ့လည်း စမ်းရှာလို့ရတယ်နော်။")
        return
    results = movie_search(text)
    if results and (has_movie_intent(text) or len(text.split()) <= 5):
        await show_movie_list(msg, results, "🔎 <b>Movie Results</b>"); return
    if has_movie_intent(text):
        await send_admin_request(context, update, text)
        await msg.reply_text(f"😒 <b>{html.escape(text)}</b> ကို မတွေ့သေးဘူးဟ။ Request ပို့ထားပေးတယ်။\n\nMovie နာမည်ကို တိတိကျကျရေးရင် ပိုကောင်းတယ် 😝", parse_mode="HTML"); return
    if await handle_special_reply(msg, text): return
    await msg.reply_text("😝 ဘာလုပ်မလဲ? Movie ရှာချင်ရင် <b>Search Movie</b> ကိုနှိပ်၊ AI ပြောချင်ရင် <b>Xiao Yu AI</b> ကိုနှိပ်နော်။", parse_mode="HTML", reply_markup=home_keyboard())


async def callback_handler(update, context):
    q = update.callback_query; data = q.data or ""
    if data == "check_join": return await check_join(update, context)
    if data == "home": return await show_home(update, context)
    if data == "search": return await start_search(update, context)
    if data == "categories": return await show_categories(update, context)
    if data == "latest": return await show_latest_movies(update, context)
    if data == "ai": return await start_ai(update, context)
    if data == "help": return await show_help(update, context)
    if data.startswith("genre:"): return await show_genre(update, context, data.split(":",1)[1])
    if data == "next_poster":
        await q.answer(); await q.message.reply_text("⏭️ Latest movie carousel ကို Mini App ထဲမှာ ပိုလှအောင်ကြည့်လို့ရတယ် 😝", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ Open Movie App", web_app=WebAppInfo(url=RENDER_URL+"/app"))] if WebAppInfo else [InlineKeyboardButton("📺 Movie Channel", url=MOVIE_CHANNEL_URL)]])); return
    await q.answer()


async def channel_post_handler(update, context):
    msg = update.channel_post
    if not msg or (msg.chat.username or "").lower() != MAIN_CHANNEL.lstrip("@").lower(): return
    movie = parse_movie_post(msg)
    movie["post_link"] = f"https://t.me/{msg.chat.username}/{msg.message_id}"
    replaced = False
    for i, old in enumerate(movie_database):
        if str(old.get("chat_id")) == str(msg.chat_id) and old.get("message_id") == msg.message_id:
            movie_database[i] = movie; replaced = True; break
    if not replaced: movie_database.append(movie)
    save_movies(); invalidate_carousel()
    print("Saved movie:", movie.get("title"))


# ---------------- Embedded Mini App ----------------
MINI_APP_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>Movie Myanmar</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
*{box-sizing:border-box}body{margin:0;background:#07070d;color:#f6f4ff;font-family:Arial,sans-serif;overflow-x:hidden}.app{width:100%;min-height:100vh;background:radial-gradient(circle at 20% 0,#22104a 0,#0b0913 38%,#05050a 100%);padding-bottom:28px}.top{position:sticky;top:0;z-index:10;padding:18px 16px 12px;background:rgba(7,7,13,.82);backdrop-filter:blur(16px);border-bottom:1px solid #282039}.brand{font-size:24px;font-weight:900;letter-spacing:1px}.brand span{color:#6cf6ff}.sub{font-size:12px;color:#aaa3bd;margin-top:4px}.search{margin-top:15px;display:flex;gap:8px}.search input{flex:1;background:#11101b;border:1px solid #302846;border-radius:14px;color:white;padding:13px 14px;outline:none}.search button,.pill{border:0;border-radius:13px;padding:0 15px;background:linear-gradient(135deg,#5af5ff,#bb5cff);color:#08080e;font-weight:800}.section{padding:18px 14px 0}.sectionhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:11px}.sectionhead h2{font-size:16px;margin:0}.muted{color:#8f899f;font-size:11px}.rail{display:flex;gap:11px;overflow-x:auto;padding-bottom:5px;scroll-snap-type:x mandatory}.rail::-webkit-scrollbar{display:none}.card{min-width:150px;width:150px;scroll-snap-align:start;background:#100e18;border:1px solid #29233a;border-radius:16px;overflow:hidden;box-shadow:0 10px 30px #0007}.poster{height:215px;width:100%;object-fit:cover;background:#181522}.ct{padding:10px}.title{font-size:13px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{font-size:10px;color:#9d96ad;margin-top:5px}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{padding:10px 13px;border:1px solid #302a43;background:#12101c;border-radius:13px;font-size:12px}.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.nav button{padding:12px 7px;border:1px solid #302a43;background:#11101a;color:white;border-radius:13px;font-weight:700}.empty{padding:30px 10px;text-align:center;color:#9992a8}.overlay{display:none;position:fixed;inset:0;background:#000b;z-index:30;padding:30px 14px}.modal{margin:auto;max-width:480px;background:#11101a;border:1px solid #3a3150;border-radius:20px;overflow:hidden;max-height:90vh;overflow-y:auto}.modal img{width:100%;max-height:390px;object-fit:cover}.mb{padding:16px}.close{float:right;background:#292338;border:0;color:white;border-radius:10px;padding:7px 11px}.open{display:block;width:100%;padding:13px;border:0;border-radius:13px;background:linear-gradient(135deg,#5af5ff,#bb5cff);font-weight:900;margin-top:14px}.loader{text-align:center;padding:20px;color:#aaa}</style></head><body><main class="app"><header class="top"><div class="brand">MOVIE <span>MYANMAR</span></div><div class="sub">Xiao Yu CyB • Cinema Mini App</div><div class="search"><input id="q" placeholder="Search movie..."><button onclick="searchMovies()">🔎</button></div></header><section class="section"><div class="nav"><button onclick="loadLatest()">🔥 Latest</button><button onclick="showGenres()">🎭 Genres</button><button onclick="openAi()">🤖 Xiao Yu</button></div></section><section class="section"><div class="sectionhead"><h2>NOW SHOWING</h2><span class="muted">Swipe →</span></div><div id="rail" class="rail"><div class="loader">Loading movies...</div></div></section><section class="section"><div class="sectionhead"><h2>GENRES</h2></div><div id="genres" class="chips"></div></section><section id="results" class="section"></section></main><div id="overlay" class="overlay" onclick="if(event.target===this)closeModal()"><div id="modal" class="modal"></div></div><script>
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}const $=id=>document.getElementById(id);let movies=[];
function esc(s){return String(s||'').replace(/[&<>\"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\':'&#92;','"':'&quot;'}[m]))}
function card(m){return `<article class="card" onclick="detail(${m.id})"><img class="poster" src="${m.poster?'/api/poster?id='+m.id:''}" onerror="this.style.visibility='hidden'"><div class="ct"><div class="title">${esc(m.title)}</div><div class="meta">${esc(m.year||'')} ${esc(m.quality||'')}</div></div></article>`}
async function api(u){const r=await fetch(u);return r.json()}
async function loadLatest(){const d=await api('/api/latest');movies=d.movies||[];$('rail').innerHTML=movies.map(card).join('')||'<div class="empty">No movies yet</div>';}
async function searchMovies(){const q=$('q').value.trim();if(!q)return;const d=await api('/api/search?q='+encodeURIComponent(q));renderResults('SEARCH RESULTS',d.movies||[])}
function renderResults(t,arr){$('results').innerHTML=`<div class="sectionhead"><h2>${t}</h2><span class="muted">${arr.length} found</span></div>`+`<div class="rail">${arr.map(card).join('')||'<div class="empty">Not found 😒</div>'}</div>`}
async function showGenres(){const d=await api('/api/genres');$('genres').innerHTML=(d.genres||[]).map(g=>`<button class="chip" onclick="genre('${encodeURIComponent(g)}')">${esc(g)}</button>`).join('')}
async function genre(g){const d=await api('/api/genre?name='+g);renderResults('🎭 '+decodeURIComponent(g),d.movies||[])}
async function detail(id){const m=await api('/api/movie?id='+id);if(!m.title)return;$('modal').innerHTML=`<button class="close" onclick="closeModal()">✕</button><img src="${m.poster?'/api/poster?id='+m.id:''}"><div class="mb"><h2>${esc(m.title)}</h2><div class="meta">${esc(m.year)} • ${esc(m.quality)} • ${esc(m.audio)}</div><p>${esc(m.description||'Movie Myanmar')}</p><a href="${m.link}" target="_blank"><button class="open">🎬 Open Movie</button></a></div>`;$('overlay').style.display='flex'}
function closeModal(){$('overlay').style.display='none'}function openAi(){if(tg&&tg.sendData){tg.sendData('ai');tg.close()}else alert('Open Xiao Yu AI from the Telegram bot menu 😝')}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')searchMovies()});loadLatest();showGenres();
</script></body></html>'''


def mini_movie(m, idx):
    return {"id": idx, "title": m.get("title", "Movie"), "year": m.get("year", ""), "quality": m.get("quality", ""), "audio": m.get("audio", ""), "genres": m.get("genres", []), "description": m.get("description", ""), "link": build_movie_link(m), "poster": bool(m.get("poster_file_id"))}


def get_poster_bytes(index):
    if not (0 <= index < len(movie_database)): return None, "image/jpeg"
    fid = movie_database[index].get("poster_file_id")
    if not fid: return None, "image/jpeg"
    if fid in poster_cache: return poster_cache[fid]
    try:
        req = Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={fid}")
        data = json.loads(urlopen(req, timeout=12).read().decode())
        path = data.get("result", {}).get("file_path")
        if not path: return None, "image/jpeg"
        raw = urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=20).read()
        poster_cache[fid] = (raw, "image/jpeg")
        return poster_cache[fid]
    except Exception as e:
        print("poster error:", e); return None, "image/jpeg"


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send_bytes(self, data, content_type="text/plain; charset=utf-8", status=200):
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def send_json(self, obj, status=200): self.send_bytes(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)
    def do_GET(self):
        try:
            p = urlparse(self.path); path=p.path; qs=parse_qs(p.query)
            if path in ("/", "/health"):
                return self.send_bytes(b"Movie Myanmar Bot is alive")
            if path in ("/app", "/miniapp"):
                return self.send_bytes(MINI_APP_HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/movies" or path == "/api/latest":
                n=min(int(qs.get("limit", [20])[0]),50); return self.send_json({"movies":[mini_movie(m,i) for i,m in enumerate(reversed(movie_database[-n:]))]})
            if path == "/api/search":
                q=qs.get("q", [""])[0]; arr=movie_search(q)[:30]; return self.send_json({"movies":[mini_movie(m,movie_database.index(m)) for m in arr]})
            if path == "/api/genres":
                return self.send_json({"genres":list(GENRE_KEYWORDS.keys())})
            if path == "/api/genre":
                g=qs.get("name", [""])[0]; arr=latest(50) if g=="All" else [m for m in reversed(movie_database) if g in m.get("genres", [])][:50]
                return self.send_json({"movies":[mini_movie(m,movie_database.index(m)) for m in arr]})
            if path == "/api/movie":
                i=int(qs.get("id", ["-1"])[0]); return self.send_json(mini_movie(movie_database[i],i) if 0<=i<len(movie_database) else {})
            if path == "/api/poster":
                i=int(qs.get("id", ["-1"])[0]); raw,ct=get_poster_bytes(i)
                return self.send_bytes(raw or b"", ct, 200 if raw else 404)
            return self.send_bytes(b"Not Found", status=404)
        except Exception as e:
            print("GET error:", traceback.format_exc()); return self.send_bytes(b"Server error", status=500)
    def do_POST(self):
        global webhook_application, webhook_loop
        try:
            if urlparse(self.path).path != WEBHOOK_PATH: return self.send_bytes(b"Not Found", status=404)
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET: return self.send_bytes(b"Forbidden", status=403)
            n=int(self.headers.get("Content-Length", "0")); body=self.rfile.read(n); data=json.loads(body.decode("utf-8"))
            update=Update.de_json(data, webhook_application.bot)
            if webhook_loop and webhook_application: asyncio.run_coroutine_threadsafe(webhook_application.update_queue.put(update), webhook_loop)
            return self.send_bytes(b"OK")
        except Exception:
            print("POST error:", traceback.format_exc()); return self.send_bytes(b"Bad Request", status=400)


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


async def main():
    global webhook_application, webhook_loop
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is missing")
    app=Application.builder().token(BOT_TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    webhook_application=app; webhook_loop=asyncio.get_running_loop()
    await app.initialize(); await app.start()
    if WebAppInfo and MenuButtonWebApp:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎬 Movie Myanmar", web_app=WebAppInfo(url=RENDER_URL+"/app")))
        except Exception as e: print("menu web app setup skipped:", e)
    threading.Thread(target=start_health_server, daemon=True).start()
    await app.bot.set_webhook(url=RENDER_URL+WEBHOOK_PATH, secret_token=WEBHOOK_SECRET, allowed_updates=["message","callback_query","channel_post"])
    print("Movie Myanmar Bot running")
    print("Mini App:", RENDER_URL+"/app")
    while True: await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
