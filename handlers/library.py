from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import logging
import time
from html import escape
from secrets import token_hex

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from tmdb import TmdbError
from formatting import release_information_text

from handlers.common import (
    allowed,
    database,
    edit_personal_text,
    reply_personal_text,
    tmdb,
    user_identity,
)

from models import PersonalLibraryPage
from release_intelligence import (
    SCHEDULE_WINDOWS,
    personal_schedule_page,
)
from handlers.common import settings
from handlers.import_tasks import import_task_manager
from release_refresh import (
    RELEASE_REFRESH_ACTIVE_KEY,
    RELEASE_REFRESH_WORKER_KEY,
    ReleaseRefreshWorker,
)


LOGGER = logging.getLogger(__name__)


SESSION_KEY = "kokabiel_library_menus"
MAX_SESSIONS = 8

CATEGORIES = {
    "all": "All",
    "films": "Films",
    "series": "Series",
    "documentaries": "Documentaries",
}

COLLECTIONS = {
    "all": "Library",
    "watchlist": "Watchlist",
    "watched": "Watched",
    "progress": "Progress",
    "ratings": "Ratings",
}

SORTS = {
    "title": "Alphabetical",
    "updated": "Recently updated",
    "added": "Recently added",
    "year": "Release year",
    "rating": "Rating",
}

STATUSES = {
    "all": "Any status",
    "none": "Not started",
    "watching": "Watching",
    "watched": "Watched",
    "caught_up": "Caught up",
    "completed": "Completed",
}


@dataclass(frozen=True, slots=True)
class LibraryView:
    collection: str = "all"
    category: str = "all"
    sort: str = "title"
    status: str = "all"
    page: int = 0


@dataclass(frozen=True, slots=True)
class LibraryMenu:
    owner_id: int
    chat_id: int
    view: LibraryView
    result: PersonalLibraryPage
    message_key: tuple[str, int] | None = None
    selected_index: int | None = None
    schedule_days: int | None = None
    schedule_page: int = 0


def _message_key(message):
    if message is None:
        return None

    ephemeral_id = (
        getattr(message, "api_kwargs", None) or {}
    ).get("ephemeral_message_id")

    if isinstance(ephemeral_id, int):
        return ("ephemeral", ephemeral_id)

    message_id = getattr(message, "message_id", None)

    if isinstance(message_id, int) and message_id > 0:
        return ("message", message_id)

    return None


def _short(value: str, size: int) -> str:
    value = " ".join(value.split())

    if len(value) <= size:
        return value

    return value[:size - 1] + "…"


def _icon(entry) -> str:
    if entry.media_class.startswith("documentary"):
        return "🎥"

    return "🎬" if entry.media_type == "movie" else "📺"


def _button(label, token, action, value="-"):
    data = f"lib|{token}|{action}|{value}"

    if len(data.encode("utf-8")) > 64:
        raise ValueError("Library callback is too long")

    return InlineKeyboardButton(
        label,
        callback_data=data,
    )


def library_text(
    user_name, view, result, *, released_counts=None,
):
    lines = [
        "🎬 <b>Kokabiel — Personal Library</b>",
        f"<i>{escape(_short(user_name, 64))}</i>",
        "",
        (
            f"<b>{CATEGORIES[view.category]} · "
            f"{COLLECTIONS[view.collection]}</b>"
        ),
        f"📚 {result.total:,} titles",
        f"↕ {SORTS[view.sort]}",
        f"◉ {STATUSES[view.status]}",
        "",
        "━━━━━━━━━━━━━━━━━━",
    ]

    previous_category = None

    for index, entry in enumerate(result.entries, 1):
        if view.collection == "progress" and view.category == "all":
            category = (
                "Documentaries"
                if entry.media_class.startswith("documentary")
                else "Films"
                if entry.media_type == "movie"
                else "TV Series"
            )

            if category != previous_category:
                lines.append(f"<b>{category}</b>")
                previous_category = category

        title = escape(_short(entry.title, 90))
        year = escape(_short(entry.year, 12))

        lines.append(
            f"{index}. {_icon(entry)} "
            f"<b>{title}</b> ({year})"
        )

        details = []

        if entry.watchlist:
            details.append("🔖 Watchlist")

        if entry.viewing_status != "none":
            details.append(
                STATUSES.get(
                    entry.viewing_status,
                    entry.viewing_status,
                )
            )

        counts = (released_counts or {}).get(
            (entry.media_type, entry.tmdb_id)
        )

        if entry.media_type == "tv" and counts is not None:
            watched, released = counts

            details.append(
                f"{watched}/{released} released episodes watched"
            )

        elif entry.watched_episodes:
            details.append(
                f"{entry.watched_episodes} episodes watched"
            )

        if (
            entry.media_type == "tv"
            and released_counts is not None
            and counts is None
        ):
            details.append("Released total unavailable")

        if entry.rating is not None:
            details.append(f"⭐ {entry.rating}/10")

        if details:
            lines.append(
                "    " + escape(" · ".join(details))
            )

        lines.append("")

    if not result.entries:
        lines.append("No matching titles.")
        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        f"Page {result.page + 1} of {result.page_count}",
        "<i>Select a title or change your filters.</i>",
    ])

    return "\n".join(lines)


def library_keyboard(token, view, result):
    rows = []

    def button(label, action, value="-"):
        return _button(label, token, action, value)

    def selected(label, active):
        return f"✓ {label}" if active else label

    rows.append([
        button(
            selected("All", view.category == "all"),
            "category", "all",
        ),
        button(
            selected("Films", view.category == "films"),
            "category", "films",
        ),
    ])

    rows.append([
        button(
            selected("Series", view.category == "series"),
            "category", "series",
        ),
        button(
            selected(
                "Docs",
                view.category == "documentaries",
            ),
            "category", "documentaries",
        ),
    ])

    rows.append([
        button(
            selected("Library", view.collection == "all"),
            "collection", "all",
        ),
        button(
            selected(
                "Watchlist",
                view.collection == "watchlist",
            ),
            "collection", "watchlist",
        ),
    ])

    rows.append([
        button(
            selected(
                "Watched",
                view.collection == "watched",
            ),
            "collection", "watched",
        ),
        button(
            selected(
                "Progress",
                view.collection == "progress",
            ),
            "collection", "progress",
        ),
        button(
            selected(
                "Ratings",
                view.collection == "ratings",
            ),
            "collection", "ratings",
        ),
    ])

    rows.append([
        button(
            f"↕ {SORTS[view.sort]}",
            "sort",
        ),
        button(
            f"◉ {STATUSES[view.status]}",
            "status",
        ),
    ])

    for index, entry in enumerate(result.entries):
        rows.append([
            button(
                f"{index + 1}. {_icon(entry)} "
                f"{_short(entry.title, 46)}",
                "title",
                str(index),
            )
        ])

    navigation = []

    if result.has_previous:
        navigation.append(
            button(
                "◀ Previous",
                "page",
                str(result.page - 1),
            )
        )

    if result.has_next:
        navigation.append(
            button(
                "Next ▶",
                "page",
                str(result.page + 1),
            )
        )

    if navigation:
        rows.append(navigation)

    if (
        view.collection == "progress"
        and any(
            entry.media_type == "tv"
            for entry in result.entries
        )
    ):
        rows.append([
            button(
                "📊 Released episode totals",
                "episode_totals",
            )
        ])

    rows.append([
        button(
            "📅 Release Schedule",
            "schedule",
            "30",
        )
    ])

    rows.append([
        button("↻ Refresh", "refresh"),
        button("🏠 Reset", "reset"),
        button("✕ Close", "close"),
    ])

    return InlineKeyboardMarkup(rows)


def _save_menu(context, token, menu):
    sessions = context.user_data.setdefault(
        SESSION_KEY, {}
    )

    sessions[token] = menu

    while len(sessions) > MAX_SESSIONS:
        sessions.pop(next(iter(sessions)))


async def _load(update, context, view):
    user_id, _ = user_identity(update)

    result = await database(context).personal_library_page(
        update.effective_chat.id,
        user_id,
        collection=view.collection,
        category=view.category,
        sort=view.sort,
        status=view.status,
        page=view.page,
    )

    view = replace(view, page=result.page)

    return LibraryMenu(
        owner_id=user_id,
        chat_id=update.effective_chat.id,
        view=view,
        result=result,
    )



def schedule_text(user_name, region, result):
    lines = [
        "📅 <b>My Release Schedule</b>",
        f"<i>{escape(_short(user_name, 64))}</i>",
        "",
        (
            f"{result.start} — {result.end}"
            f" · {escape(region)}"
        ),
        "",
        f"<b>Tracked titles:</b> {result.tracked:,}",
        f"✓ Fresh: {result.fresh:,}",
        f"◷ Outdated: {result.stale:,}",
        f"? Not checked: {result.unchecked:,}",
        "",
        f"<b>Upcoming events:</b> {result.total:,}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    last_date = None

    for item in result.entries:
        event = item.event
        candidate = item.candidate

        if last_date != event.date:
            lines.append(
                f"<b>{event.date}</b>"
            )
            last_date = event.date

        title = escape(
            _short(candidate.title, 90)
        )

        lines.append(
            f"{_icon(candidate)} <b>{title}</b>"
        )

        label = event.label

        if event.season is not None:
            if event.episode is not None:
                label += (
                    f" · S{event.season:02d}"
                    f"E{event.episode:02d}"
                )
            else:
                label += f" · Season {event.season}"

        lines.append(
            escape(_short(label, 100))
        )

        if item.stale:
            lines.append(
                "<i>◷ Cached date — needs refresh</i>"
            )

        lines.append("")

    if not result.entries:
        if result.unchecked or result.stale:
            lines.append(
                "No upcoming events in the available "
                "cache. Coverage is incomplete."
            )
        elif result.tracked:
            lines.append(
                "No upcoming dates listed in this period."
            )
        else:
            lines.append(
                "Add titles to your watchlist or "
                "start tracking a series."
            )

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        f"Page {result.page + 1} of {result.page_count}",
        "",
        "<i>TMDB dates may change. "
        "Only listed upcoming events are shown.</i>",
    ])

    return "\n".join(lines)


def schedule_keyboard(token, days, result):
    rows = [[
        _button(
            f"{'✓ ' if days == window else ''}{window} days",
            token,
            "schedule",
            str(window),
        )
        for window in SCHEDULE_WINDOWS
    ]]

    navigation = []

    if result.page > 0:
        navigation.append(
            _button(
                "◀ Previous",
                token,
                "schedule_page",
                str(result.page - 1),
            )
        )

    if result.page + 1 < result.page_count:
        navigation.append(
            _button(
                "Next ▶",
                token,
                "schedule_page",
                str(result.page + 1),
            )
        )

    if navigation:
        rows.append(navigation)

    rows.append([
        _button(
            "↻ Refresh all tracked titles",
            token,
            "schedule_update_all",
        ),
    ])

    rows.append([
        _button(
            "↻ Refresh one batch",
            token,
            "schedule_update",
        ),
    ])

    rows.append([
        _button("↻ Reload schedule", token, "schedule_reload"),
    ])

    rows.append([
        _button("◀ Back to library", token, "schedule_back"),
        _button("✕ Close", token, "close"),
    ])

    return InlineKeyboardMarkup(rows)


async def _schedule_content(
    context,
    menu,
    user_name,
    token,
    *,
    days,
    page=0,
):
    region = settings(context).region

    candidates = await database(context).release_candidates(
        menu.owner_id,
        region=region,
    )

    result = personal_schedule_page(
        candidates,
        today=date.today(),
        now=int(time.time()),
        days=days,
        page=page,
    )

    menu = replace(
        menu,
        selected_index=None,
        schedule_days=days,
        schedule_page=result.page,
    )

    return (
        menu,
        schedule_text(user_name, region, result),
        schedule_keyboard(token, days, result),
    )


async def open_library(
    update,
    context,
    *,
    collection="all",
    schedule_days=None,
):
    if not await allowed(update, context):
        return

    _, user_name = user_identity(update)

    menu = await _load(
        update,
        context,
        LibraryView(collection=collection),
    )

    token = token_hex(12)

    if schedule_days is None:
        text = library_text(
            user_name, menu.view, menu.result,
        )
        markup = library_keyboard(
            token, menu.view, menu.result,
        )
    else:
        menu, text, markup = await _schedule_content(
            context,
            menu,
            user_name,
            token,
            days=schedule_days,
        )

    message = await reply_personal_text(
        update,
        context,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )

    message_key = _message_key(message)

    if message_key is not None:
        _save_menu(
            context,
            token,
            replace(menu, message_key=message_key),
        )


async def mylibrary(update, context):
    await open_library(update, context)


async def myreleases(update, context):
    await open_library(
        update, context, schedule_days=30,
    )


async def myratings(update, context):
    await open_library(
        update,
        context,
        collection="ratings",
    )


def _next(mapping, current):
    keys = tuple(mapping)
    return keys[(keys.index(current) + 1) % len(keys)]



def _refresh_worker(context):
    bot_data = context.application.bot_data
    client = tmdb(context)

    existing = bot_data.get(
        RELEASE_REFRESH_WORKER_KEY
    )

    if existing is None or existing[0] is not client:
        worker = ReleaseRefreshWorker(
            database(context),
            client,
        )

        existing = (client, worker)

        bot_data[
            RELEASE_REFRESH_WORKER_KEY
        ] = existing

    return existing[1]


def _refresh_summary(result):
    labels = {
        "busy": "Another refresh is active.",
        "paused": "Refresh is temporarily paused.",
        "rate_limited": "TMDB rate limit reached.",
        "authorization": "TMDB authorization failed.",
        "batch_limit": "Batch limit reached.",
        "time_budget": "Batch time budget reached.",
        "overall_time_budget": "Overall refresh time budget reached.",
        "overall_batch_limit": "Overall refresh batch limit reached.",
        "shutdown": "Refresh stopped for shutdown.",
        "remaining": "Some titles still need refreshing.",
    }

    lines = [
        "<b>Refresh result</b>",
        f"Checked: {result.attempted}",
        f"Updated: {result.updated}",
        f"Failed: {result.failed}",
    ]

    if result.skipped:
        lines.append(
            f"Skipped: {result.skipped}"
        )

    if result.stop_reason:
        lines.append(
            labels.get(
                result.stop_reason,
                "Refresh stopped.",
            )
        )

    if (
        not result.attempted
        and not result.stale
        and not result.stop_reason
    ):
        lines.append(
            "All eligible metadata is up to date."
        )

    return "\n".join(lines)


async def _finish_release_refresh(
    update,
    context,
    menu,
    user_name,
    worker,
    reservation,
    *,
    all_titles=False,
):
    application = context.application
    bot_data = application.bot_data
    query = update.callback_query
    user_id = menu.owner_id

    try:
        try:
            region = settings(context).region

            if all_titles:
                manager = import_task_manager(application)

                def should_continue():
                    return (
                        application.running
                        and manager.accepting
                        and bot_data.get(
                            RELEASE_REFRESH_ACTIVE_KEY
                        ) is reservation
                    )

                async def on_progress(batch, result):
                    if not should_continue():
                        return

                    text = (
                        "📅 <b>Refreshing release information</b>\n\n"
                        f"Batch: {batch}\n"
                        f"Tracked titles: {result.eligible}\n"
                        f"Checked: {result.attempted}\n"
                        f"Updated: {result.updated}\n"
                        f"Failed: {result.failed}\n\n"
                        "Your watch history and ratings are unchanged."
                    )

                    try:
                        await edit_personal_text(
                            context,
                            query.message,
                            user_id,
                            text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=None,
                        )
                    except Exception:
                        LOGGER.warning(
                            "Release refresh progress edit failed"
                        )

                result = await worker.refresh_all(
                    user_id,
                    region=region,
                    should_continue=should_continue,
                    on_progress=on_progress,
                )

            else:
                result = await worker.refresh(
                    user_id,
                    region=region,
                )

            summary = _refresh_summary(result)

            if all_titles:
                summary += (
                    f"\nRemaining stale or unchecked: {result.stale}"
                )

        except Exception:
            LOGGER.exception(
                "Release metadata refresh failed"
            )

            summary = (
                "<b>Refresh interrupted.</b> "
                "Previously stored metadata is retained; "
                "some titles may already have been updated."
            )

        if (
            bot_data.get(RELEASE_REFRESH_ACTIVE_KEY)
            is not reservation
        ):
            return

        token = token_hex(12)

        new_menu, text, markup = await _schedule_content(
            context,
            menu,
            user_name,
            token,
            days=menu.schedule_days or 30,
            page=0,
        )

        await edit_personal_text(
            context,
            query.message,
            user_id,
            summary + "\n\n" + text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

        _save_menu(context, token, new_menu)

    finally:
        if (
            bot_data.get(RELEASE_REFRESH_ACTIVE_KEY)
            is reservation
        ):
            bot_data.pop(
                RELEASE_REFRESH_ACTIVE_KEY,
                None,
            )


async def _start_release_refresh(
    update,
    context,
    menu,
    user_name,
    token,
    sessions,
    *,
    all_titles=False,
):
    query = update.callback_query
    application = context.application
    bot_data = application.bot_data

    if menu.schedule_days is None:
        await query.answer(
            "Open your release schedule first.",
            show_alert=True,
        )
        return

    manager = import_task_manager(application)

    if not application.running or not manager.accepting:
        await query.answer(
            "Kokabiel is shutting down.",
            show_alert=True,
        )
        return

    if manager.writer_count:
        await query.answer(
            "An import is being applied. Try again afterward.",
            show_alert=True,
        )
        return

    if bot_data.get(RELEASE_REFRESH_ACTIVE_KEY):
        await query.answer(
            "A release refresh is already running.",
            show_alert=True,
        )
        return

    # Reserve before the first await. Import admission
    # checks this same reservation.
    reservation = object()

    bot_data[
        RELEASE_REFRESH_ACTIVE_KEY
    ] = reservation

    submitted = False

    try:
        worker = _refresh_worker(context)

        await query.answer("Refreshing release information…")

        await edit_personal_text(
            context,
            query.message,
            menu.owner_id,
            (
                "📅 <b>Refreshing release information</b>\n\n"
                + (
                    "Checking tracked titles in multiple batches.\n"
                    if all_titles
                    else "Checking up to 20 titles against TMDB.\n"
                )
                + "Your watch history and ratings are unchanged.\n\n"
                + "The schedule will update when the refresh finishes."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

        sessions.pop(token, None)

        if not application.running or not manager.accepting:
            await edit_personal_text(
                context,
                query.message,
                menu.owner_id,
                "Kokabiel is shutting down. Refresh was not started. "
                "Reopen /myreleases after restart.",
                reply_markup=None,
            )
            return

        coroutine = _finish_release_refresh(
            update,
            context,
            menu,
            user_name,
            worker,
            reservation,
            all_titles=all_titles,
        )

        try:
            task = application.create_task(
                coroutine,
                update=update,
                name="kokabiel-release-refresh",
            )
        except BaseException:
            coroutine.close()
            raise

        def release_reservation(completed):
            if (
                bot_data.get(RELEASE_REFRESH_ACTIVE_KEY)
                is reservation
            ):
                bot_data.pop(
                    RELEASE_REFRESH_ACTIVE_KEY,
                    None,
                )

        task.add_done_callback(release_reservation)

        submitted = True

    finally:
        if (
            not submitted
            and bot_data.get(RELEASE_REFRESH_ACTIVE_KEY)
            is reservation
        ):
            bot_data.pop(
                RELEASE_REFRESH_ACTIVE_KEY,
                None,
            )


async def library_callback(update, context):
    query = update.callback_query
    data = (query.data or "").split("|")

    if len(data) != 4:
        await query.answer("Invalid library action.")
        return

    _, token, action, value = data

    sessions = context.user_data.get(
        SESSION_KEY, {}
    )

    menu = sessions.get(token)
    user_id, user_name = user_identity(update)

    if (
        menu is None
        or menu.owner_id != user_id
        or update.effective_chat is None
        or menu.chat_id != update.effective_chat.id
        or query.message is None
        or menu.message_key is None
        or menu.message_key != _message_key(query.message)
    ):
        await query.answer(
            "This library menu has expired or is not yours.",
            show_alert=True,
        )
        return

    if not await allowed(update, context):
        return

    if sessions.get(token) is not menu:
        await query.answer(
            "This library menu has expired.",
            show_alert=True,
        )
        return

    view = menu.view

    if action == "episode_totals":
        if (
            value != "-"
            or menu.schedule_days is not None
            or menu.selected_index is not None
            or view.collection != "progress"
        ):
            await query.answer(
                "Open your progress library first.",
                show_alert=True,
            )
            return

        entries = [
            entry
            for entry in menu.result.entries
            if entry.media_type == "tv"
        ]

        if not entries:
            await query.answer("No series on this page.")
            return

        await query.answer("Loading released episode totals…")

        # Reuse the original released-episode resolver.
        from handlers.commands import _progress_released_counts

        counts = await _progress_released_counts(
            context,
            entries,
        )

        if sessions.get(token) is not menu:
            return

        new_token = token_hex(12)

        await edit_personal_text(
            context,
            query.message,
            user_id,
            library_text(
                user_name,
                view,
                menu.result,
                released_counts=counts,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=library_keyboard(
                new_token,
                view,
                menu.result,
            ),
        )

        sessions.pop(token, None)
        _save_menu(context, new_token, menu)
        return

    if action in {"schedule_update", "schedule_update_all"}:
        if value != "-":
            await query.answer(
                "Invalid refresh action.",
                show_alert=True,
            )
            return

        await _start_release_refresh(
            update,
            context,
            menu,
            user_name,
            token,
            sessions,
            all_titles=(action == "schedule_update_all"),
        )
        return

    if action in {
        "schedule",
        "schedule_page",
        "schedule_reload",
    }:
        if menu.selected_index is not None:
            await query.answer("Return to the library first.")
            return

        days = menu.schedule_days or 30
        page = menu.schedule_page

        try:
            if action == "schedule":
                days = int(value)
                page = 0

                if days not in SCHEDULE_WINDOWS:
                    raise ValueError

            elif action == "schedule_page":
                if menu.schedule_days is None:
                    raise ValueError

                page = int(value)

                if page < 0:
                    raise ValueError

            elif menu.schedule_days is None:
                raise ValueError

        except ValueError:
            await query.answer("Invalid schedule selection.")
            return

        await query.answer()

        new_token = token_hex(12)

        new_menu, text, markup = await _schedule_content(
            context,
            menu,
            user_name,
            new_token,
            days=days,
            page=page,
        )

        if sessions.get(token) is not menu:
            return

        await edit_personal_text(
            context,
            query.message,
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

        sessions.pop(token, None)
        _save_menu(context, new_token, new_menu)
        return

    if menu.schedule_days is not None:
        if action == "schedule_back":
            action = "refresh"
        elif action != "close":
            await query.answer("Return to the library first.")
            return

    if action == "release":
        if menu.selected_index is None:
            await query.answer(
                "Select a library title first.",
                show_alert=True,
            )
            return

        entry = menu.result.entries[menu.selected_index]

        await query.answer("Loading release information…")

        try:
            details = await tmdb(context).details(
                entry.media_type,
                entry.tmdb_id,
            )

            text = release_information_text(details)

        except TmdbError:
            text = (
                "Release information is temporarily unavailable. "
                "Your personal library has not changed."
            )

        if sessions.get(token) is not menu:
            return

        new_token = token_hex(12)

        markup = InlineKeyboardMarkup([
            [
                _button(
                    "◀ Back to title",
                    new_token,
                    "detail",
                )
            ],
            [
                _button(
                    "◀ Back to library",
                    new_token,
                    "refresh",
                )
            ],
        ])

        await edit_personal_text(
            context,
            query.message,
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

        sessions.pop(token, None)
        _save_menu(context, new_token, menu)
        return

    if action == "media":
        if menu.selected_index is None:
            await query.answer(
                "Select a library title first.",
                show_alert=True,
            )
            return

        entry = menu.result.entries[
            menu.selected_index
        ]

        valid_actions = {"watchlist", "rate"}

        if entry.media_type == "tv":
            valid_actions.add("progress")
        else:
            valid_actions.add("watched")

        if value not in valid_actions:
            await query.answer(
                "That action is not available.",
                show_alert=True,
            )
            return

        # Reuse the existing media action implementation.
        # Import locally to avoid a module dependency cycle.
        from handlers.callbacks import _action

        short_type = (
            "m" if entry.media_type == "movie" else "t"
        )

        changes_state = value in {"watchlist", "watched"}

        if changes_state:
            # Invalidate before attempting the mutation.
            # A lost Telegram response must not permit replay.
            sessions.pop(token, None)

        try:
            await _action(
                update,
                context,
                value,
                short_type,
                entry.tmdb_id,
            )
        except TmdbError:
            await query.answer(
                "Media information is temporarily unavailable.",
                show_alert=True,
            )

            if changes_state:
                await edit_personal_text(
                    context,
                    query.message,
                    user_id,
                    "Media information is temporarily unavailable. "
                    "Reopen /mylibrary to continue.",
                    parse_mode=None,
                    reply_markup=None,
                )

            return

        if changes_state:
            # Reload using the original view, not the selected
            # index: a mutation may change collection membership.
            new_menu = replace(
                await _load(update, context, view),
                message_key=menu.message_key,
            )

            new_token = token_hex(12)

            await edit_personal_text(
                context,
                query.message,
                user_id,
                library_text(
                    user_name,
                    new_menu.view,
                    new_menu.result,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=library_keyboard(
                    new_token,
                    new_menu.view,
                    new_menu.result,
                ),
            )

            _save_menu(context, new_token, new_menu)

        return

    if (
        menu.selected_index is not None
        and action not in {"refresh", "close", "detail"}
    ):
        await query.answer(
            "Return to your library first.",
            show_alert=True,
        )
        return

    if action == "close":
        await query.answer()

        await edit_personal_text(
            context,
            query.message,
            user_id,
            "Library closed. Use /mylibrary to reopen.",
            parse_mode=None,
            reply_markup=None,
        )

        sessions.pop(token, None)
        return

    if action == "detail":
        if menu.selected_index is None:
            await query.answer("Select a title first.")
            return

        action = "title"
        value = str(menu.selected_index)

    if action == "title":
        try:
            index = int(value)

            if not 0 <= index < len(menu.result.entries):
                raise ValueError

            entry = menu.result.entries[index]

        except ValueError:
            await query.answer(
                "Invalid title selection.",
                show_alert=True,
            )
            return

        await query.answer()

        text = [
            f"{_icon(entry)} <b>{escape(_short(entry.title, 150))}</b>",
            f"Year: {escape(_short(entry.year, 12))}",
            "",
            (
                "<b>Personal status:</b> "
                + escape(
                    STATUSES.get(
                        entry.viewing_status,
                        "Unknown",
                    )
                )
            ),
            (
                "<b>Watchlist:</b> "
                + ("Yes" if entry.watchlist else "No")
            ),
        ]

        if entry.media_type == "tv":
            text.append(
                f"<b>Watched episodes:</b> "
                f"{entry.watched_episodes}"
            )

        if entry.rating is not None:
            text.append(
                f"<b>Your rating:</b> ⭐ {entry.rating}/10"
            )

        new_token = token_hex(12)

        first_action = (
            _button(
                "📺 Episode Progress",
                new_token,
                "media",
                "progress",
            )
            if entry.media_type == "tv"
            else _button(
                "✅ Toggle Watched",
                new_token,
                "media",
                "watched",
            )
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "View on TMDB",
                    url=(
                        "https://www.themoviedb.org/"
                        f"{entry.media_type}/{entry.tmdb_id}"
                    ),
                )
            ],
            [
                _button(
                    "📅 Release information",
                    new_token,
                    "release",
                ),
            ],
            [
                first_action,
                _button(
                    "🔖 Toggle Watchlist",
                    new_token,
                    "media",
                    "watchlist",
                ),
            ],
            [
                _button(
                    "⭐ Rate Title",
                    new_token,
                    "media",
                    "rate",
                ),
            ],
            [
                _button(
                    "◀ Back to library",
                    new_token,
                    "refresh",
                )
            ],
        ])

        await edit_personal_text(
            context,
            query.message,
            user_id,
            "\n".join(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

        sessions.pop(token, None)
        _save_menu(
            context,
            new_token,
            replace(menu, selected_index=index),
        )
        return

    if action == "category" and value in CATEGORIES:
        view = replace(
            view, category=value, page=0,
        )

    elif action == "collection" and value in COLLECTIONS:
        view = replace(
            view,
            collection=value,
            status="all",
            page=0,
        )

    elif action == "sort":
        view = replace(
            view,
            sort=_next(SORTS, view.sort),
            page=0,
        )

    elif action == "status":
        view = replace(
            view,
            status=_next(STATUSES, view.status),
            page=0,
        )

    elif action == "page":
        try:
            page = int(value)

            if not 0 <= page < menu.result.page_count:
                raise ValueError

        except ValueError:
            await query.answer("Invalid page.")
            return

        view = replace(view, page=page)

    elif action == "reset":
        view = LibraryView()

    elif action != "refresh":
        await query.answer("Invalid library action.")
        return

    await query.answer()

    new_menu = replace(
        await _load(update, context, view),
        message_key=menu.message_key,
    )
    new_token = token_hex(12)

    await edit_personal_text(
        context,
        query.message,
        user_id,
        library_text(
            user_name,
            new_menu.view,
            new_menu.result,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=library_keyboard(
            new_token,
            new_menu.view,
            new_menu.result,
        ),
    )

    sessions.pop(token, None)
    _save_menu(context, new_token, new_menu)
