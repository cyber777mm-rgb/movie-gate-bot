async def reply_movie_search(
    update,
    context,
    query
):

    results = movie_search(
        query
    )

    if not results:

        requested = await send_movie_request(
            update,
            context,
            query
        )

        answer = (
            f"🔎 <b>{query}</b> ကို "
            "Movie Myanmar ထဲမှာ မတွေ့သေးဘူးနော် 😒\n\n"
            "ဟွန့်... အခုချက်ချင်းကြည့်ချင်တာလား 😂\n"
            "နည်းနည်းစောင့်ပေးဦးဟာ 🎬\n\n"
            "📩 ဒီကား Request ကို "
            "<b>ကိုကို လင်းမြတ်</b> ဆီ ပို့ပေးလိုက်ပြီ ❤️"
        )

        await update.message.reply_text(
            answer,
            parse_mode="HTML",
            reply_markup=movie_keyboard(),
        )

        return

    buttons = []

    for movie in results:

        name = movie.get(
            "name",
            "Unknown Movie"
        )

        year = movie.get(
            "year",
            ""
        )

        label = f"🎬 {name}"

        if year:
            label += f" ({year})"

        buttons.append([
            InlineKeyboardButton(
                label,
                url=movie.get(
                    "post_link",
                    MOVIE_CHANNEL_URL
                )
            )
        ])

    text = (
        f"🔎 <b>{query}</b>\n\n"
        f"🎬 Movie Myanmar ထဲမှာ "
        f"<b>{len(results)}</b> ကားတွေ့တယ်နော် ❤️\n\n"
        "ကြည့်ချင်တဲ့ကားကို နှိပ်လိုက် 👇"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )
