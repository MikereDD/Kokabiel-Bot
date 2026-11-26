from __future__ import annotations

import logging
from datetime import date
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from formatting import media_caption, progress_summary_text
from handlers.common import (
    allowed,
    database,
    delete_personal_message,
    edit_personal_text,
    media_type_from_short,
    send_personal_callback_text,
    tmdb,
    user_identity,
)
from handlers.imports import import_callback_router
from handlers.library import library_callback
from handlers.owner import owner_callback_router
from keyboards import (
    EPISODE_PAGE_SIZE,
    SEARCH_PAGE_SIZE,
    SEASON_PAGE_SIZE,
    episode_list_keyboard,
    media_actions_keyboard,
    progress_menu_keyboard,
    rating_keyboard,
    search_results_keyboard,
    season_list_keyboard,
)
from models import EpisodeDetails, MediaDetails, SeasonDetails
from tmdb import TmdbError

LOGGER = logging.getLogger(__name__)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    data = query.data or ""
    if data.startswith("own|"):
        await owner_callback_router(update, context)
        return

    if data.startswith("lib|"):
        await library_callback(update, context)
        return

    if data.startswith("imp|"):
        await import_callback_router(update, context)
        return

    if not await allowed(update, context):
        return

    parts = data.split("|")
    try:
        if data == "noop|future":
            await query.answer("That episode has not aired yet.", show_alert=True)
            return
        if len(parts) == 3 and parts[:2] == ["menu", "close"]:
            await _close_personal_menu(update, context, int(parts[2]))
            return
        if len(parts) == 3 and parts[0] == "pick":
            await _pick(update, context, parts[1], int(parts[2]))
            return
        if len(parts) == 3 and parts[0] == "page":
            await _search_page(update, context, parts[1], int(parts[2]))
            return
        if len(parts) == 4 and parts[0] == "act":
            await _action(update, context, parts[1], parts[2], int(parts[3]))
            return
        if len(parts) == 5 and parts[0] == "score":
            await _score(
                update,
                context,
                int(parts[1]),
                parts[2],
                int(parts[3]),
                int(parts[4]),
            )
            return
        if parts and parts[0] == "prog":
            await _progress_router(update, context, parts)
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
    chat_id = update.effective_chat.id
    average, count = await database(context).group_rating(chat_id, media_type, tmdb_id)

    try:
        await query.edit_message_text(
            f"Selected: <b>{escape(details.title)}</b> ({escape(details.year)})",
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
        enabled = await database(context).toggle_watchlist(
            chat_id, details, user_id, user_name
        )
        await query.answer(
            f"{details.title} added to your watchlist."
            if enabled
            else f"{details.title} removed from your watchlist."
        )
        return

    if action == "watched":
        if details.media_type != "movie":
            await query.answer("Use My Progress for a series.", show_alert=True)
            return
        enabled = await database(context).toggle_movie_watched(
            chat_id, details, user_id, user_name
        )
        await query.answer(
            f"{details.title} marked watched."
            if enabled
            else f"Watched status removed from {details.title}."
        )
        return

    if action in {"suggest", "recommend"}:
        enabled = await database(context).toggle_suggestion(
            chat_id, details, user_id, user_name
        )
        await query.answer(
            f"{details.title} added to the group suggestions."
            if enabled
            else f"Your suggestion for {details.title} was removed."
        )
        return

    if action == "rate":
        await query.answer("Choose your personal rating.")
        await send_personal_callback_text(
            update,
            context,
            (
                f"⭐ <b>{escape(user_name)}’s rating</b>\n"
                f"<b>{escape(details.title)}</b> ({escape(details.year)})\n\n"
                "Choose a score from 1 to 10."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=rating_keyboard(details, user_id),
        )
        return

    if action == "progress":
        if not details.is_series:
            await query.answer("Episode progress is only available for series.", show_alert=True)
            return
        await query.answer("Opening your progress menu.")
        state, released_keys = await _series_state(update, context, details)
        await send_personal_callback_text(
            update,
            context,
            progress_summary_text(
                details,
                user_name=user_name,
                status=state.viewing_status,
                watched_count=state.watched_episodes,
                released_count=len(released_keys),
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=progress_menu_keyboard(details, user_id),
        )
        return

    await query.answer("That action is not available.")


async def _score(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rating: int,
    short_type: str,
    tmdb_id: int,
    owner_id: int,
) -> None:
    if not 1 <= rating <= 10:
        raise ValueError("invalid rating")
    if not await _personal_menu_owner(update, owner_id):
        return

    query = update.callback_query
    media_type = media_type_from_short(short_type)
    details = await tmdb(context).details(media_type, tmdb_id)
    user_id, user_name = user_identity(update)
    await database(context).rate(
        update.effective_chat.id, details, user_id, user_name, rating
    )
    average, count = await database(context).group_rating(
        update.effective_chat.id, media_type, tmdb_id
    )
    await query.answer(f"Your rating: {rating}/10")
    await edit_personal_text(
        context,
        query.message,
        owner_id,
        (
            f"⭐ <b>{escape(details.title)}</b>\n\n"
            f"Your rating: <b>{rating}/10</b>\n"
            f"Group rating: <b>{average:.1f}/10</b> ({count} "
            f"{'rating' if count == 1 else 'ratings'})"
        ),
        parse_mode=ParseMode.HTML,
    )


async def _progress_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]
) -> None:
    action = parts[1] if len(parts) > 1 else ""

    if action in {"open", "all", "watching", "clear"} and len(parts) == 4:
        tmdb_id = int(parts[2])
        owner_id = int(parts[3])
        if not await _personal_menu_owner(update, owner_id):
            return
        if action == "open":
            await _progress_open(update, context, tmdb_id, owner_id)
        elif action == "all":
            await _progress_mark_all(update, context, tmdb_id, owner_id)
        elif action == "watching":
            await _progress_set_watching(update, context, tmdb_id, owner_id)
        else:
            await _progress_clear(update, context, tmdb_id, owner_id)
        return

    if action == "seasons" and len(parts) == 5:
        tmdb_id = int(parts[2])
        owner_id = int(parts[3])
        page = int(parts[4])
        if not await _personal_menu_owner(update, owner_id):
            return
        await _progress_seasons(update, context, tmdb_id, owner_id, page)
        return

    if action in {"season", "sall", "sclr"} and len(parts) == 6:
        tmdb_id = int(parts[2])
        season_number = int(parts[3])
        owner_id = int(parts[4])
        page = int(parts[5])
        if not await _personal_menu_owner(update, owner_id):
            return
        if action == "season":
            await _progress_season(
                update, context, tmdb_id, season_number, owner_id, page
            )
        elif action == "sall":
            await _progress_season_all(
                update, context, tmdb_id, season_number, owner_id, page
            )
        else:
            await _progress_season_clear(
                update, context, tmdb_id, season_number, owner_id, page
            )
        return

    if action == "ep" and len(parts) == 7:
        tmdb_id = int(parts[2])
        season_number = int(parts[3])
        episode_number = int(parts[4])
        owner_id = int(parts[5])
        page = int(parts[6])
        if not await _personal_menu_owner(update, owner_id):
            return
        await _progress_episode(
            update,
            context,
            tmdb_id,
            season_number,
            episode_number,
            owner_id,
            page,
        )
        return

    await update.callback_query.answer("That progress button is no longer valid.")


async def _progress_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tmdb_id: int, owner_id: int
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    state, released_keys = await _series_state(update, context, details)
    _, user_name = user_identity(update)
    await query.answer()
    await _edit_personal_menu(
        context,
        query,
        owner_id,
        progress_summary_text(
            details,
            user_name=user_name,
            status=state.viewing_status,
            watched_count=state.watched_episodes,
            released_count=len(released_keys),
        ),
        progress_menu_keyboard(details, owner_id),
    )


async def _progress_seasons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmdb_id: int,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    seasons = [season for season in details.seasons if season.season_number > 0]
    page_count = max(1, (len(seasons) + SEASON_PAGE_SIZE - 1) // SEASON_PAGE_SIZE)
    if page < 0 or page >= page_count:
        await query.answer("That season page is no longer available.", show_alert=True)
        return
    counts = await database(context).season_progress_counts(
        update.effective_chat.id, owner_id, tmdb_id
    )
    await query.answer()
    await _edit_personal_menu(
        context,
        query,
        owner_id,
        f"📚 <b>{escape(details.title)} — choose a season</b>\n\n"
        "Each episode selection is personal to you.",
        season_list_keyboard(details, owner_id, page, counts),
    )


async def _progress_season(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmdb_id: int,
    season_number: int,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    season = await tmdb(context).season_details(tmdb_id, season_number)
    page_count = max(1, (len(season.episodes) + EPISODE_PAGE_SIZE - 1) // EPISODE_PAGE_SIZE)
    if page < 0 or page >= page_count:
        await query.answer("That episode page is no longer available.", show_alert=True)
        return
    watched = await database(context).watched_episode_keys(
        update.effective_chat.id, owner_id, tmdb_id
    )
    released = _released_keys_for_season(season)
    watched_in_season = len(
        [key for key in watched if key[0] == season_number]
    )
    released_in_season = len(released)
    await query.answer()
    await _edit_personal_menu(
        context,
        query,
        owner_id,
        (
            f"📺 <b>{escape(details.title)} — {escape(season.name)}</b>\n\n"
            f"Watched: {watched_in_season}/{released_in_season} released episodes\n"
            "Tap an episode to toggle it. 🕒 means it has not aired yet."
        ),
        episode_list_keyboard(
            details, season, owner_id, page, watched, released
        ),
    )


async def _progress_episode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmdb_id: int,
    season_number: int,
    episode_number: int,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    season = await tmdb(context).season_details(tmdb_id, season_number)
    episode = _find_episode(season, episode_number)
    if episode is None:
        await query.answer("That episode is unavailable.", show_alert=True)
        return
    key = (season_number, episode_number)
    if key not in _released_keys_for_season(season):
        await query.answer("That episode has not aired yet.", show_alert=True)
        return

    watched = await database(context).watched_episode_keys(
        update.effective_chat.id, owner_id, tmdb_id
    )
    enabled = key not in watched
    _, user_name = user_identity(update)
    await database(context).set_episode_watched(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        season_number,
        episode_number,
        enabled,
    )
    released_all = await _released_keys(context, details)
    await database(context).sync_series_progress(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        released_all,
    )
    await query.answer(
        f"S{season_number}E{episode_number} marked watched."
        if enabled
        else f"S{season_number}E{episode_number} marked unwatched."
    )
    await _progress_season_no_answer(
        update, context, details, season, owner_id, page
    )


async def _progress_mark_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tmdb_id: int, owner_id: int
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    released = await _released_keys(context, details)
    _, user_name = user_identity(update)
    await database(context).set_episode_keys(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        released,
        enabled=True,
    )
    await database(context).sync_series_progress(
        update.effective_chat.id, details, owner_id, user_name, released
    )
    await query.answer(
        "All released episodes marked watched."
        if details.series_status == "ongoing"
        else "All episodes marked watched."
    )
    await _progress_open_no_answer(update, context, details, owner_id)


async def _progress_set_watching(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tmdb_id: int, owner_id: int
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    _, user_name = user_identity(update)
    await database(context).set_series_status(
        update.effective_chat.id, details, owner_id, user_name, "watching"
    )
    await query.answer("Series status set to Watching.")
    await _progress_open_no_answer(update, context, details, owner_id)


async def _progress_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE, tmdb_id: int, owner_id: int
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    _, user_name = user_identity(update)
    await database(context).clear_series_progress(
        update.effective_chat.id, details, owner_id, user_name
    )
    await query.answer("Your series progress was cleared.")
    await _progress_open_no_answer(update, context, details, owner_id)


async def _progress_season_all(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmdb_id: int,
    season_number: int,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    season = await tmdb(context).season_details(tmdb_id, season_number)
    keys = _released_keys_for_season(season)
    _, user_name = user_identity(update)
    await database(context).set_episode_keys(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        keys,
        enabled=True,
    )
    released_all = await _released_keys(context, details)
    await database(context).sync_series_progress(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        released_all,
    )
    await query.answer(f"Season {season_number} marked watched.")
    await _progress_season_no_answer(
        update, context, details, season, owner_id, page
    )


async def _progress_season_clear(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmdb_id: int,
    season_number: int,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    details = await tmdb(context).details("tv", tmdb_id)
    season = await tmdb(context).season_details(tmdb_id, season_number)
    keys = {
        (episode.season_number, episode.episode_number)
        for episode in season.episodes
    }
    _, user_name = user_identity(update)
    await database(context).set_episode_keys(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        keys,
        enabled=False,
    )
    released_all = await _released_keys(context, details)
    await database(context).sync_series_progress(
        update.effective_chat.id,
        details,
        owner_id,
        user_name,
        released_all,
    )
    await query.answer(f"Season {season_number} progress cleared.")
    await _progress_season_no_answer(
        update, context, details, season, owner_id, page
    )


async def _progress_season_no_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details: MediaDetails,
    season: SeasonDetails,
    owner_id: int,
    page: int,
) -> None:
    query = update.callback_query
    watched = await database(context).watched_episode_keys(
        update.effective_chat.id, owner_id, details.tmdb_id
    )
    released = _released_keys_for_season(season)
    watched_in_season = len([key for key in watched if key[0] == season.season_number])
    await _edit_personal_menu(
        context,
        query,
        owner_id,
        (
            f"📺 <b>{escape(details.title)} — {escape(season.name)}</b>\n\n"
            f"Watched: {watched_in_season}/{len(released)} released episodes\n"
            "Tap an episode to toggle it. 🕒 means it has not aired yet."
        ),
        episode_list_keyboard(
            details, season, owner_id, page, watched, released
        ),
    )


async def _progress_open_no_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details: MediaDetails,
    owner_id: int,
) -> None:
    query = update.callback_query
    state, released_keys = await _series_state(update, context, details)
    _, user_name = user_identity(update)
    await _edit_personal_menu(
        context,
        query,
        owner_id,
        progress_summary_text(
            details,
            user_name=user_name,
            status=state.viewing_status,
            watched_count=state.watched_episodes,
            released_count=len(released_keys),
        ),
        progress_menu_keyboard(details, owner_id),
    )


async def _series_state(
    update: Update, context: ContextTypes.DEFAULT_TYPE, details: MediaDetails
):
    user_id, user_name = user_identity(update)
    state = await database(context).user_state(
        update.effective_chat.id, user_id, "tv", details.tmdb_id
    )
    released = await _released_keys(context, details)
    # Re-evaluate caught-up/completed state whenever the menu opens so a newly
    # released episode automatically moves an ongoing series back to Watching.
    if state.watched_episodes or state.viewing_status in {"caught_up", "completed"}:
        state = await database(context).sync_series_progress(
            update.effective_chat.id,
            details,
            user_id,
            user_name,
            released,
        )
    return state, released


async def _released_keys(
    context: ContextTypes.DEFAULT_TYPE, details: MediaDetails
) -> set[tuple[int, int]]:
    return await tmdb(context).released_episode_keys(
        details, today=date.today().isoformat()
    )


def _released_keys_for_season(season: SeasonDetails) -> set[tuple[int, int]]:
    today = date.today().isoformat()
    return {
        (episode.season_number, episode.episode_number)
        for episode in season.episodes
        if episode.air_date and episode.air_date <= today
    }


def _find_episode(
    season: SeasonDetails, episode_number: int
) -> EpisodeDetails | None:
    return next(
        (
            episode
            for episode in season.episodes
            if episode.episode_number == episode_number
        ),
        None,
    )


async def _personal_menu_owner(update: Update, owner_id: int) -> bool:
    user = update.effective_user
    if user and user.id == owner_id:
        return True
    await update.callback_query.answer(
        "This personal menu belongs to another member.", show_alert=True
    )
    return False


async def _close_personal_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
) -> None:
    if not await _personal_menu_owner(update, owner_id):
        return
    query = update.callback_query
    await query.answer()
    await delete_personal_message(
        context,
        query.message,
        owner_id,
    )


async def _edit_personal_menu(
    context: ContextTypes.DEFAULT_TYPE,
    query,
    owner_id: int,
    text: str,
    reply_markup,
) -> None:
    try:
        await edit_personal_text(
            context,
            query.message,
            owner_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
