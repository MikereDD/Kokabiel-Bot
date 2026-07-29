from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from formatting import media_caption
from handlers.common import (
    allowed,
    database,
    media_type_from_short,
    tmdb,
    user_identity,
)
from keyboards import (
    SEARCH_PAGE_SIZE,
    media_actions_keyboard,
    rating_keyboard,
    search_results_keyboard,
)
from models import MediaDetails
from tmdb import TmdbError

LOGGER = logging.getLogger(__name__)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not await allowed(update, context):
        return

    data = query.data or ""
    parts = data.split("|")
    try:
        if len(parts) == 3 and parts[0] == "pick":
            await _pick(update, context, parts[1], int(parts[2]))
            return
        if len(parts) == 3 and parts[0] == "page":
            await _search_page(update, context, parts[1], int(parts[2]))
            return
        if len(parts) == 4 and parts[0] == "act":
            await _action(update, context, parts[1], parts[2], int(parts[3]))
            return
        if len(parts) == 4 and parts[0] == "score":
            await _score(update, context, int(parts[1]), parts[2], int(parts[3]))
            return
    except (ValueError, TmdbError):
        LOGGER.exception("Callback failed: %s", data)
        await query.answer("That action could not be completed.", show_alert=True)
        return

    await query.answer("That button is no longer valid.")


async def _search_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, search_token: str, page: int
) -> None:
    query = update.callback_query
    search_cache = context.chat_data.get("kokabiel_search_results", {})
    results = search_cache.get(search_token)
    if not results:
        await query.answer(
            "Those search results expired. Run the command again.",
            show_alert=True,
        )
        return

    page_count = max(1, (len(results) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    if page < 0 or page >= page_count:
        await query.answer("That results page is no longer available.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=search_results_keyboard(
            results, search_token=search_token, page=page
        )
    )


async def _pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE, short_type: str, tmdb_id: int
) -> None:
    query = update.callback_query
    await query.answer()
    media_type = media_type_from_short(short_type)
    details = await tmdb(context).details(media_type, tmdb_id)
    average, count = await database(context).group_rating(
        update.effective_chat.id, media_type, tmdb_id
    )

    try:
        await query.edit_message_text(
            f"Selected: <b>{_escape(details.title)}</b> ({_escape(details.year)})",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass

    kwargs = {
        "caption": media_caption(details, group_rating=average, rating_count=count),
        "parse_mode": ParseMode.HTML,
        "reply_markup": media_actions_keyboard(details),
    }
    if details.poster_url:
        await context.bot.send_photo(update.effective_chat.id, details.poster_url, **kwargs)
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            text=kwargs.pop("caption"),
            parse_mode=ParseMode.HTML,
            reply_markup=kwargs["reply_markup"],
        )


async def _action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    short_type: str,
    tmdb_id: int,
) -> None:
    query = update.callback_query
    media_type = media_type_from_short(short_type)
    details = await tmdb(context).details(media_type, tmdb_id)
    chat_id = update.effective_chat.id
    user_id, user_name = user_identity(update)

    if action == "watchlist":
        await database(context).set_watchlist(chat_id, details, user_id, user_name)
        await query.answer("Added to the group watchlist.")
    elif action == "watched":
        await database(context).set_watched(chat_id, details, user_id, user_name)
        await query.answer("Marked as watched by the group.")
    elif action == "recommend":
        created = await database(context).recommend(
            chat_id, details, user_id, user_name
        )
        await query.answer(
            "Recommendation recorded."
            if created
            else "You already recommended this title."
        )
    elif action == "rate":
        await query.answer("Choose a rating from 1 to 10.")
        await query.edit_message_reply_markup(reply_markup=rating_keyboard(details))
        return
    elif action == "back":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=media_actions_keyboard(details))
        return
    else:
        await query.answer("That action is not available.")
        return

    await _refresh_card(update, context, details)


async def _score(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rating: int,
    short_type: str,
    tmdb_id: int,
) -> None:
    if not 1 <= rating <= 10:
        raise ValueError("invalid rating")

    query = update.callback_query
    media_type = media_type_from_short(short_type)
    details = await tmdb(context).details(media_type, tmdb_id)
    user_id, user_name = user_identity(update)
    await database(context).rate(
        update.effective_chat.id, details, user_id, user_name, rating
    )
    await query.answer(f"Your rating: {rating}/10")
    await _refresh_card(update, context, details)


async def _refresh_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, details: MediaDetails
) -> None:
    query = update.callback_query
    average, count = await database(context).group_rating(
        update.effective_chat.id, details.media_type, details.tmdb_id
    )
    caption = media_caption(details, group_rating=average, rating_count=count)
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=media_actions_keyboard(details),
            )
        else:
            await query.edit_message_text(
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=media_actions_keyboard(details),
            )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


def _escape(value: str) -> str:
    from html import escape

    return escape(value)
