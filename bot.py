import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes


BOT_TOKEN = os.environ["BOT_TOKEN"]

MAIN_CHANNEL = "@moviemyanmar567"
MAIN_CHANNEL_URL = "https://t.me/moviemyanmar567"
MOVIE_CHANNEL_URL = "https://t.me/moviemyanmar568"


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Movie Myanmar Bot is running")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(
            chat_id=MAIN_CHANNEL,
            user_id=user_id
        )

        if member.status in ["member", "administrator", "creator"]:
            keyboard = [[
                InlineKeyboardButton(
                    "🎥 ဇာတ်ကားကြည့်ရန်",
                    url=MOVIE_CHANNEL_URL
                )
            ]]

            text = (
                "✅ Main Channel ကို Join ထားပြီးပါပြီ။\n\n"
                "အောက်ကခလုတ်ကိုနှိပ်ပြီး ဇာတ်ကားကြည့်နိုင်ပါတယ်။"
            )

        else:
            keyboard = [[
                InlineKeyboardButton(
                    "📢 Main Channel Join မယ်",
                    url=MAIN_CHANNEL_URL
                )
            ]]

            text = (
                "❌ အရင်ဆုံး Main Channel ကို Join ပါ။\n\n"
                "Join ပြီးရင် /start ကို ပြန်နှိပ်ပါ။"
            )

    except Exception as e:
        print(f"Membership check error: {e}")

        keyboard = [[
            InlineKeyboardButton(
                "📢 Main Channel Join မယ်",
                url=MAIN_CHANNEL_URL
            )
        ]]

        text = (
            "❌ Main Channel ကို စစ်ဆေးလို့မရပါ။\n\n"
            "Main Channel ကို Join လုပ်ပြီး /start ကို ပြန်နှိပ်ပါ။"
        )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def main():
    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()


if __name__ == "__main__":
    main()
