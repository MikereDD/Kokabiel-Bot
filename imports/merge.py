from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from imports.models import (
    ImportSource,
    ImportStatus,
    NormalizedImportRecord,
)
from imports.parse import ParsedImport, parse_import


@dataclass(frozen=True, slots=True)
class ImportConflict:
    field: str
    values: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class ImportPlanItem:
    key: str
    identity_basis: str
    proposed: NormalizedImportRecord
    source_record_count: int
    source_files: tuple[str, ...]
    conflicts: tuple[ImportConflict, ...]


@dataclass(frozen=True, slots=True)
class ImportPlan:
    source: ImportSource
    raw_record_count: int
    items: tuple[ImportPlanItem, ...]

    @property
    def conflict_count(self) -> int:
        return sum(
            len(item.conflicts)
            for item in self.items
        )

    @property
    def conflicted_item_count(self) -> int:
        return sum(
            1
            for item in self.items
            if item.conflicts
        )


def analyze_import(
    path: str,
    *,
    source_name: str | None = None,
) -> ImportPlan:
    return build_import_plan(
        parse_import(
            path,
            source_name=source_name,
        )
    )


def build_import_plan(
    parsed: ParsedImport,
) -> ImportPlan:
    groups: dict[
        str,
        list[NormalizedImportRecord],
    ] = {}

    bases: dict[str, str] = {}

    for index, record in enumerate(
        parsed.records
    ):
        key, basis = _record_key(
            record,
            index=index,
        )

        groups.setdefault(
            key,
            [],
        ).append(record)

        bases[key] = basis

    items = tuple(
        _merge_group(
            key,
            bases[key],
            groups[key],
        )
        for key in sorted(
            groups,
            key=_plan_group_sort_key,
        )
    )

    return ImportPlan(
        source=parsed.source,
        raw_record_count=len(
            parsed.records
        ),
        items=items,
    )


def _plan_group_sort_key(
    key: str,
) -> tuple[int, str]:
    """
    Keep deterministic import planning while processing parent media
    before granular season/episode history.

    This lets a parent TV record establish its final state first so later
    episode-union records do not create an unnecessary intermediate
    Watching -> Completed update.
    """

    granularity = key.split(
        ":",
        1,
    )[0]

    rank = {
        "movie": 0,
        "tv": 0,
        "season": 1,
        "episode": 2,
    }.get(
        granularity,
        3,
    )

    return (
        rank,
        key,
    )


def _record_key(
    record: NormalizedImportRecord,
    *,
    index: int,
) -> tuple[str, str]:
    granularity = _granularity(record)

    identities = (
        (
            "tmdb",
            record.tmdb_id,
        ),
        (
            "imdb",
            record.imdb_id,
        ),
        (
            "tvdb",
            record.tvdb_id,
        ),
        (
            "trakt",
            record.trakt_id,
        ),
        (
            "simkl",
            record.simkl_id,
        ),
    )

    for name, value in identities:
        if value not in {
            None,
            "",
        }:
            return (
                f"{granularity}:{name}:{value}",
                name,
            )

    if record.title:
        title = " ".join(
            record.title
            .casefold()
            .split()
        )

        year = (
            str(record.year)
            if record.year is not None
            else "unknown"
        )

        return (
            f"{granularity}:title:{title}:{year}",
            "title_year",
        )

    return (
        f"{granularity}:unresolved:{index}",
        "unresolved",
    )


def _granularity(
    record: NormalizedImportRecord,
) -> str:
    if record.media_type == "episode":
        return (
            "episode:"
            f"{record.season if record.season is not None else '?'}:"
            f"{record.episode if record.episode is not None else '?'}"
        )

    if record.media_type == "season":
        return (
            "season:"
            f"{record.season if record.season is not None else '?'}"
        )

    return record.media_type


def _merge_group(
    key: str,
    identity_basis: str,
    records: list[
        NormalizedImportRecord
    ],
) -> ImportPlanItem:
    conflicts: list[
        ImportConflict
    ] = []

    title = _merge_text(
        "title",
        (
            record.title
            for record in records
        ),
        conflicts,
        casefold=True,
    )

    year = _merge_scalar(
        "year",
        (
            record.year
            for record in records
        ),
        conflicts,
    )

    tmdb_id = _merge_scalar(
        "tmdb_id",
        (
            record.tmdb_id
            for record in records
        ),
        conflicts,
    )

    imdb_id = _merge_text(
        "imdb_id",
        (
            record.imdb_id
            for record in records
        ),
        conflicts,
        casefold=True,
    )

    tvdb_id = _merge_scalar(
        "tvdb_id",
        (
            record.tvdb_id
            for record in records
        ),
        conflicts,
    )

    trakt_id = _merge_scalar(
        "trakt_id",
        (
            record.trakt_id
            for record in records
        ),
        conflicts,
    )

    simkl_id = _merge_scalar(
        "simkl_id",
        (
            record.simkl_id
            for record in records
        ),
        conflicts,
    )

    season = _merge_scalar(
        "season",
        (
            record.season
            for record in records
        ),
        conflicts,
    )

    episode = _merge_scalar(
        "episode",
        (
            record.episode
            for record in records
        ),
        conflicts,
    )

    media_type = _merge_scalar(
        "media_type",
        (
            record.media_type
            for record in records
        ),
        conflicts,
    ) or "unknown"

    watched = any(
        record.watched
        for record in records
    )

    watched_at = _latest_timestamp(
        record.watched_at
        for record in records
    )

    status = _merge_status(
        (
            record.status
            for record in records
        ),
        watched=watched,
        conflicts=conflicts,
    )

    if status == "completed":
        watched = True

    watchlist = any(
        record.watchlist
        for record in records
    )

    # Historical/current watched state wins over stale watchlist state.
    if watched or status in {
        "watching",
        "completed",
        "dropped",
        "on_hold",
    }:
        watchlist = False

    rating = _merge_rating(
        records,
        conflicts,
    )

    rated_at = None

    if rating is not None:
        rated_at = _latest_timestamp(
            record.rated_at
            for record in records
            if record.rating == rating
        )

    source_files = tuple(
        sorted(
            {
                record.source_file
                for record in records
                if record.source_file
            }
        )
    )

    episode_mapping_unverified = any(
        record.episode_mapping_unverified
        for record in records
    )

    if episode_mapping_unverified:
        conflicts.append(
            ImportConflict(
                field="episode_mapping",
                values=("unverified",),
                message=(
                    "Simkl anime episode numbering has not "
                    "been verified against TMDB. Episode "
                    "history requires review."
                ),
            )
        )

    proposed = NormalizedImportRecord(
        episode_mapping_unverified=episode_mapping_unverified,
        source=records[0].source,
        media_type=media_type,
        title=title,
        year=year,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        trakt_id=trakt_id,
        simkl_id=simkl_id,
        season=season,
        episode=episode,
        watched=watched,
        watched_at=watched_at,
        watchlist=watchlist,
        status=status,
        rating=rating,
        rated_at=rated_at,
        source_file=(
            source_files[0]
            if len(source_files) == 1
            else None
        ),
    )

    return ImportPlanItem(
        key=key,
        identity_basis=identity_basis,
        proposed=proposed,
        source_record_count=len(records),
        source_files=source_files,
        conflicts=tuple(conflicts),
    )


def _merge_scalar(
    field: str,
    values: Iterable[object | None],
    conflicts: list[
        ImportConflict
    ],
):
    present = [
        value
        for value in values
        if value is not None
    ]

    unique = list(
        dict.fromkeys(present)
    )

    if not unique:
        return None

    if len(unique) > 1:
        conflicts.append(
            ImportConflict(
                field=field,
                values=tuple(
                    str(value)
                    for value in unique
                ),
                message=(
                    f"Conflicting {field} values "
                    "were supplied by the export."
                ),
            )
        )

    return unique[0]


def _merge_text(
    field: str,
    values: Iterable[str | None],
    conflicts: list[
        ImportConflict
    ],
    *,
    casefold: bool,
) -> str | None:
    present = [
        value.strip()
        for value in values
        if value
        and value.strip()
    ]

    if not present:
        return None

    normalized: dict[
        str,
        str,
    ] = {}

    for value in present:
        key = (
            value.casefold()
            if casefold
            else value
        )

        normalized.setdefault(
            key,
            value,
        )

    unique = list(
        normalized.values()
    )

    if len(unique) > 1:
        conflicts.append(
            ImportConflict(
                field=field,
                values=tuple(unique),
                message=(
                    f"Conflicting {field} values "
                    "were supplied by the export."
                ),
            )
        )

    return unique[0]


def _merge_rating(
    records: list[
        NormalizedImportRecord
    ],
    conflicts: list[
        ImportConflict
    ],
) -> float | None:
    ratings = [
        record.rating
        for record in records
        if record.rating is not None
    ]

    unique = list(
        dict.fromkeys(ratings)
    )

    if not unique:
        return None

    if len(unique) == 1:
        return unique[0]

    conflicts.append(
        ImportConflict(
            field="rating",
            values=tuple(
                str(value)
                for value in unique
            ),
            message=(
                "The export contains multiple different "
                "ratings for the same media identity."
            ),
        )
    )

    # Never silently choose between conflicting ratings.
    return None


def _merge_status(
    values: Iterable[
        ImportStatus | None
    ],
    *,
    watched: bool,
    conflicts: list[
        ImportConflict
    ],
) -> ImportStatus | None:
    present = [
        value
        for value in values
        if value is not None
    ]

    unique = list(
        dict.fromkeys(present)
    )

    if not unique:
        return None

    # These are expected progressions rather than conflicts.
    if watched and unique == [
        "plan_to_watch"
    ]:
        return None

    if "completed" in unique:
        return "completed"

    if "watching" in unique:
        others = {
            value
            for value in unique
            if value not in {
                "watching",
                "plan_to_watch",
            }
        }

        if not others:
            return "watching"

    non_plan = [
        value
        for value in unique
        if value != "plan_to_watch"
    ]

    if len(non_plan) == 1:
        return non_plan[0]

    if len(non_plan) == 0:
        return (
            None
            if watched
            else "plan_to_watch"
        )

    conflicts.append(
        ImportConflict(
            field="status",
            values=tuple(unique),
            message=(
                "The export contains incompatible "
                "progress states for the same media identity."
            ),
        )
    )

    # Require review rather than selecting a destructive state.
    return None


def _latest_timestamp(
    values: Iterable[str | None],
) -> str | None:
    present = [
        value.strip()
        for value in values
        if value
        and value.strip()
    ]

    if not present:
        return None

    parsed = [
        (
            _parse_timestamp(value),
            index,
            value,
        )
        for index, value in enumerate(
            present
        )
    ]

    valid = [
        item
        for item in parsed
        if item[0] is not None
    ]

    if valid:
        return max(
            valid,
            key=lambda item: (
                item[0],
                item[1],
            ),
        )[2]

    # Preserve data even when a source uses a date format we do not yet
    # understand. Do not invent chronological meaning.
    return present[-1]


def _parse_timestamp(
    value: str,
) -> datetime | None:
    normalized = value.strip()

    if normalized.endswith("Z"):
        normalized = (
            normalized[:-1]
            + "+00:00"
        )

    try:
        return datetime.fromisoformat(
            normalized
        )
    except ValueError:
        pass

    for pattern in (
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(
                value,
                pattern,
            )
        except ValueError:
            continue

    return None
