from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from html import escape
from pathlib import Path
from secrets import token_hex

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from credits import ABOUT_CAPTION
from handlers.library import open_library
from formatting import (
    group_watchlist_text,
    media_caption,
    personal_list_text,
    personal_progress_text,
    random_pick_text,
    suggestions_text,
)
from handlers.common import (
    allowed,
    database,
    edit_personal_text,
    reply_personal_text,
    settings,
    tmdb,
    user_identity,
)
from keyboards import about_keyboard, media_actions_keyboard, search_results_keyboard
from models import PersonalEntry, SearchResult
from tmdb import TmdbError

SearchFunction = Callable[[str, int], Awaitable[list[SearchResult]]]

HELP_TEXT = """<b>Kokabiel commands</b>

<b>Discover</b>
/movie &lt;title&gt; — search films
/tv &lt;title&gt; — search TV and limited series
/documentary &lt;title&gt; — search documentary films and series
/search &lt;title&gt; — search everything together
/random — choose from the group watchlist

<b>Personal</b>
/mylibrary — browse your complete media library
/myratings — browse your personal ratings
/myreleases — show upcoming releases in your library
/mywatchlist — show your personal watchlist
/watched — show your watched, caught-up, and completed titles
/myprogress — show your film, TV, and documentary progress
/mysuggestions — show titles you suggested to this group
/import — import tracker data; export uploads are accepted only in a private chat

<b>Group</b>
/watchlist — show titles members saved to their watchlists
/suggestions — show the group suggestion list

/about — show Kokabiel credits and TMDB attribution
/help — show this message

Every member has independent watchlists, ratings, and episode progress.
Kokabiel responds only to commands and its own buttons.
It does not inspect ordinary group conversation.
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context, defer_activity=True):
        return
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context, defer_activity=True):
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
    if not await allowed(update, context, defer_activity=True):
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
        f"Choose the correct title for <b>{escape(query)}</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=search_results_keyboard(
            results, search_token=search_token, page=0
        ),
    )


async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entries = await database(context).list_group_watchlist(update.effective_chat.id)
    await update.effective_message.reply_text(
        group_watchlist_text(entries), parse_mode=ParseMode.HTML
    )


async def mywatchlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await open_library(
        update,
        context,
        collection="watchlist",
    )


async def watched(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await open_library(
        update,
        context,
        collection="watched",
    )


async def myprogress(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await open_library(
        update,
        context,
        collection="progress",
    )


async def _progress_released_counts(
    context: ContextTypes.DEFAULT_TYPE,
    entries: list[PersonalEntry],
) -> dict[tuple[str, int], tuple[int, int]]:
    """Resolve current released-episode totals without failing the dashboard."""

    client = tmdb(context)
    today = date.today().isoformat()
    semaphore = asyncio.Semaphore(4)

    async def resolve(
        entry: PersonalEntry,
    ) -> tuple[tuple[str, int], tuple[int, int] | None]:
        key = (entry.media_type, entry.tmdb_id)

        if entry.media_type != "tv":
            return key, None

        try:
            async with semaphore:
                details = await client.details(
                    entry.media_type,
                    entry.tmdb_id,
                )
                released = await client.released_episode_keys(
                    details,
                    today=today,
                )
        except TmdbError:
            return key, None

        watched = await database(context).watched_episode_keys(
            entry.chat_id,
            entry.user_id,
            entry.tmdb_id,
        )
        return key, (len(watched & released), len(released))

    results = await asyncio.gather(
        *(resolve(entry) for entry in entries if entry.media_type == "tv")
    )

    return {
        key: count
        for key, count in results
        if count is not None
    }


async def suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entries = await database(context).list_group_suggestions(update.effective_chat.id)
    await update.effective_message.reply_text(
        suggestions_text("Group suggestions", entries),
        parse_mode=ParseMode.HTML,
    )


async def mysuggestions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    user_id, user_name = user_identity(update)
    entries = await database(context).list_my_suggestions(
        update.effective_chat.id, user_id
    )
    await reply_personal_text(
        update,
        context,
        suggestions_text(f"{user_name}’s suggestions", entries, personal=True),
        parse_mode=ParseMode.HTML,
    )


async def random_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    entry = await database(context).random_group_watchlist(update.effective_chat.id)
    if entry is None:
        await update.effective_message.reply_text(
            "The group watchlist is empty. Members can add titles with My Watchlist."
        )
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
            reply_markup=media_actions_keyboard(details),
        )
    else:
        await update.effective_message.reply_text(
            media_caption(details, group_rating=average, rating_count=count),
            parse_mode=ParseMode.HTML,
            reply_markup=media_actions_keyboard(details),
        )
