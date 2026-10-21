def movie_identity(movie):
    """
    Return a stable identity for a movie.
    ID is preferred, then Telegram chat/message,
    then storage channel/media group.
    """
    if not isinstance(movie, dict):
        return None

    movie_id = str(movie.get("id", "") or "").strip()
    if movie_id:
        return ("id", movie_id)

    chat_id = str(movie.get("chat_id", "") or "").strip()
    message_id = str(movie.get("message_id", "") or "").strip()

    if chat_id and message_id:
        return ("message", chat_id, message_id)

    storage = str(
        movie.get("storage_channel", "") or ""
    ).strip()

    group_id = str(
        movie.get("media_group_id", "") or ""
    ).strip()

    if storage and group_id:
        return ("album", storage, group_id)

    return None


def merge_movie_lists(primary, secondary):
    """
    Merge movie lists safely.

    Primary is preferred when the same movie exists,
    but records that exist only in secondary are always kept.
    """

    result = []
    seen = set()

    for source in (primary, secondary):
        if not isinstance(source, list):
            continue

        for raw_movie in source:
            if not isinstance(raw_movie, dict):
                continue

            movie = dict(raw_movie)

            ensure_movie_id(movie)

            identity = movie_identity(movie)

            # If no useful identity exists, use the generated ID.
            if identity is None:
                identity = (
                    "id",
                    str(movie.get("id", "")),
                )

            if identity in seen:
                continue

            seen.add(identity)
            result.append(movie)

    return result
