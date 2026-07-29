from __future__ import annotations

from html import escape

from models import CatalogEntry, MediaDetails, SearchResult

TELEGRAM_PHOTO_CAPTION_LIMIT = 1024


def display_type(media_type: str) -> str:
    return "Film" if media_type == "movie" else "TV Series"


def selection_label(result: SearchResult) -> str:
    icon = "🎬" if result.media_type == "movie" else "📺"
    title = result.title.strip() or "Untitled"
    label = f"{icon} {title} — {result.year}"
    if result.original_title and result.original_title.casefold() != title.casefold():
        label += f" ({result.original_title})"
    return _truncate_plain(label, 60)


def media_caption(
    details: MediaDetails,
    *,
    group_rating: float | None = None,
    rating_count: int = 0,
) -> str:
    icon = "🎬" if details.media_type == "movie" else "📺"
    title = escape(details.title)
    heading = f"{icon} <b>{title}</b> ({escape(details.year)})"

    metadata = [display_type(details.media_type)]
    if details.runtime:
        metadata.append(details.runtime)
    if details.certification:
        metadata.append(details.certification)

    lines = [heading, " · ".join(escape(item) for item in metadata)]

    if details.original_title.casefold() != details.title.casefold():
        lines.append(f"<b>Original title:</b> {escape(details.original_title)}")
    if details.genres:
        lines.append(f"<b>Genres:</b> {escape(', '.join(details.genres[:5]))}")

    if details.vote_count:
        lines.append(
            f"<b>TMDB:</b> {details.vote_average:.1f}/10 "
            f"({details.vote_count:,} votes)"
        )
    if group_rating is not None and rating_count:
        noun = "rating" if rating_count == 1 else "ratings"
        lines.append(f"<b>Our group:</b> {group_rating:.1f}/10 ({rating_count} {noun})")

    lead_label = "Director" if details.media_type == "movie" else "Created by"
    if details.directors_or_creators:
        lines.append(
            f"<b>{lead_label}:</b> {escape(', '.join(details.directors_or_creators))}"
        )
    if details.cast:
        lines.append(f"<b>Cast:</b> {escape(', '.join(details.cast))}")
    if details.streaming:
        lines.append(f"<b>Stream:</b> {escape(', '.join(details.streaming))}")

    overview = escape(details.overview.strip() or "No synopsis is currently available.")
    prefix = "\n".join(lines) + "\n\n"
    suffix = "\n\n<i>Media data supplied by TMDB.</i>"
    room = TELEGRAM_PHOTO_CAPTION_LIMIT - len(prefix) - len(suffix)
    if room < 40:
        overview = ""
    elif len(overview) > room:
        overview = overview[: max(0, room - 1)].rstrip() + "…"
    return prefix + overview + suffix


def catalog_text(title: str, entries: list[CatalogEntry]) -> str:
    if not entries:
        return f"<b>{escape(title)}</b>\n\nNothing has been added yet."

    lines = [f"<b>{escape(title)}</b>", ""]
    for index, entry in enumerate(entries[:30], start=1):
        kind = "Film" if entry.media_type == "movie" else "TV"
        extra = ""
        if entry.group_rating is not None:
            extra = f" · ⭐ {entry.group_rating:.1f}/10"
        lines.append(
            f"{index}. <b>{escape(entry.title)}</b> ({escape(entry.year)}) "
            f"· {kind}{extra}"
        )
    if len(entries) > 30:
        lines.append(f"\n…and {len(entries) - 30} more.")
    return "\n".join(lines)


def random_pick_text(entry: CatalogEntry) -> str:
    kind = "Film" if entry.media_type == "movie" else "TV Series"
    return (
        "🎲 <b>Random watchlist pick</b>\n\n"
        f"<b>{escape(entry.title)}</b> ({escape(entry.year)})\n"
        f"{kind}"
    )


def _truncate_plain(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
