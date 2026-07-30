from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from secrets import token_hex

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from credits import ABOUT_CAPTION
from formatting import catalog_text, media_caption, random_pick_text
from handlers.common import allowed, database, settings, tmdb
from keyboards import (
    about_keyboard,
    media_actions_keyboard,
    search_results_keyboard,
)
from models import SearchResult
from tmdb import TmdbError

SearchFunction = Callable[[str, int], Awaitable[list[SearchResult]]]

HELP_TEXT = """<b>Kokabiel commands</b>

/movie &lt;title&gt; — search films
/tv &lt;title&gt; — search TV series
/documentary &lt;title&gt; — search documentaries
/search &lt;title&gt; — search films and TV together
/watchlist — show the shared group watchlist
/watched — show titles marked watched
/random — choose from the shared watchlist
/about — show Kokabiel credits and TMDB attribution
/help — show this message

Kokabiel responds only to commands and its own buttons.
It does not inspect ordinary group conversation.
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return

    credits_card = Path(__file__).resolve().parent.parent / "assets" / "kokabiel-credits.png"
    with credits_card.open("rb") as photo:
        await update.effective_message.reply_photo(
            photo=photo,
            caption=ABOUT_CAPTION,
            parse_mode=ParseMode.HTML,
            reply_markup=about_keyboard(),
        )


async def movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _search(update, context, "movie", tmdb(context).search_movie)


async def tv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _search(update, context, "tv", tmdb(context).search_tv)


async def documentary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _search(update, context, "documentary", tmdb(context).search_documentary)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _search(update, context, "search", tmdb(context).search_multi)


async def _search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
    search_function: SearchFunction,
) -> None:
    if not await allowed(update, context):
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(f"Usage: /{command_name} <title>")
        return

    try:
        results = await search_function(query, settings(context).result_limit)
    except TmdbError:
        await update.effective_message.reply_text(
            "TMDB could not complete that search. Please try again shortly."
        )
        raise

    if not results:
        await update.effective_message.reply_text(f"No matching titles found for “{query}”.")
        return

    search_token = token_hex(4)
    search_cache = context.chat_data.setdefault("kokabiel_search_results", {})
    search_cache[search_token] = results
    while len(search_cache) > 20:
        search_cache.pop(next(iter(search_cache)))

    await update.effective_message.reply_text(
        f"Choose the correct title for <b>{_escape(query)}</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=search_results_keyboard(
            results, search_token=search_token, page=0
        ),
    )


async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entries = await database(context).list_entries(update.effective_chat.id, watchlist=True)
    await update.effective_message.reply_text(
        catalog_text("Group watchlist", entries), parse_mode=ParseMode.HTML
    )


async def watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entries = await database(context).list_entries(update.effective_chat.id, watched=True)
    await update.effective_message.reply_text(
        catalog_text("Watched by the group", entries), parse_mode=ParseMode.HTML
    )


async def random_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entry = await database(context).random_watchlist(update.effective_chat.id)
    if entry is None:
        await update.effective_message.reply_text("The group watchlist is empty.")
        return

    details = await tmdb(context).details(entry.media_type, entry.tmdb_id)
    average, count = await database(context).group_rating(
        update.effective_chat.id, entry.media_type, entry.tmdb_id
    )
    await update.effective_message.reply_text(
        random_pick_text(entry), parse_mode=ParseMode.HTML
    )
    if details.poster_url:
        await update.effective_message.reply_photo(
            details.poster_url,
            caption=media_caption(details, group_rating=average, rating_count=count),
            parse_mode=ParseMode.HTML,
            reply_markup=media_actions_keyboard(
                details, watchlist=entry.watchlist, watched=entry.watched
            ),
        )
    else:
        await update.effective_message.reply_text(
            media_caption(details, group_rating=average, rating_count=count),
            parse_mode=ParseMode.HTML,
            reply_markup=media_actions_keyboard(
                details, watchlist=entry.watchlist, watched=entry.watched
            ),
        )


def _escape(value: str) -> str:
    from html import escape

    return escape(value)
