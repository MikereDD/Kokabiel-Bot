from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ImportSource = Literal[
    "trakt",
    "imdb",
    "letterboxd",
    "simkl",
]

ImportedMediaType = Literal[
    "movie",
    "tv",
    "episode",
    "season",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class NormalizedImportRecord:
    source: ImportSource
    media_type: ImportedMediaType

    title: str | None = None
    year: int | None = None

    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    trakt_id: int | None = None
    simkl_id: int | None = None

    season: int | None = None
    episode: int | None = None

    watched: bool = False
    watched_at: str | None = None

    watchlist: bool = False

    rating: float | None = None
    rated_at: str | None = None

    source_file: str | None = None
