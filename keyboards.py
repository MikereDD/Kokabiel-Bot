from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from credits import TMDB_ATTRIBUTION_URL, TMDB_URL
from models import MediaDetails, SearchResult


def about_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Visit TMDB", url=TMDB_URL),
                InlineKeyboardButton(
                    "Logo & attribution",
                    url=TMDB_ATTRIBUTION_URL,
                ),
            ]
        ]
    )


def search_results_keyboard(results: list[SearchResult]) -> InlineKeyboardMarkup:
    from formatting import selection_label

    rows = [
        [
            InlineKeyboardButton(
                selection_label(item),
                callback_data=f"pick|{item.short_type}|{item.tmdb_id}",
            )
        ]
        for item in results
    ]
    return InlineKeyboardMarkup(rows)


def media_actions_keyboard(details: MediaDetails) -> InlineKeyboardMarkup:
    short = details.short_type
    tmdb_id = details.tmdb_id
    rows = [
        [
            InlineKeyboardButton(
                "✅ Mark watched", callback_data=f"act|watched|{short}|{tmdb_id}"
            ),
            InlineKeyboardButton(
                "➕ Watchlist", callback_data=f"act|watchlist|{short}|{tmdb_id}"
            ),
        ],
        [
            InlineKeyboardButton("⭐ Rate", callback_data=f"act|rate|{short}|{tmdb_id}"),
            InlineKeyboardButton(
                "💬 Recommend", callback_data=f"act|recommend|{short}|{tmdb_id}"
            ),
        ],
    ]

    links: list[InlineKeyboardButton] = []
    if details.trailer_url:
        links.append(InlineKeyboardButton("▶️ Trailer", url=details.trailer_url))
    if details.imdb_id:
        links.append(
            InlineKeyboardButton("IMDb", url=f"https://www.imdb.com/title/{details.imdb_id}/")
        )
    links.append(InlineKeyboardButton("TMDB", url=details.tmdb_url))
    rows.append(links)
    return InlineKeyboardMarkup(rows)


def rating_keyboard(details: MediaDetails) -> InlineKeyboardMarkup:
    short = details.short_type
    tmdb_id = details.tmdb_id
    rows = []
    for start in (1, 6):
        rows.append(
            [
                InlineKeyboardButton(
                    str(rating), callback_data=f"score|{rating}|{short}|{tmdb_id}"
                )
                for rating in range(start, start + 5)
            ]
        )
    rows.append(
        [InlineKeyboardButton("↩ Back", callback_data=f"act|back|{short}|{tmdb_id}")]
    )
    return InlineKeyboardMarkup(rows)
