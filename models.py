from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MediaType = Literal["movie", "tv"]


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

    @property
    def short_type(self) -> str:
        return "m" if self.media_type == "movie" else "t"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    chat_id: int
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str
    poster_url: str | None
    added_by: int
    added_by_name: str
    added_at: str
    watchlist: bool = False
    watched: bool = False
    recommended_by: tuple[str, ...] = field(default_factory=tuple)
    group_rating: float | None = None
    rating_count: int = 0
