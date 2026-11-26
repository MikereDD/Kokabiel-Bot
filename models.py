from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MediaType = Literal["movie", "tv"]
MediaClass = Literal[
    "film",
    "documentary_film",
    "tv_series",
    "limited_series",
    "documentary_series",
    "documentary_limited_series",
]
SeriesStatus = Literal["ongoing", "ended", "unknown"]
ViewingStatus = Literal["none", "watching", "watched", "caught_up", "completed"]


@dataclass(frozen=True, slots=True)
class SearchResult:
    media_type: MediaType
    tmdb_id: int
    title: str
    original_title: str
    year: str
    overview: str
    poster_path: str | None
    genre_ids: tuple[int, ...] = ()
    popularity: float = 0.0

    @property
    def short_type(self) -> str:
        return "m" if self.media_type == "movie" else "t"


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    date: str
    region: str
    release_type: int

    @property
    def label(self) -> str:
        return {
            1: "Premiere",
            2: "Limited theatrical",
            3: "Theatrical",
            4: "Digital",
            5: "Physical media",
            6: "Television",
        }.get(self.release_type, "Other")


@dataclass(frozen=True, slots=True)
class SeasonSummary:
    season_number: int
    name: str
    episode_count: int
    air_date: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodeDetails:
    season_number: int
    episode_number: int
    name: str
    air_date: str | None
    runtime: int | None = None


@dataclass(frozen=True, slots=True)
class SeasonDetails:
    tmdb_id: int
    season_number: int
    name: str
    episodes: tuple[EpisodeDetails, ...] = ()


@dataclass(frozen=True, slots=True)
class MediaDetails:
    media_type: MediaType
    tmdb_id: int
    title: str
    original_title: str
    year: str
    overview: str
    poster_url: str | None
    genres: tuple[str, ...] = ()
    runtime: str | None = None
    certification: str | None = None
    directors_or_creators: tuple[str, ...] = ()
    cast: tuple[str, ...] = ()
    vote_average: float = 0.0
    vote_count: int = 0
    imdb_id: str | None = None
    trailer_url: str | None = None
    tmdb_url: str = ""
    streaming: tuple[str, ...] = ()
    rent: tuple[str, ...] = ()
    buy: tuple[str, ...] = ()
    media_class: MediaClass = "film"
    is_documentary: bool = False
    series_status: SeriesStatus = "unknown"
    tmdb_status: str | None = None
    series_type: str | None = None
    first_air_date: str | None = None
    last_air_date: str | None = None
    number_of_seasons: int = 0
    number_of_episodes: int = 0
    networks: tuple[str, ...] = ()
    seasons: tuple[SeasonSummary, ...] = ()
    release_date: str | None = None
    release_region: str | None = None
    release_events: tuple[ReleaseEvent, ...] = ()
    next_episode: EpisodeDetails | None = None

    @property
    def lifecycle_label(self) -> str:
        if not self.is_series:
            return self.tmdb_status or "Unknown"

        status = (self.tmdb_status or "").strip().casefold()

        return {
            "returning series": "Continuing",
            "ended": "Ended",
            "canceled": "Canceled",
            "cancelled": "Canceled",
            "in production": "In production",
            "planned": "Planned",
            "pilot": "Pilot",
        }.get(status, "Unknown")

    @property
    def short_type(self) -> str:
        return "m" if self.media_type == "movie" else "t"

    @property
    def is_series(self) -> bool:
        return self.media_type == "tv"

    @property
    def type_label(self) -> str:
        labels = {
            "film": "Film",
            "documentary_film": "Documentary Film",
            "tv_series": "TV Series",
            "limited_series": "Limited Series",
            "documentary_series": "Documentary Series",
            "documentary_limited_series": "Documentary Limited Series",
        }
        return labels[self.media_class]

    @property
    def icon(self) -> str:
        if self.is_documentary:
            return "🎥"
        return "🎬" if self.media_type == "movie" else "📺"


@dataclass(frozen=True, slots=True)
class PersonalEntry:
    chat_id: int
    user_id: int
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str
    media_class: MediaClass
    poster_url: str | None
    watchlist: bool
    viewing_status: ViewingStatus
    updated_at: str
    watched_episodes: int = 0
    rating: int | None = None


@dataclass(frozen=True, slots=True)
class PersonalLibraryPage:
    entries: tuple[PersonalEntry, ...]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(
            1,
            (self.total + self.page_size - 1) // self.page_size,
        )

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.page_count


@dataclass(frozen=True, slots=True)
class GroupEntry:
    chat_id: int
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str
    media_class: MediaClass
    poster_url: str | None
    member_count: int
    member_names: tuple[str, ...] = field(default_factory=tuple)
    group_rating: float | None = None
    rating_count: int = 0


@dataclass(frozen=True, slots=True)
class SuggestionEntry:
    chat_id: int
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str
    media_class: MediaClass
    suggested_count: int
    suggested_by: tuple[str, ...] = field(default_factory=tuple)
    suggested_at: str = ""


@dataclass(frozen=True, slots=True)
class UserMediaState:
    watchlist: bool = False
    viewing_status: ViewingStatus = "none"
    watched_episodes: int = 0


@dataclass(frozen=True, slots=True)
class KnownChat:
    chat_id: int
    chat_type: str
    title: str
    username: str | None
    is_active: bool
    first_seen_at: str
    last_seen_at: str
