from __future__ import annotations

from html import escape
from datetime import date

from models import (GroupEntry, MediaDetails, PersonalEntry, SearchResult,
                    SuggestionEntry)

TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_SOFT_LIMIT = 3900


def selection_label(result: SearchResult) -> str:
    icon = "🎬" if result.media_type == "movie" else "📺"
    title = result.title.strip() or "Untitled"
    label = f"{icon} {title} — {result.year}"
    if result.original_title and result.original_title.casefold() != title.casefold():
        label += f" ({result.original_title})"
    return _truncate_plain(label, 60)



def _release_date(value):
    if not isinstance(value, str):
        return None

    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.isoformat() == value else None


def _display_date(value):
    return f"{value:%b} {value.day}, {value.year}"


def _fit_escaped(value: str, budget: int) -> str:
    """Fit escaped text without splitting an HTML entity."""

    if budget <= 0:
        return ""

    encoded = escape(value)

    if len(encoded) <= budget:
        return encoded

    low, high = 0, len(value)

    while low < high:
        middle = (low + high + 1) // 2

        if len(escape(value[:middle])) + 1 <= budget:
            low = middle
        else:
            high = middle - 1

    return escape(value[:low].rstrip()) + "…"


def release_information_lines(
    details: MediaDetails,
    *,
    today: date | None = None,
    compact: bool = False,
) -> list[str]:

    today = today or date.today()
    lines = []

    if details.media_type == "movie":
        original = _release_date(details.release_date)

        lines.append(
            "<b>Release date:</b> "
            + (
                _display_date(original)
                if original
                else "Not listed"
            )
        )

        region = _fit_escaped(
            details.release_region or "Region",
            12,
        )

        kinds = (2, 3, 4, 5) if compact else range(1, 7)

        labels = {
            1: "Premiere",
            2: "Limited theatrical",
            3: "Theatrical",
            4: "Digital",
            5: "Physical media",
            6: "Television",
        }

        for kind in kinds:
            dates = sorted({
                parsed
                for event in details.release_events
                if event.release_type == kind
                and event.region == details.release_region
                and (parsed := _release_date(event.date))
            })

            if not dates:
                continue

            upcoming = [
                value for value in dates
                if value >= today
            ]

            selected = upcoming[0] if upcoming else dates[0]

            lines.append(
                f"<b>{region} {labels[kind]}:</b> "
                f"{_display_date(selected)}"
            )

        return lines

    lines.append(
        "<b>Series status:</b> "
        + escape(details.lifecycle_label)
    )

    premiere = _release_date(details.first_air_date)

    lines.append(
        "<b>Premiere:</b> "
        + (
            _display_date(premiere)
            if premiere
            else "Not listed"
        )
    )

    if not compact:
        last = _release_date(details.last_air_date)

        if last:
            lines.append(
                "<b>Last listed air date:</b> "
                + _display_date(last)
            )

    if details.lifecycle_label in {"Ended", "Canceled"}:
        return lines

    episode = details.next_episode

    if episode is not None:
        air_date = _release_date(episode.air_date)

        if air_date is None or air_date >= today:
            coordinate = (
                f"S{episode.season_number:02d}"
                f"E{episode.episode_number:02d}"
            )

            lines.append(
                f"<b>Next episode:</b> {coordinate}"
            )

            if not compact:
                lines.append(
                    _fit_escaped(episode.name, 160)
                )

            lines.append(
                "<b>Air date:</b> "
                + (
                    _display_date(air_date)
                    if air_date
                    else "Not announced"
                )
            )

            if episode.runtime and not compact:
                lines.append(
                    f"<b>Runtime:</b> {episode.runtime} min"
                )

    upcoming_seasons = sorted(
        (
            air_date,
            season.season_number,
        )
        for season in details.seasons
        if season.season_number > 0
        and (air_date := _release_date(season.air_date))
        and air_date >= today
    )

    if upcoming_seasons:
        air_date, number = upcoming_seasons[0]

        lines.append(
            f"<b>Season {number} premiere:</b> "
            f"{_display_date(air_date)}"
        )

    if (
        not upcoming_seasons
        and (
            episode is None
            or (
                _release_date(episode.air_date)
                and _release_date(episode.air_date) < today
            )
        )
    ):
        lines.append(
            "<i>No upcoming air date listed.</i>"
        )

    return lines


def release_information_text(
    details: MediaDetails,
    *,
    today: date | None = None,
) -> str:

    lines = [
        f"{details.icon} <b>"
        f"{_fit_escaped(details.title, 240)}</b>",
        escape(details.type_label),
        "",
        "<b>📅 Release information</b>",
        "",
        *release_information_lines(
            details,
            today=today,
        ),
        "",
    ]

    if details.media_type == "movie":
        lines.append(
            "<i>Regional dates show the next listed release "
            "or the first listed release if none is upcoming.</i>"
        )

    lines.append(
        "<i>Dates supplied by TMDB and subject to change.</i>"
    )

    return "\n".join(lines)


def media_caption(
    details: MediaDetails,
    *,
    group_rating: float | None = None,
    rating_count: int = 0,
) -> str:
    heading = (
        f"{details.icon} <b>{_fit_escaped(details.title, 320)}</b> " f"({_display_years(details)})"
    )

    metadata = [details.type_label]
    if details.media_type == "movie" and details.runtime:
        metadata.append(details.runtime)
    if details.certification:
        metadata.append(details.certification)
    lines = [
        heading,
        _fit_escaped(" · ".join(metadata), 160),
    ]

    lines.extend(
        release_information_lines(
            details,
            compact=True,
        )
    )

    if details.original_title.casefold() != details.title.casefold():
        lines.append(f"<b>Original title:</b> {escape(details.original_title)}")

    if details.media_type == "tv":
        if details.number_of_seasons:
            noun = "season" if details.number_of_seasons == 1 else "seasons"
            lines.append(f"<b>Seasons:</b> {details.number_of_seasons} {noun}")
        if details.number_of_episodes:
            noun = "episode" if details.number_of_episodes == 1 else "episodes"
            lines.append(f"<b>Episodes:</b> {details.number_of_episodes} {noun}")
        if details.runtime:
            lines.append(f"<b>Episode length:</b> {escape(details.runtime)}")
        if details.networks:
            lines.append(
                f"<b>Network/Platform:</b> {escape(', '.join(details.networks))}"
            )

    if details.genres:
        lines.append(f"<b>Genres:</b> {escape(', '.join(details.genres[:5]))}")

    if details.vote_count:
        lines.append(
            f"<b>TMDB:</b> {details.vote_average:.1f}/10 "
            f"({details.vote_count:,} votes)"
        )
    if group_rating is not None and rating_count:
        noun = "rating" if rating_count == 1 else "ratings"
        lines.append(
            f"<b>Group rating:</b> {group_rating:.1f}/10 ({rating_count} {noun})"
        )

    lead_label = "Director" if details.media_type == "movie" else "Created by"
    if details.directors_or_creators:
        lines.append(
            f"<b>{lead_label}:</b> {escape(', '.join(details.directors_or_creators))}"
        )
    if details.cast:
        cast_label = "Featured" if details.is_documentary else "Cast"
        lines.append(f"<b>{cast_label}:</b> {escape(', '.join(details.cast))}")
    if details.streaming:
        lines.append(f"<b>Stream:</b> {escape(', '.join(details.streaming))}")

    overview = details.overview.strip() or "No synopsis is currently available."
    provider_attribution = bool(details.streaming or details.rent or details.buy)
    suffix = "\n\n<i>Media data supplied by TMDB."
    if provider_attribution:
        suffix += " Streaming availability powered by JustWatch."
    suffix += "</i>"
    retained = []

    for line in lines:
        candidate = "\n".join([*retained, line]) + "\n\n"

        if len(candidate) + len(suffix) <= TELEGRAM_PHOTO_CAPTION_LIMIT:
            retained.append(line)

    prefix = "\n".join(retained) + "\n\n"
    room = TELEGRAM_PHOTO_CAPTION_LIMIT - len(prefix) - len(suffix)

    overview = (
        _fit_escaped(overview, room)
        if room >= 40
        else ""
    )

    return prefix + overview + suffix


def group_watchlist_text(entries: list[GroupEntry]) -> str:
    if not entries:
        return "<b>Group watchlist</b>\n\nNo members have added anything yet."

    lines = ["<b>Group watchlist</b>", ""]
    for index, entry in enumerate(entries[:30], start=1):
        member_word = "member" if entry.member_count == 1 else "members"
        rating = (
            f" · ⭐ {entry.group_rating:.1f}/10"
            if entry.group_rating is not None
            else ""
        )
        lines.append(
            f"{index}. <b>{escape(entry.title)}</b> ({escape(entry.year)})\n"
            f"   {escape(_media_class_label(entry.media_class))} · "
            f"{entry.member_count} {member_word}{rating}"
        )
    if len(entries) > 30:
        lines.append(f"\n…and {len(entries) - 30} more.")
    return "\n".join(lines)


def personal_list_text(title: str, entries: list[PersonalEntry]) -> str:
    if not entries:
        return f"<b>{escape(title)}</b>\n\nNothing here yet."

    lines = [f"<b>{escape(title)}</b>", ""]
    for index, entry in enumerate(entries[:30], start=1):
        status = _status_label(entry.viewing_status)
        extra = ""
        if entry.media_type == "tv" and entry.watched_episodes:
            extra = f" · {entry.watched_episodes} episodes watched"
        lines.append(
            f"{index}. <b>{escape(entry.title)}</b> ({escape(entry.year)})\n"
            f"   {escape(_media_class_label(entry.media_class))}"
            f"{(' · ' + status) if status else ''}{extra}"
        )
    if len(entries) > 30:
        lines.append(f"\n…and {len(entries) - 30} more.")
    return "\n".join(lines)


def suggestions_text(
    title: str, entries: list[SuggestionEntry], *, personal: bool = False
) -> str:
    if not entries:
        return f"<b>{escape(title)}</b>\n\nNo suggestions yet."

    lines = [f"<b>{escape(title)}</b>", ""]
    for index, entry in enumerate(entries[:30], start=1):
        if personal:
            detail = _media_class_label(entry.media_class)
        else:
            member_word = "member" if entry.suggested_count == 1 else "members"
            detail = f"Suggested by {entry.suggested_count} {member_word}"
            if entry.suggested_by:
                names = ", ".join(entry.suggested_by[:3])
                if len(entry.suggested_by) > 3:
                    names += f" and {len(entry.suggested_by) - 3} more"
                detail += f" · {names}"
        lines.append(
            f"{index}. <b>{escape(entry.title)}</b> ({escape(entry.year)})\n"
            f"   {escape(detail)}"
        )
    if len(entries) > 30:
        lines.append(f"\n…and {len(entries) - 30} more.")
    return "\n".join(lines)


def random_pick_text(entry: GroupEntry) -> str:
    return (
        "🎲 <b>Random group-watchlist pick</b>\n\n"
        f"<b>{escape(entry.title)}</b> ({escape(entry.year)})\n"
        f"{escape(_media_class_label(entry.media_class))} · "
        f"saved by {entry.member_count} "
        f"{'member' if entry.member_count == 1 else 'members'}"
    )


def progress_summary_text(
    details: MediaDetails,
    *,
    user_name: str,
    status: str,
    watched_count: int,
    released_count: int,
) -> str:
    status_label = _status_label(status) or "Not started"
    lines = [
        f"📺 <b>{escape(user_name)}’s progress</b>",
        f"<b>{escape(details.title)}</b> ({escape(details.year)})",
        "",
        f"<b>Status:</b> {escape(status_label)}",
        f"<b>Episodes watched:</b> {watched_count}/{released_count}",
    ]
    if details.series_status == "ongoing":
        lines.append("Watching every released episode means <b>Caught up</b>.")
    elif details.series_status == "ended":
        lines.append("Watching every episode means <b>Completed</b>.")
    return "\n".join(lines)


def _display_years(details: MediaDetails) -> str:
    if details.media_type == "movie":
        return escape(details.year)
    start = (details.first_air_date or "")[:4] or details.year
    end = (details.last_air_date or "")[:4]
    if details.series_status == "ongoing":
        return escape(f"{start}–")
    if end and end != start:
        return escape(f"{start}–{end}")
    return escape(start)


def _media_class_label(media_class: str) -> str:
    return {
        "film": "Film",
        "documentary_film": "Documentary Film",
        "tv_series": "TV Series",
        "limited_series": "Limited Series",
        "documentary_series": "Documentary Series",
        "documentary_limited_series": "Documentary Limited Series",
    }.get(media_class, "Media")


def _status_label(status: str) -> str:
    return {
        "watching": "▶ Watching",
        "watched": "✅ Watched",
        "caught_up": "✅ Caught up",
        "completed": "🏁 Completed",
    }.get(status, "")


def _truncate_plain(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def personal_progress_text(
    user_name: str,
    entries: list[PersonalEntry],
    released_counts: dict[tuple[str, int], tuple[int, int]] | None = None,
) -> str:
    """Render personal viewing history as a grouped media dashboard."""

    if not entries:
        return (
            f"🎞 <b>{escape(user_name)}’s media progress</b>\n\n"
            "Nothing watched or in progress yet."
        )

    released_counts = released_counts or {}

    sections = (
        (
            "🎬 <b>FILMS</b>",
            [
                entry
                for entry in entries
                if entry.media_class == "film"
            ],
        ),
        (
            "📺 <b>TV SERIES</b>",
            [
                entry
                for entry in entries
                if entry.media_class in {"tv_series", "limited_series"}
            ],
        ),
        (
            "🎥 <b>DOCUMENTARIES</b>",
            [
                entry
                for entry in entries
                if entry.media_class
                in {
                    "documentary_film",
                    "documentary_series",
                    "documentary_limited_series",
                }
            ],
        ),
    )

    lines = [f"🎞 <b>{escape(user_name)}’s media progress</b>"]

    status_order = {
        "watching": 0,
        "caught_up": 1,
        "completed": 2,
        "watched": 2,
        "none": 3,
    }

    shown = 0
    truncated = False

    for heading, section_entries in sections:
        if not section_entries:
            continue

        section_entries.sort(
            key=lambda entry: (
                status_order.get(entry.viewing_status, 99),
                entry.title.casefold(),
                entry.year,
            )
        )

        section_started = False

        for entry in section_entries:
            if shown >= 30:
                truncated = True
                break

            icon, status_name = _progress_status(entry.viewing_status)

            block = [
                f"{icon} <b>{escape(entry.title)}</b> ({escape(entry.year)})"
            ]

            if entry.media_type == "tv":
                key = (entry.media_type, entry.tmdb_id)
                released_progress = released_counts.get(key)

                if (
                    released_progress is not None
                    and released_progress[1] > 0
                ):
                    watched_released, released = released_progress
                    block.append(
                        f"   {_progress_bar(watched_released, released)} "
                        f"{watched_released}/{released} released"
                    )
                else:
                    noun = (
                        "episode"
                        if entry.watched_episodes == 1
                        else "episodes"
                    )
                    block.append(
                        f"   {entry.watched_episodes} {noun} watched"
                    )

            block.append(
                f"   {escape(_media_class_label(entry.media_class))}"
                f" · {escape(status_name)}"
            )

            if section_started:
                addition = ["", *block]
            else:
                addition = [
                    "",
                    heading,
                    "━━━━━━━━━━━━━━━━━━",
                    *block,
                ]

            candidate = "\n".join([*lines, *addition])

            # Leave room for the final “…and N more” notice.
            if len(candidate) > TELEGRAM_MESSAGE_SOFT_LIMIT - 64:
                truncated = True
                break

            lines.extend(addition)
            section_started = True
            shown += 1

        if truncated:
            break

    remaining = len(entries) - shown
    if remaining > 0:
        notice = f"\n…and {remaining} more."
        candidate = "\n".join(lines) + notice
        if len(candidate) <= TELEGRAM_MESSAGE_SOFT_LIMIT:
            lines.append(f"\n…and {remaining} more.")

    return "\n".join(lines)

def _progress_status(status: str) -> tuple[str, str]:
    return {
        "watching": ("▶", "Watching"),
        "watched": ("✅", "Watched"),
        "caught_up": ("✅", "Caught up"),
        "completed": ("🏁", "Completed"),
    }.get(status, ("•", "Not started"))


def _progress_bar(watched: int, released: int, width: int = 10) -> str:
    if released <= 0:
        return "░" * width

    ratio = min(1.0, max(0.0, watched / released))
    filled = round(ratio * width)

    if watched > 0 and filled == 0:
        filled = 1

    return ("█" * filled) + ("░" * (width - filled))
