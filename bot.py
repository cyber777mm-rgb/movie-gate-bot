import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]

MAIN_CHANNEL import= "@moviemyanmar567"
MOVIE_CHANNEL = "https://t.me/moviemyanmar568"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(MAIN_CHANNEL, user_id)

        if member.status in ["member", "administrator", "creator"]:
            keyboard = [[
                InlineKeyboardButton(
                    "🎥 ဇာတ်ကားကြည့်ရန်",
                    url=MOVIE_CHANNEL
                )
            ]]
            text = "✅ Main Channel ကို Join ထားပြီးပါပြီ။\n\nအောက်ကခလုတ်ကိုနှိပ်ပြီး ဇာတ်ကားကြည့်နိုင်ပါတယ်။"

        else:
            keyboard = [[
                InlineKeyboardButton(
                    "📢 Main Channel Join မယ်",
                    url="https://t.me/moviemyanmar567"
                )
            ]]
            text = "❌ အရင်ဆုံး Main Channel ကို Join ပါ။\n\nJoin ပြီးရင် /start ကို ပြန်နှိပ်ပါ။"

    except Exception:
        keyboard = [[
            InlineKeyboardButton(
                "📢 Main Channel Join မယ်",
                url="https://t.me/moviemyanmar567"
            )
        ]]
        text = "❌ အရင်ဆုံး Main Channel ကို Join ပါ။"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()

if __name__ == "__main__":
    main()

