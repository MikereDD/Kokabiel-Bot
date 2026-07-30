from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from credits import TMDB_ATTRIBUTION_URL, TMDB_URL
from models import MediaDetails, SearchResult, SeasonDetails

SEARCH_PAGE_SIZE = 6
SEASON_PAGE_SIZE = 6
EPISODE_PAGE_SIZE = 6


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


def search_results_keyboard(
    results: list[SearchResult],
    *,
    search_token: str,
    page: int = 0,
) -> InlineKeyboardMarkup:
    from formatting import selection_label

    start = page * SEARCH_PAGE_SIZE
    end = start + SEARCH_PAGE_SIZE
    visible_results = results[start:end]

    rows = [
        [
            InlineKeyboardButton(
                selection_label(item),
                callback_data=f"pick|{item.short_type}|{item.tmdb_id}",
            )
        ]
        for item in visible_results
    ]

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀ Previous",
                callback_data=f"page|{search_token}|{page - 1}",
            )
        )
    if end < len(results):
        navigation.append(
            InlineKeyboardButton(
                "More results ▶",
                callback_data=f"page|{search_token}|{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)

    return InlineKeyboardMarkup(rows)


def media_actions_keyboard(details: MediaDetails) -> InlineKeyboardMarkup:
    short = details.short_type
    tmdb_id = details.tmdb_id

    if details.is_series:
        first_action = InlineKeyboardButton(
            "📺 My Progress", callback_data=f"act|progress|{short}|{tmdb_id}"
        )
    else:
        first_action = InlineKeyboardButton(
            "✅ Mark watched", callback_data=f"act|watched|{short}|{tmdb_id}"
        )

    rows = [
        [
            first_action,
            InlineKeyboardButton(
                "➕ My Watchlist",
                callback_data=f"act|watchlist|{short}|{tmdb_id}",
            ),
        ],
        [
            InlineKeyboardButton("⭐ Rate", callback_data=f"act|rate|{short}|{tmdb_id}"),
            InlineKeyboardButton(
                "💡 Suggest to Group",
                callback_data=f"act|suggest|{short}|{tmdb_id}",
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


def rating_keyboard(details: MediaDetails, owner_id: int) -> InlineKeyboardMarkup:
    short = details.short_type
    tmdb_id = details.tmdb_id
    rows = []
    for start in (1, 6):
        rows.append(
            [
                InlineKeyboardButton(
                    str(rating),
                    callback_data=f"score|{rating}|{short}|{tmdb_id}|{owner_id}",
                )
                for rating in range(start, start + 5)
            ]
        )
    rows.append(
        [InlineKeyboardButton("Close", callback_data=f"menu|close|{owner_id}")]
    )
    return InlineKeyboardMarkup(rows)


def progress_menu_keyboard(details: MediaDetails, owner_id: int) -> InlineKeyboardMarkup:
    tmdb_id = details.tmdb_id
    all_label = (
        "✅ Mark all released watched"
        if details.series_status == "ongoing"
        else "🏁 Mark all episodes watched"
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📚 Choose a season",
                    callback_data=f"prog|seasons|{tmdb_id}|{owner_id}|0",
                )
            ],
            [
                InlineKeyboardButton(
                    all_label,
                    callback_data=f"prog|all|{tmdb_id}|{owner_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "▶ Set as watching",
                    callback_data=f"prog|watching|{tmdb_id}|{owner_id}",
                ),
                InlineKeyboardButton(
                    "🧹 Clear my progress",
                    callback_data=f"prog|clear|{tmdb_id}|{owner_id}",
                ),
            ],
            [InlineKeyboardButton("Close", callback_data=f"menu|close|{owner_id}")],
        ]
    )


def season_list_keyboard(
    details: MediaDetails,
    owner_id: int,
    page: int,
    watched_counts: dict[int, int],
) -> InlineKeyboardMarkup:
    seasons = [season for season in details.seasons if season.season_number > 0]
    start = page * SEASON_PAGE_SIZE
    end = start + SEASON_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []

    for season in seasons[start:end]:
        watched = watched_counts.get(season.season_number, 0)
        label = f"Season {season.season_number} — {watched}/{season.episode_count} watched"
        rows.append(
            [
                InlineKeyboardButton(
                    _truncate(label, 60),
                    callback_data=(
                        f"prog|season|{details.tmdb_id}|{season.season_number}|"
                        f"{owner_id}|0"
                    ),
                )
            ]
        )

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀ Previous",
                callback_data=f"prog|seasons|{details.tmdb_id}|{owner_id}|{page - 1}",
            )
        )
    if end < len(seasons):
        navigation.append(
            InlineKeyboardButton(
                "More seasons ▶",
                callback_data=f"prog|seasons|{details.tmdb_id}|{owner_id}|{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                "↩ Progress menu",
                callback_data=f"prog|open|{details.tmdb_id}|{owner_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def episode_list_keyboard(
    details: MediaDetails,
    season: SeasonDetails,
    owner_id: int,
    page: int,
    watched_keys: set[tuple[int, int]],
    released_keys: set[tuple[int, int]],
) -> InlineKeyboardMarkup:
    start = page * EPISODE_PAGE_SIZE
    end = start + EPISODE_PAGE_SIZE
    visible = season.episodes[start:end]
    rows: list[list[InlineKeyboardButton]] = []

    for episode in visible:
        key = (episode.season_number, episode.episode_number)
        if key in watched_keys:
            icon = "☑"
            callback = (
                f"prog|ep|{details.tmdb_id}|{episode.season_number}|"
                f"{episode.episode_number}|{owner_id}|{page}"
            )
        elif key in released_keys:
            icon = "☐"
            callback = (
                f"prog|ep|{details.tmdb_id}|{episode.season_number}|"
                f"{episode.episode_number}|{owner_id}|{page}"
            )
        else:
            icon = "🕒"
            callback = "noop|future"
        label = f"{icon} E{episode.episode_number}: {episode.name}"
        rows.append([InlineKeyboardButton(_truncate(label, 60), callback_data=callback)])

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀ Previous",
                callback_data=(
                    f"prog|season|{details.tmdb_id}|{season.season_number}|"
                    f"{owner_id}|{page - 1}"
                ),
            )
        )
    if end < len(season.episodes):
        navigation.append(
            InlineKeyboardButton(
                "More episodes ▶",
                callback_data=(
                    f"prog|season|{details.tmdb_id}|{season.season_number}|"
                    f"{owner_id}|{page + 1}"
                ),
            )
        )
    if navigation:
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "✅ Mark released season watched",
                callback_data=(
                    f"prog|sall|{details.tmdb_id}|{season.season_number}|"
                    f"{owner_id}|{page}"
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🧹 Clear season",
                callback_data=(
                    f"prog|sclr|{details.tmdb_id}|{season.season_number}|"
                    f"{owner_id}|{page}"
                ),
            ),
            InlineKeyboardButton(
                "↩ Seasons",
                callback_data=f"prog|seasons|{details.tmdb_id}|{owner_id}|0",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def owner_confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Cancel", callback_data=f"own|no|{token}"),
                InlineKeyboardButton("Confirm", callback_data=f"own|ok|{token}"),
            ]
        ]
    )


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
