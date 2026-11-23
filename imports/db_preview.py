from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from imports.resolve import (
    ResolutionPreview,
    ResolvedImportItem,
)
from models import MediaType, ViewingStatus


StateAction = Literal[
    "none",
    "add",
    "update",
]

RatingAction = Literal[
    "none",
    "add",
    "review",
]

NoticeSeverity = Literal[
    "warning",
    "review",
]


@dataclass(frozen=True, slots=True)
class ExistingPersonalState:
    watchlist: bool
    viewing_status: ViewingStatus
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExistingRating:
    rating: int
    rated_at: str


@dataclass(frozen=True, slots=True)
class ExistingUserSnapshot:
    user_id: int
    states: dict[
        tuple[MediaType, int],
        ExistingPersonalState,
    ]
    ratings: dict[
        tuple[MediaType, int],
        ExistingRating,
    ]
    episodes: dict[
        int,
        frozenset[tuple[int, int]],
    ]


@dataclass(frozen=True, slots=True)
class ImportDbNotice:
    severity: NoticeSeverity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ImportDbPreviewItem:
    resolution: ResolvedImportItem

    media_type: MediaType | None
    tmdb_id: int | None

    state_action: StateAction
    before_watchlist: bool
    after_watchlist: bool
    before_status: ViewingStatus
    after_status: ViewingStatus

    episode_additions: tuple[
        tuple[int, int],
        ...
    ]

    rating_action: RatingAction
    existing_rating: int | None
    proposed_rating: int | None

    notices: tuple[ImportDbNotice, ...]

    @property
    def needs_review(self) -> bool:
        return any(
            notice.severity == "review"
            for notice in self.notices
        )

    @property
    def has_changes(self) -> bool:
        return (
            self.state_action != "none"
            or bool(self.episode_additions)
            or self.rating_action == "add"
        )


@dataclass(frozen=True, slots=True)
class ImportDatabasePreview:
    source: str
    user_id: int
    raw_record_count: int
    items: tuple[ImportDbPreviewItem, ...]

    @property
    def state_add_count(self) -> int:
        return sum(
            item.state_action == "add"
            for item in self.items
        )

    @property
    def state_update_count(self) -> int:
        return sum(
            item.state_action == "update"
            for item in self.items
        )

    @property
    def episode_add_count(self) -> int:
        return sum(
            len(item.episode_additions)
            for item in self.items
        )

    @property
    def rating_add_count(self) -> int:
        return sum(
            item.rating_action == "add"
            for item in self.items
        )

    @property
    def review_count(self) -> int:
        return sum(
            item.needs_review
            for item in self.items
        )

    @property
    def skip_count(self) -> int:
        return sum(
            not item.has_changes
            and not item.needs_review
            for item in self.items
        )


def load_existing_user_snapshot(
    database_path: str | Path,
    user_id: int,
) -> ExistingUserSnapshot:
    """
    Read Kokabiel state using a SQLite read-only connection.

    The database must already exist. This function cannot create, modify,
    migrate or commit database content.
    """

    path = Path(database_path).resolve()

    if not path.is_file():
        raise FileNotFoundError(
            f"Kokabiel database does not exist: {path}"
        )

    uri = f"{path.as_uri()}?mode=ro"

    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(
            "PRAGMA query_only = ON"
        )
        connection.execute("BEGIN")

        state_rows = connection.execute(
            """
            SELECT
                media_type,
                tmdb_id,
                watchlist,
                viewing_status,
                updated_at
            FROM user_media_state
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

        rating_rows = connection.execute(
            """
            SELECT
                media_type,
                tmdb_id,
                rating,
                rated_at
            FROM ratings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

        episode_rows = connection.execute(
            """
            SELECT
                tmdb_id,
                season_number,
                episode_number
            FROM episode_progress
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

        connection.rollback()

    finally:
        connection.close()

    states: dict[
        tuple[MediaType, int],
        ExistingPersonalState,
    ] = {}

    for row in state_rows:
        media_type = str(
            row["media_type"]
        )

        if media_type not in {
            "movie",
            "tv",
        }:
            continue

        key = (
            media_type,
            int(row["tmdb_id"]),
        )

        states[key] = ExistingPersonalState(
            watchlist=bool(
                row["watchlist"]
            ),
            viewing_status=str(
                row["viewing_status"]
            ),
            updated_at=str(
                row["updated_at"]
            ),
        )

    ratings: dict[
        tuple[MediaType, int],
        ExistingRating,
    ] = {}

    for row in rating_rows:
        media_type = str(
            row["media_type"]
        )

        if media_type not in {
            "movie",
            "tv",
        }:
            continue

        key = (
            media_type,
            int(row["tmdb_id"]),
        )

        ratings[key] = ExistingRating(
            rating=int(row["rating"]),
            rated_at=str(
                row["rated_at"]
            ),
        )

    episode_sets: dict[
        int,
        set[tuple[int, int]],
    ] = {}

    for row in episode_rows:
        tmdb_id = int(
            row["tmdb_id"]
        )

        episode_sets.setdefault(
            tmdb_id,
            set(),
        ).add(
            (
                int(row["season_number"]),
                int(row["episode_number"]),
            )
        )

    episodes = {
        tmdb_id: frozenset(keys)
        for tmdb_id, keys
        in episode_sets.items()
    }

    return ExistingUserSnapshot(
        user_id=user_id,
        states=states,
        ratings=ratings,
        episodes=episodes,
    )


def preview_database_import(
    resolution: ResolutionPreview,
    *,
    database_path: str | Path,
    user_id: int,
) -> ImportDatabasePreview:
    snapshot = load_existing_user_snapshot(
        database_path,
        user_id,
    )

    return build_database_preview(
        resolution,
        snapshot,
    )


def build_database_preview(
    resolution: ResolutionPreview,
    snapshot: ExistingUserSnapshot,
) -> ImportDatabasePreview:
    canonical_counts: dict[
        tuple[str, int, str],
        int,
    ] = {}

    for item in resolution.items:
        if (
            item.status != "resolved"
            or item.resolved is None
        ):
            continue

        key = _canonical_key(item)

        canonical_counts[key] = (
            canonical_counts.get(
                key,
                0,
            )
            + 1
        )

    items = tuple(
        _preview_item(
            item,
            snapshot,
            canonical_counts,
        )
        for item in resolution.items
    )

    return ImportDatabasePreview(
        source=resolution.source,
        user_id=snapshot.user_id,
        raw_record_count=(
            resolution.raw_record_count
        ),
        items=items,
    )


def _canonical_key(
    item: ResolvedImportItem,
) -> tuple[str, int, str]:
    assert item.resolved is not None

    record = item.plan_item.proposed

    if record.media_type == "episode":
        granularity = (
            "episode:"
            f"{record.season}:"
            f"{record.episode}"
        )

    elif record.media_type == "season":
        granularity = (
            f"season:{record.season}"
        )

    else:
        granularity = "media"

    return (
        item.resolved.media_type,
        item.resolved.tmdb_id,
        granularity,
    )


def _preview_item(
    item: ResolvedImportItem,
    snapshot: ExistingUserSnapshot,
    canonical_counts: dict[
        tuple[str, int, str],
        int,
    ],
) -> ImportDbPreviewItem:
    notices: list[ImportDbNotice] = []

    if (
        item.status != "resolved"
        or item.resolved is None
    ):
        notices.append(
            ImportDbNotice(
                severity="review",
                code=(
                    "resolution_"
                    + item.status
                ),
                message=item.reason,
            )
        )

        return _empty_preview_item(
            item,
            notices,
        )

    canonical_key = _canonical_key(
        item
    )

    if canonical_counts.get(
        canonical_key,
        0,
    ) > 1:
        notices.append(
            ImportDbNotice(
                severity="review",
                code="canonical_duplicate",
                message=(
                    "Multiple import records resolved to "
                    "the same canonical TMDB identity "
                    "and granularity."
                ),
            )
        )

        return _empty_preview_item(
            item,
            notices,
            media_type=(
                item.resolved.media_type
            ),
            tmdb_id=(
                item.resolved.tmdb_id
            ),
        )

    media_type = item.resolved.media_type
    tmdb_id = item.resolved.tmdb_id
    record = item.plan_item.proposed

    for conflict in (
        item.plan_item.conflicts
    ):
        notices.append(
            ImportDbNotice(
                severity="review",
                code=(
                    "source_conflict_"
                    + conflict.field
                ),
                message=conflict.message,
            )
        )

    state_key = (
        media_type,
        tmdb_id,
    )

    existing_state = (
        snapshot.states.get(
            state_key
        )
    )

    (
        state_action,
        before_watchlist,
        after_watchlist,
        before_status,
        after_status,
        state_notices,
    ) = _plan_state(
        item,
        existing_state,
    )

    notices.extend(
        state_notices
    )

    episode_additions: tuple[
        tuple[int, int],
        ...
    ] = ()

    if (
        record.media_type == "episode"
        and record.watched
    ):
        if (
            record.season is None
            or record.episode is None
        ):
            notices.append(
                ImportDbNotice(
                    severity="review",
                    code="episode_coordinates_missing",
                    message=(
                        "Episode history lacks a season "
                        "or episode number."
                    ),
                )
            )
        else:
            key = (
                record.season,
                record.episode,
            )

            existing = (
                snapshot.episodes.get(
                    tmdb_id,
                    frozenset(),
                )
            )

            if key not in existing:
                episode_additions = (
                    key,
                )

    elif (
        record.media_type == "tv"
        and (
            record.season is not None
            or record.episode is not None
        )
    ):
        notices.append(
            ImportDbNotice(
                severity="warning",
                code="series_progress_hint_not_expanded",
                message=(
                    "The source supplied only a last-episode "
                    "progress marker. Kokabiel will not invent "
                    "the preceding episode history."
                ),
            )
        )

    rating_action: RatingAction = "none"
    existing_rating_value = None
    proposed_rating = None

    if record.rating is not None:
        if record.media_type not in {
            "movie",
            "tv",
        }:
            notices.append(
                ImportDbNotice(
                    severity="review",
                    code="granular_rating_unsupported",
                    message=(
                        "Episode/season ratings cannot be "
                        "silently converted into a series rating."
                    ),
                )
            )

        elif (
            record.rating < 1
            or record.rating > 10
            or not float(
                record.rating
            ).is_integer()
        ):
            notices.append(
                ImportDbNotice(
                    severity="review",
                    code="rating_not_representable",
                    message=(
                        "The imported rating cannot be "
                        "represented on Kokabiel's integer "
                        "1-10 rating scale."
                    ),
                )
            )

        else:
            proposed_rating = int(
                record.rating
            )

            existing_rating = (
                snapshot.ratings.get(
                    state_key
                )
            )

            if existing_rating is None:
                rating_action = "add"

            else:
                existing_rating_value = (
                    existing_rating.rating
                )

                if (
                    existing_rating.rating
                    != proposed_rating
                ):
                    rating_action = "review"

                    notices.append(
                        ImportDbNotice(
                            severity="review",
                            code="rating_conflict",
                            message=(
                                "Kokabiel already has a different "
                                "rating for this title. The existing "
                                "rating will not be overwritten "
                                "automatically."
                            ),
                        )
                    )

    return ImportDbPreviewItem(
        resolution=item,
        media_type=media_type,
        tmdb_id=tmdb_id,
        state_action=state_action,
        before_watchlist=before_watchlist,
        after_watchlist=after_watchlist,
        before_status=before_status,
        after_status=after_status,
        episode_additions=(
            episode_additions
        ),
        rating_action=rating_action,
        existing_rating=(
            existing_rating_value
        ),
        proposed_rating=proposed_rating,
        notices=tuple(notices),
    )


def _plan_state(
    item: ResolvedImportItem,
    existing: ExistingPersonalState | None,
) -> tuple[
    StateAction,
    bool,
    bool,
    ViewingStatus,
    ViewingStatus,
    list[ImportDbNotice],
]:
    assert item.resolved is not None

    record = item.plan_item.proposed
    media_type = item.resolved.media_type

    notices: list[
        ImportDbNotice
    ] = []

    before_watchlist = (
        existing.watchlist
        if existing is not None
        else False
    )

    before_status: ViewingStatus = (
        existing.viewing_status
        if existing is not None
        else "none"
    )

    after_watchlist = (
        before_watchlist
    )
    after_status = before_status

    desired_watchlist: bool | None = None
    desired_status: ViewingStatus | None = None

    if media_type == "movie":
        if record.watched:
            desired_status = "watched"
            desired_watchlist = False

        elif (
            record.watchlist
            or record.status
            == "plan_to_watch"
        ):
            desired_watchlist = True

        if record.status in {
            "watching",
            "completed",
            "dropped",
            "on_hold",
        }:
            notices.append(
                ImportDbNotice(
                    severity="review",
                    code="movie_status_unsupported",
                    message=(
                        "The source supplied a movie progress "
                        "status that does not map directly to "
                        "Kokabiel's movie state."
                    ),
                )
            )

    else:
        if (
            record.media_type == "episode"
            and record.watched
        ):
            desired_status = "watching"
            desired_watchlist = False

        elif record.media_type == "tv":
            if record.status == "completed":
                desired_status = "completed"
                desired_watchlist = False

            elif record.status == "watching":
                desired_status = "watching"
                desired_watchlist = False

            elif (
                record.status
                == "plan_to_watch"
                or record.watchlist
            ):
                desired_watchlist = True

            elif record.status in {
                "dropped",
                "on_hold",
            }:
                notices.append(
                    ImportDbNotice(
                        severity="review",
                        code="series_status_unsupported",
                        message=(
                            "Kokabiel has no direct dropped/on-hold "
                            "state. Existing progress will be preserved."
                        ),
                    )
                )

            elif (
                record.watched
                and record.status is None
            ):
                notices.append(
                    ImportDbNotice(
                        severity="review",
                        code="series_watched_ambiguous",
                        message=(
                            "The source marks the series as watched "
                            "without enough information to distinguish "
                            "watching, caught up, or completed."
                        ),
                    )
                )

    if desired_status is not None:
        after_status = _non_regressing_status(
            before_status,
            desired_status,
            media_type,
        )

        if after_status != "none":
            after_watchlist = False

    if desired_watchlist is True:
        if after_status == "none":
            after_watchlist = True

    elif desired_watchlist is False:
        after_watchlist = False

    changed = (
        after_watchlist
        != before_watchlist
        or after_status
        != before_status
    )

    if not changed:
        action: StateAction = "none"

    elif existing is None:
        action = "add"

    else:
        action = "update"

    return (
        action,
        before_watchlist,
        after_watchlist,
        before_status,
        after_status,
        notices,
    )


def _non_regressing_status(
    existing: ViewingStatus,
    proposed: ViewingStatus,
    media_type: MediaType,
) -> ViewingStatus:
    if media_type == "movie":
        if existing == "watched":
            return existing

        return proposed

    rank = {
        "none": 0,
        "watching": 1,
        "caught_up": 2,
        "completed": 3,
    }

    existing_rank = rank.get(
        existing,
        0,
    )

    proposed_rank = rank.get(
        proposed,
        0,
    )

    if existing_rank >= proposed_rank:
        return existing

    return proposed


def _empty_preview_item(
    item: ResolvedImportItem,
    notices: list[ImportDbNotice],
    *,
    media_type: MediaType | None = None,
    tmdb_id: int | None = None,
) -> ImportDbPreviewItem:
    return ImportDbPreviewItem(
        resolution=item,
        media_type=media_type,
        tmdb_id=tmdb_id,
        state_action="none",
        before_watchlist=False,
        after_watchlist=False,
        before_status="none",
        after_status="none",
        episode_additions=(),
        rating_action="none",
        existing_rating=None,
        proposed_rating=None,
        notices=tuple(notices),
    )
