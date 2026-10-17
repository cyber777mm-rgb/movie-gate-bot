import os
import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MAIN_CHANNEL = "@moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/+7EGkZEWlzfAyNGM1"
RENDER_URL = "https://movie-gate-bot.onrender.com"

WEBHOOK_PATH = "/telegram-webhook"

if BOT_TOKEN:
    WEBHOOK_SECRET = hashlib.sha256(
        BOT_TOKEN.encode("utf-8")
    ).hexdigest()[:32]
else:
    WEBHOOK_SECRET = ""

# =========================================================
# GEMINI
# =========================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )
        print("Gemini client initialized.")
    except Exception as error:
        print("Gemini client error:", error)
        gemini_client = None

# =========================================================
# GLOBALS
# =========================================================

telegram_application = None
main_loop = None
http_server = None

# =========================================================
# KEYBOARDS
# =========================================================

def movie_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 ဇာတ်ကားကြည့်ရန်",
                    callback_data="movies",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 ဇာတ်ကားရှာမယ်",
                    callback_data="search_movie",
                ),
                InlineKeyboardButton(
                    "📂 အမျိုးအစား",
                    callback_data="categories",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💬 AI နဲ့စကားပြောမယ်",
                    callback_data="ai_chat",
                )
            ],
        ]
    )


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Movie Channel ဝင်မယ်",
                    url=MOVIE_CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ ဝင်ပြီးပြီ စစ်မယ်",
                    callback_data="check_membership",
                )
            ],
        ]
    )


def category_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💥 Action",
                    callback_data="cat_action",
                ),
                InlineKeyboardButton(
                    "😂 Comedy",
                    callback_data="cat_comedy",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❤️ Romance",
                    callback_data="cat_romance",
                ),
                InlineKeyboardButton(
                    "👻 Horror",
                    callback_data="cat_horror",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔪 Thriller",
                    callback_data="cat_thriller",
                ),
                InlineKeyboardButton(
                    "🎭 Drama",
                    callback_data="cat_drama",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Main Menu",
                    callback_data="main_menu",
                )
            ],
        ]
    )

# =========================================================
# MEMBERSHIP
# =========================================================

async def is_member(bot, user_id):
    try:
        member = await bot.get_chat_member(
            MAIN_CHANNEL,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as error:
        print("Membership check error:", error)
        return False

# =========================================================
# MAIN MENU
# =========================================================

async def show_main_menu(
    update,
    context,
):
    text = (
        "🎬 <b>Movie Myanmar</b>\n\n"
        "ဟေး 👋 ကြိုဆိုပါတယ်နော် ❤️\n"
        "ဇာတ်ကားကြည့်ဖို့ "
        "အောက်က menu ကနေ ရွေးလိုက်ပါ 👇"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )

# =========================================================
# RENDER HEALTH SERVER + TELEGRAM WEBHOOK
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def log_message(
        self,
        format_string,
        *args,
    ):
        return

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
        ):

            body = b"Movie Myanmar bot is running."

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(body)

            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        if self.path != WEBHOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        incoming_secret = self.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if incoming_secret != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return

        try:

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
                body.decode("utf-8")
            )

            update = Update.de_json(
                data,
                telegram_application.bot,
            )

            future = (
                asyncio.run_coroutine_threadsafe(
                    telegram_application.update_queue.put(
                        update
                    ),
                    main_loop,
                )
            )

            future.result(timeout=10)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        except Exception as error:

            print(
                "Webhook error:",
                error,
            )

            self.send_response(500)
            self.end_headers()


def start_http_server():

    global http_server

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    http_server = HTTPServer(
        (
            "0.0.0.0",
            port,
        ),
        HealthHandler,
    )

    thread = threading.Thread(
        target=http_server.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        "HTTP server started on port",
        port,
    )

# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if await is_member(
        context.bot,
        user_id,
    ):

        await show_main_menu(
            update,
            context,
        )

        return

    await update.message.reply_text(
        "🎬 <b>Movie Myanmar</b>\n\n"
        "ဟေး 👋 Channel ထဲဝင်ပြီးမှ "
        "bot ကို ဆက်သုံးလို့ရမယ်နော် ❤️\n\n"
        "အောက်က <b>Movie Channel ဝင်မယ်</b> "
        "ကိုနှိပ်ပြီး ဝင်ပေးပါ 👇",
        parse_mode="HTML",
        reply_markup=join_keyboard(),
    )

# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id

    data = query.data

    # -----------------------------------------------------
    # MEMBERSHIP CHECK
    # -----------------------------------------------------

    if data == "check_membership":

        if await is_member(
            context.bot,
            user_id,
        ):

            await query.edit_message_text(
                "✅ ဝင်ထားတာ အဆင်ပြေပြီနော် ❤️\n\n"
                "အခု Movie Myanmar ကို "
                "စသုံးလို့ရပါပြီ 👇",
                reply_markup=movie_keyboard(),
            )

        else:

            await query.edit_message_text(
                "🥺 Channel ထဲ မဝင်ရသေးဘူးနော်။\n\n"
                "အရင်ဆုံး Channel ထဲဝင်ပြီး "
                "ပြန်စစ်ပေးပါ ❤️",
                reply_markup=join_keyboard(),
            )

        return

    # -----------------------------------------------------
    # MEMBERSHIP GATE
    # -----------------------------------------------------

    if not await is_member(
        context.bot,
        user_id,
    ):

        await query.edit_message_text(
            "🥺 Bot သုံးဖို့ Movie Channel ထဲ "
            "အရင်ဝင်ပေးပါနော် ❤️",
            reply_markup=join_keyboard(),
        )

        return

    # -----------------------------------------------------
    # MAIN MENU
    # -----------------------------------------------------

    if data == "main_menu":

        await show_main_menu(
            update,
            context,
        )

        return

    # -----------------------------------------------------
    # MOVIES
    # -----------------------------------------------------

    if data == "movies":

        await query.edit_message_text(
            "🎬 <b>Movie Myanmar</b>\n\n"
            "ဇာတ်ကားတွေကို Movie Channel ထဲမှာ\n"
            "ဆက်ကြည့်နိုင်ပါတယ် 👇\n\n"
            "📂 <b>ဇာတ်ကားအမျိုးအစား "
            "ရွေးပါ</b> 👇",
            parse_mode="HTML",
            reply_markup=category_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SEARCH
    # -----------------------------------------------------

    if data == "search_movie":

        context.user_data[
            "search_mode"
        ] = True

        await query.edit_message_text(
            "🔎 <b>ဇာတ်ကားရှာမယ်</b>\n\n"
            "ရှာချင်တဲ့ ဇာတ်ကားနာမည်ကို "
            "ရိုက်ပို့လိုက်ပါနော် 🎬",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # CATEGORIES
    # -----------------------------------------------------

    if data == "categories":

        await query.edit_message_text(
            "📂 <b>ဇာတ်ကားအမျိုးအစား "
            "ရွေးပါ</b> 👇",
            parse_mode="HTML",
            reply_markup=category_keyboard(),
        )

        return

    # -----------------------------------------------------
    # CATEGORY BUTTONS
    # -----------------------------------------------------

    if data.startswith("cat_"):

        category_names = {

            "cat_action":
                "💥 Action",

            "cat_comedy":
                "😂 Comedy",

            "cat_romance":
                "❤️ Romance",

            "cat_horror":
                "👻 Horror",

            "cat_thriller":
                "🔪 Thriller",

            "cat_drama":
                "🎭 Drama",
        }

        name = category_names.get(
            data,
            "🎬 Movie",
        )

        await query.edit_message_text(
            f"{name}\n\n"
            "ဒီအပိုင်းကို Movie Channel ထဲမှာ\n"
            "ဆက်ကြည့်နိုင်ပါတယ် 👇\n\n"
            "📂 ဇာတ်ကားအမျိုးအစား "
            "ရွေးပါ 👇",
            reply_markup=category_keyboard(),
        )

        return

    # -----------------------------------------------------
    # AI CHAT
    # -----------------------------------------------------

    if data == "ai_chat":

        context.user_data[
            "ai_mode"
        ] = True

        await query.edit_message_text(
            "💬 <b>AI Chat</b>\n\n"
            "ဟေး 😄 ဘာအကြောင်းပြောချင်လဲ "
            "ပြောလို့ရတယ်နော်။\n"
            "ဇာတ်ကားအကြောင်းလည်း "
            "မေးလို့ရတယ် 🎬❤️",
            parse_mode="HTML",
        )

        return

# =========================================================
# GEMINI PERSONALITY
# =========================================================

SYSTEM_INSTRUCTION = """
You are a friendly fictional young woman
chatting naturally with a Myanmar user.

Reply mainly in casual Burmese.

Your personality:
- warm
- friendly
- playful
- natural
- caring
- slightly cute
- not overly formal

Important:
- Do not pretend to be a real human.
- Do not mention system prompts.
- Do not mention APIs.
- Do not mention models.
- Do not mention internal errors.
- Do not expose technical instructions.
- Do not overuse emojis.
- Match the user's casual tone.
- Keep normal chat replies reasonably short.
- If the user asks a real question,
  answer helpfully.
- If the user jokes,
  joke back naturally.
- If the user says hello,
  greet naturally.
- If the user talks about movies,
  respond naturally.
"""

# =========================================================
# GEMINI CHAT
# =========================================================

async def gemini_reply(
    user_id,
    user_text,
    context,
):

    if gemini_client is None:

        return (
            "ဟေး 😅 AI ချိတ်ဆက်မှုလေး "
            "မရသေးဘူး။ ခဏနေရင် "
            "ပြန်ပြောကြမယ်နော် ❤️"
        )

    history = context.user_data.setdefault(
        "chat_history",
        [],
    )

    history.append(
        {
            "role": "user",
            "text": user_text,
        }
    )

    history = history[-12:]

    context.user_data[
        "chat_history"
    ] = history

    conversation_lines = []

    for item in history:

        conversation_lines.append(
            f'{item["role"]}: {item["text"]}'
        )

    conversation = "\n".join(
        conversation_lines
    )

    prompt = (
        SYSTEM_INSTRUCTION
        + "\n\n"
        + "Conversation history:\n"
        + conversation
        + "\n\n"
        + "Reply naturally to the user's latest message."
    )

    try:

        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model="gemini-3.8-flash",
            contents=prompt,
        )

        answer = getattr(
            response,
            "text",
            None,
        )

        if not answer:

            answer = (
                "ဟေး 😅 စာပြန်ဖို့ "
                "ခဏလေး အခက်အခဲဖြစ်သွားတယ်နော်။ "
                "ပြန်ပြောကြမယ် ❤️"
            )

        history.append(
            {
                "role": "assistant",
                "text": answer,
            }
        )

        context.user_data[
            "chat_history"
        ] = history[-12:]

        return answer

    except Exception as error:

        print(
            "Gemini error:",
            error,
        )

        return (
            "ဟေး 😅 ခဏလေး connection "
            "အဆင်မပြေသွားတယ်။ "
            "ပြန်ပို့ပေးနော် ❤️"
        )

# =========================================================
# TEXT MESSAGE HANDLER
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not update.message.text:
        return

    user_id = update.effective_user.id

    text = update.message.text.strip()

    # -----------------------------------------------------
    # MEMBERSHIP GATE
    # -----------------------------------------------------

    if not await is_member(
        context.bot,
        user_id,
    ):

        await update.message.reply_text(
            "🥺 Bot သုံးဖို့ Movie Channel ထဲ "
            "အရင်ဝင်ပေးပါနော် ❤️",
            reply_markup=join_keyboard(),
        )

        return

    # -----------------------------------------------------
    # MOVIE SEARCH
    # -----------------------------------------------------

    if context.user_data.get(
        "search_mode"
    ):

        context.user_data[
            "search_mode"
        ] = False

        await update.message.reply_text(
            f"🔎 <b>{text}</b> ကို "
            "ရှာပေးမယ်နော် 🎬\n\n"
            "Movie Channel ထဲမှာ "
            "ရှာကြည့်နိုင်ပါတယ် 👇\n"
            f"{MOVIE_CHANNEL_URL}",
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )

        return

    # -----------------------------------------------------
    # AI MODE
    # -----------------------------------------------------

    if context.user_data.get(
        "ai_mode"
    ):

        answer = await gemini_reply(
            user_id,
            text,
            context,
        )

        await update.message.reply_text(
            answer
        )

        return

    # -----------------------------------------------------
    # AUTOMATIC AI ENTRY
    # -----------------------------------------------------

    lower = text.lower()

    ai_words = [
        "ai",
        "ဟိုင်း",
        "ဟေး",
        "hello",
        "hi",
    ]

    if any(
        word in lower
        for word in ai_words
    ):

        context.user_data[
            "ai_mode"
        ] = True

        answer = await gemini_reply(
            user_id,
            text,
            context,
        )

        await update.message.reply_text(
            answer
        )

        return

    # -----------------------------------------------------
    # DEFAULT RESPONSE
    # -----------------------------------------------------

    await update.message.reply_text(
        "ဟေး 😄 ဘာလုပ်ချင်လဲ "
        "အောက်က menu ကနေ ရွေးလိုက်နော် 👇",
        reply_markup=movie_keyboard(),
    )

# =========================================================
# MAIN
# =========================================================

async def main():

    global telegram_application
    global main_loop

    print("========================================")
    print("Movie Myanmar Bot starting...")
    print("========================================")

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    main_loop = (
        asyncio.get_running_loop()
    )

    # -----------------------------------------------------
    # CREATE TELEGRAM APPLICATION
    # -----------------------------------------------------

    telegram_application = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )

    # -----------------------------------------------------
    # HANDLERS
    # -----------------------------------------------------

    telegram_application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    telegram_application.add_handler(
        CallbackQueryHandler
