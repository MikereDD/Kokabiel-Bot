from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from imports.detect import (
    DetectedImport,
    detect_import_source,
)
from imports.models import (
    ImportedMediaType,
    ImportSource,
    ImportStatus,
    NormalizedImportRecord,
)


class ImportParseError(ValueError):
    """Raised when a validated tracker export cannot be normalized."""


@dataclass(frozen=True, slots=True)
class ParsedImport:
    source: ImportSource
    records: tuple[NormalizedImportRecord, ...]
    source_files: tuple[str, ...]


LETTERBOXD_DATA_FILES = frozenset(
    {
        "watched.csv",
        "ratings.csv",
        "diary.csv",
        "watchlist.csv",
    }
)

TRAKT_DATA_PREFIXES = (
    "watched-history",
    "watched-movies",
    "watched-shows",
    "ratings-movies",
    "ratings-shows",
    "ratings-seasons",
    "ratings-episodes",
    "lists-watchlist",
    "collection-movies",
    "collection-shows",
)

SIMKL_STATUS_MAP: dict[str, ImportStatus] = {
    "watching": "watching",
    "completed": "completed",
    "plan to watch": "plan_to_watch",
    "plan_to_watch": "plan_to_watch",
    "plantowatch": "plan_to_watch",
    "dropped": "dropped",
    "hold": "on_hold",
    "on hold": "on_hold",
    "on_hold": "on_hold",
}

LAST_EPISODE_PATTERN = re.compile(
    r"^S(?P<season>\d+)E(?P<episode>\d+)$",
    re.IGNORECASE,
)


def parse_import(
    path: str | Path,
    *,
    source_name: str | None = None,
) -> ParsedImport:
    """
    Validate, detect and normalize a tracker export.

    This layer performs no network calls, database writes, Telegram actions,
    extraction to disk, or command execution.
    """

    candidate = Path(path)

    logical_name = PurePosixPath(
        (
            source_name
            or candidate.name
        ).replace("\\", "/")
    ).name

    if not logical_name:
        logical_name = candidate.name

    detected = detect_import_source(
        candidate,
        source_name=logical_name,
    )

    _assert_unchanged(
        candidate,
        detected.inspection.sha256,
    )

    records: list[NormalizedImportRecord]
    source_files: list[str]

    if detected.source == "imdb":
        data = candidate.read_bytes()
        records = _parse_imdb_csv(
            data,
            logical_name,
        )
        source_files = [logical_name]

    elif detected.source == "simkl":
        data = candidate.read_bytes()

        if detected.inspection.kind == "json":
            records = _parse_simkl_json(
                data,
                logical_name,
            )
        else:
            records = _parse_simkl_csv(
                data,
                logical_name,
            )

        source_files = [logical_name]

    elif detected.source == "letterboxd":
        if detected.inspection.kind == "zip":
            records, source_files = (
                _parse_letterboxd_zip(candidate)
            )
        else:
            data = candidate.read_bytes()
            records = _parse_letterboxd_csv(
                data,
                logical_name,
            )
            source_files = [logical_name]

    elif detected.source == "trakt":
        if detected.inspection.kind == "zip":
            records, source_files = (
                _parse_trakt_zip(candidate)
            )
        else:
            data = candidate.read_bytes()
            records = _parse_trakt_json(
                data,
                logical_name,
            )
            source_files = [logical_name]

    else:
        raise ImportParseError(
            f"Unsupported detected source: {detected.source}"
        )

    _assert_unchanged(
        candidate,
        detected.inspection.sha256,
    )

    return ParsedImport(
        source=detected.source,
        records=tuple(records),
        source_files=tuple(source_files),
    )


def _parse_imdb_csv(
    data: bytes,
    source_file: str,
) -> list[NormalizedImportRecord]:
    rows = _csv_rows(data)
    basename = PurePosixPath(
        source_file.replace("\\", "/")
    ).name.casefold()

    records: list[NormalizedImportRecord] = []

    for row in rows:
        title = _value(
            row,
            "title",
            "original title",
        )

        imdb_id = _imdb_id(
            _value(row, "const")
        )

        if not title and not imdb_id:
            continue

        rating = _bounded_float(
            _value(row, "your rating"),
            minimum=1.0,
            maximum=10.0,
        )

        is_watchlist = basename == "watchlist.csv"
        is_checkin = basename == "checkins.csv"

        watched = (
            rating is not None
            or is_checkin
        )

        records.append(
            NormalizedImportRecord(
                source="imdb",
                media_type=_imdb_media_type(
                    _value(row, "title type")
                ),
                title=title,
                year=_year(_value(row, "year")),
                imdb_id=imdb_id,
                watched=watched,
                watched_at=(
                    _value(row, "created")
                    if is_checkin
                    else None
                ),
                watchlist=is_watchlist,
                status=(
                    "plan_to_watch"
                    if is_watchlist
                    else None
                ),
                rating=rating,
                rated_at=(
                    _value(row, "date rated")
                    if rating is not None
                    else None
                ),
                source_file=source_file,
            )
        )

    return records


def _parse_letterboxd_csv(
    data: bytes,
    source_file: str,
) -> list[NormalizedImportRecord]:
    rows = _csv_rows(data)

    basename = PurePosixPath(
        source_file.replace("\\", "/")
    ).name.casefold()

    watched_file = basename in {
        "watched.csv",
        "ratings.csv",
        "diary.csv",
    }

    watchlist_file = (
        basename == "watchlist.csv"
    )

    records: list[NormalizedImportRecord] = []

    for row in rows:
        title = _value(
            row,
            "name",
            "title",
        )

        if not title:
            continue

        source_rating = _bounded_float(
            _value(row, "rating"),
            minimum=0.5,
            maximum=5.0,
        )

        # Letterboxd uses 0.5-5 stars. Kokabiel uses 1-10.
        rating = (
            source_rating * 2.0
            if source_rating is not None
            else None
        )

        watched_at = None

        # Date in watched.csv is activity metadata, not necessarily
        # the historical date the film was viewed.
        if basename == "diary.csv":
            watched_at = _value(
                row,
                "watched date",
                "watcheddate",
            )

        records.append(
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title=title,
                year=_year(_value(row, "year")),
                watched=watched_file,
                watched_at=watched_at,
                watchlist=watchlist_file,
                status=(
                    "plan_to_watch"
                    if watchlist_file
                    else None
                ),
                rating=rating,
                rated_at=(
                    _value(row, "date")
                    if rating is not None
                    else None
                ),
                source_file=source_file,
            )
        )

    return records


def _parse_simkl_csv(
    data: bytes,
    source_file: str,
) -> list[NormalizedImportRecord]:
    records: list[NormalizedImportRecord] = []

    for row in _csv_rows(data):
        raw_type = (
            _value(row, "type")
            or ""
        ).casefold()

        media_type: ImportedMediaType

        if raw_type == "movie":
            media_type = "movie"
        elif raw_type in {"tv", "anime"}:
            media_type = "tv"
        else:
            media_type = "unknown"

        raw_status = (
            _value(row, "watchlist")
            or ""
        ).strip().casefold()

        status = SIMKL_STATUS_MAP.get(
            raw_status
        )

        season = None
        episode = None

        last_episode = _value(
            row,
            "lastepwatched",
        )

        if last_episode:
            match = LAST_EPISODE_PATTERN.match(
                last_episode.strip()
            )

            if match:
                season = int(
                    match.group("season")
                )
                episode = int(
                    match.group("episode")
                )

        watched_at = _value(
            row,
            "watcheddate",
        )

        rating = _bounded_float(
            _value(row, "rating"),
            minimum=1.0,
            maximum=10.0,
        )

        watched = bool(watched_at) or (
            status == "completed"
        )

        records.append(
            NormalizedImportRecord(
                source="simkl",
                media_type=media_type,
                title=_value(row, "title"),
                year=_year(_value(row, "year")),
                tmdb_id=_positive_int(
                    _value(row, "tmdb_id")
                ),
                imdb_id=_imdb_id(
                    _value(row, "imdb_id")
                ),
                tvdb_id=_positive_int(
                    _value(row, "tvdb_id")
                ),
                simkl_id=_positive_int(
                    _value(row, "simkl_id")
                ),
                season=season,
                episode=episode,
                watched=watched,
                watched_at=watched_at,
                watchlist=(
                    status == "plan_to_watch"
                ),
                status=status,
                rating=rating,
                source_file=source_file,
            )
        )

    return records


def _parse_simkl_json(
    data: bytes,
    source_file: str,
) -> list[NormalizedImportRecord]:
    try:
        payload = json.loads(
            data.decode("utf-8-sig")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ImportParseError(
            f"Unable to parse Simkl JSON: {source_file}"
        ) from exc

    if not isinstance(payload, dict):
        raise ImportParseError(
            "Simkl JSON root must be an object."
        )

    collections = (
        ("shows", "show"),
        ("anime", "show"),
        ("movies", "movie"),
    )

    records: list[
        NormalizedImportRecord
    ] = []

    for collection, identity_key in collections:
        items = payload.get(
            collection,
            [],
        )

        if not isinstance(items, list):
            raise ImportParseError(
                f"Simkl JSON collection is not a list: {collection}"
            )

        for item in items:
            if not isinstance(item, dict):
                raise ImportParseError(
                    f"Simkl JSON {collection} item is not an object."
                )

            identity = item.get(
                identity_key
            )

            if not isinstance(
                identity,
                dict,
            ):
                raise ImportParseError(
                    f"Simkl JSON {collection} item lacks "
                    f"{identity_key} identity."
                )

            records.extend(
                _simkl_json_item_records(
                    item,
                    identity,
                    collection=collection,
                    source_file=source_file,
                )
            )

    return records


def _simkl_json_item_records(
    item: dict[str, Any],
    identity: dict[str, Any],
    *,
    collection: str,
    source_file: str,
) -> list[NormalizedImportRecord]:
    ids = identity.get("ids")

    if not isinstance(ids, dict):
        ids = {}

    media_type = _simkl_json_media_type(
        item,
        ids,
        collection=collection,
    )

    raw_status = (
        _clean(
            item.get("status")
        )
        or ""
    ).casefold()

    status = SIMKL_STATUS_MAP.get(
        raw_status
    )

    last_watched_at = _clean(
        item.get("last_watched_at")
    )

    watched = (
        bool(last_watched_at)
        or status == "completed"
    )

    watchlist = (
        status == "plan_to_watch"
    )

    # Kokabiel's film state is watched/watchlist rather than
    # watching/completed. A completed Simkl movie therefore becomes
    # watched without carrying a series-only "completed" state.
    parent_status = status

    if (
        media_type == "movie"
        and status == "completed"
    ):
        parent_status = None

    tmdb_value: Any = ids.get(
        "tmdb"
    )

    # Simkl uses tmdbm for some anime specials that resolve in TMDB's
    # movie namespace rather than the TV namespace.
    if (
        collection == "anime"
        and media_type == "movie"
    ):
        tmdb_value = (
            ids.get("tmdbm")
            or ids.get("tmdb")
        )

    rating = _bounded_float(
        item.get("user_rating"),
        minimum=1.0,
        maximum=10.0,
    )

    common = {
        "source": "simkl",
        "title": _clean(
            identity.get("title")
        ),
        "year": _year(
            identity.get("year")
        ),
        "tmdb_id": _positive_int(
            tmdb_value
        ),
        "imdb_id": _imdb_id(
            ids.get("imdb")
        ),
        "tvdb_id": _positive_int(
            ids.get("tvdb")
        ),
        "simkl_id": _positive_int(
            ids.get("simkl")
        ),
        "source_file": source_file,
    }

    records = [
        NormalizedImportRecord(
            **common,
            media_type=media_type,
            watched=watched,
            watched_at=last_watched_at,
            watchlist=watchlist,
            status=parent_status,
            rating=rating,
            rated_at=(
                _clean(
                    item.get(
                        "user_rated_at"
                    )
                )
                if rating is not None
                else None
            ),
        )
    ]

    # Episode history is imported only when Simkl supplied explicit
    # season/episode records. last_watched is never expanded into guessed
    # preceding history.
    if media_type != "tv":
        return records

    seasons = item.get(
        "seasons",
        [],
    )

    if seasons is None:
        seasons = []

    if not isinstance(
        seasons,
        list,
    ):
        raise ImportParseError(
            "Simkl JSON seasons value is not a list."
        )

    for season_item in seasons:
        if not isinstance(
            season_item,
            dict,
        ):
            raise ImportParseError(
                "Simkl JSON season is not an object."
            )

        season_number = _positive_int(
            season_item.get("number")
        )

        episodes = season_item.get(
            "episodes",
            [],
        )

        if not isinstance(
            episodes,
            list,
        ):
            raise ImportParseError(
                "Simkl JSON episodes value is not a list."
            )

        for episode_item in episodes:
            if not isinstance(
                episode_item,
                dict,
            ):
                raise ImportParseError(
                    "Simkl JSON episode is not an object."
                )

            episode_number = (
                _positive_int(
                    episode_item.get(
                        "number"
                    )
                )
            )

            if (
                season_number is None
                or episode_number is None
            ):
                raise ImportParseError(
                    "Simkl JSON watched episode lacks "
                    "season or episode number."
                )

            records.append(
                NormalizedImportRecord(
                    **common,
                    media_type="episode",
                    episode_mapping_unverified=(
                        collection == "anime"
                    ),
                    season=season_number,
                    episode=episode_number,
                    watched=True,
                    watched_at=_clean(
                        episode_item.get(
                            "watched_at"
                        )
                    ),
                )
            )

    return records


def _simkl_json_media_type(
    item: dict[str, Any],
    ids: dict[str, Any],
    *,
    collection: str,
) -> ImportedMediaType:
    if collection == "movies":
        return "movie"

    if collection == "shows":
        return "tv"

    anime_type = (
        _clean(
            item.get("anime_type")
        )
        or ""
    ).casefold()

    if anime_type == "movie":
        return "movie"

    if (
        anime_type == "special"
        and _positive_int(
            ids.get("tmdbm")
        )
        is not None
    ):
        return "movie"

    # tv, ova and ona are episodic. Unknown anime forms remain TV-shaped
    # rather than being silently converted into films.
    return "tv"


def _parse_letterboxd_zip(
    path: Path,
) -> tuple[
    list[NormalizedImportRecord],
    list[str],
]:
    records: list[NormalizedImportRecord] = []
    source_files: list[str] = []

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            normalized = PurePosixPath(
                info.filename.replace("\\", "/")
            )

            # Letterboxd account exports may contain deleted content.
            # Deleted history must never be silently re-imported.
            if any(
                part.casefold() == "deleted"
                for part in normalized.parts[:-1]
            ):
                continue

            basename = normalized.name.casefold()

            if basename not in LETTERBOXD_DATA_FILES:
                continue

            with archive.open(info, "r") as member:
                data = member.read()

            records.extend(
                _parse_letterboxd_csv(
                    data,
                    info.filename,
                )
            )

            source_files.append(
                info.filename
            )

    return records, source_files


def _parse_trakt_zip(
    path: Path,
) -> tuple[
    list[NormalizedImportRecord],
    list[str],
]:
    records: list[NormalizedImportRecord] = []
    source_files: list[str] = []

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            basename = PurePosixPath(
                info.filename.replace("\\", "/")
            ).name.casefold()

            if not any(
                basename.startswith(prefix)
                for prefix in TRAKT_DATA_PREFIXES
            ):
                continue

            if not basename.endswith(".json"):
                continue

            with archive.open(info, "r") as member:
                data = member.read()

            records.extend(
                _parse_trakt_json(
                    data,
                    info.filename,
                )
            )

            source_files.append(
                info.filename
            )

    return records, source_files


def _parse_trakt_json(
    data: bytes,
    source_file: str,
) -> list[NormalizedImportRecord]:
    try:
        payload = json.loads(
            data.decode("utf-8-sig")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ImportParseError(
            f"Unable to parse Trakt JSON: {source_file}"
        ) from exc

    items = _json_items(payload)
    mode = _trakt_mode(source_file)

    records: list[NormalizedImportRecord] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        record = _trakt_record(
            item,
            mode=mode,
            source_file=source_file,
        )

        if record is not None:
            records.append(record)

    return records


def _trakt_record(
    item: dict[str, Any],
    *,
    mode: str,
    source_file: str,
) -> NormalizedImportRecord | None:
    media_type: ImportedMediaType
    identity: dict[str, Any]
    season = None
    episode = None

    movie = item.get("movie")
    show = item.get("show")
    season_item = item.get("season")
    episode_item = item.get("episode")

    if isinstance(movie, dict):
        media_type = "movie"
        identity = movie

    elif isinstance(episode_item, dict):
        media_type = "episode"

        season = _positive_int(
            episode_item.get("season")
        )

        episode = _positive_int(
            episode_item.get("number")
        )

        # Prefer parent-show identity for episode progress because Kokabiel
        # tracks TV progress against the series TMDB id.
        identity = (
            show
            if isinstance(show, dict)
            else episode_item
        )

    elif isinstance(season_item, dict):
        media_type = "season"

        season = _positive_int(
            season_item.get("number")
        )

        identity = (
            show
            if isinstance(show, dict)
            else season_item
        )

    elif isinstance(show, dict):
        media_type = "tv"
        identity = show

    else:
        return None

    ids = identity.get("ids")

    if not isinstance(ids, dict):
        ids = {}

    rating = _bounded_float(
        item.get("rating"),
        minimum=1.0,
        maximum=10.0,
    )

    is_history = mode == "history"
    is_watchlist = mode == "watchlist"

    return NormalizedImportRecord(
        source="trakt",
        media_type=media_type,
        title=_clean(identity.get("title")),
        year=_year(identity.get("year")),
        tmdb_id=_positive_int(
            ids.get("tmdb")
        ),
        imdb_id=_imdb_id(
            ids.get("imdb")
        ),
        tvdb_id=_positive_int(
            ids.get("tvdb")
        ),
        trakt_id=_positive_int(
            ids.get("trakt")
        ),
        season=season,
        episode=episode,
        watched=is_history,
        watched_at=(
            _clean(item.get("watched_at"))
            or _clean(
                item.get("last_watched_at")
            )
            if is_history
            else None
        ),
        watchlist=is_watchlist,
        status=(
            "plan_to_watch"
            if is_watchlist
            else None
        ),
        rating=rating,
        rated_at=(
            _clean(item.get("rated_at"))
            if rating is not None
            else None
        ),
        source_file=source_file,
    )


def _trakt_mode(
    source_file: str,
) -> str:
    basename = PurePosixPath(
        source_file.replace("\\", "/")
    ).name.casefold()

    if (
        basename.startswith("watched-")
        or basename.startswith(
            "watched-history"
        )
    ):
        return "history"

    if basename.startswith("ratings-"):
        return "rating"

    if basename.startswith(
        "lists-watchlist"
    ):
        return "watchlist"

    return "neutral"


def _json_items(
    payload: Any,
) -> list[Any]:
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        items = payload.get("items")

        if isinstance(items, list):
            return items

        return [payload]

    return []


def _csv_rows(
    data: bytes,
) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportParseError(
            "Import CSV is not valid UTF-8."
        ) from exc

    try:
        reader = csv.DictReader(
            io.StringIO(text, newline=""),
            strict=True,
        )

        result: list[dict[str, str]] = []

        for raw in reader:
            row: dict[str, str] = {}

            for key, value in raw.items():
                if key is None:
                    continue

                normalized_key = (
                    str(key)
                    .strip()
                    .casefold()
                )

                if not normalized_key:
                    continue

                row[normalized_key] = (
                    str(value).strip()
                    if value is not None
                    else ""
                )

            result.append(row)

        return result

    except csv.Error as exc:
        raise ImportParseError(
            "Import CSV could not be parsed."
        ) from exc


def _value(
    row: dict[str, str],
    *names: str,
) -> str | None:
    for name in names:
        value = row.get(
            name.casefold()
        )

        if value:
            return value

    return None


def _clean(
    value: Any,
) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def _positive_int(
    value: Any,
) -> int | None:
    text = _clean(value)

    if text is None:
        return None

    try:
        result = int(text)
    except (TypeError, ValueError):
        return None

    return result if result >= 0 else None


def _year(
    value: Any,
) -> int | None:
    result = _positive_int(value)

    if result is None:
        return None

    if 1800 <= result <= 3000:
        return result

    return None


def _bounded_float(
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    text = _clean(value)

    if text is None:
        return None

    try:
        result = float(text)
    except (TypeError, ValueError):
        return None

    if not minimum <= result <= maximum:
        return None

    return result


def _imdb_id(
    value: Any,
) -> str | None:
    text = _clean(value)

    if text is None:
        return None

    if re.fullmatch(
        r"tt\d+",
        text,
        re.IGNORECASE,
    ):
        return text.lower()

    return None


def _imdb_media_type(
    value: str | None,
) -> ImportedMediaType:
    kind = (
        value or ""
    ).replace(" ", "").casefold()

    if kind in {
        "tvseries",
        "tvminiseries",
        "tvshort",
    }:
        return "tv"

    if kind in {
        "tvepisode",
    }:
        return "episode"

    if kind in {
        "movie",
        "short",
        "video",
        "tvmovie",
        "tvspecial",
    }:
        return "movie"

    return "unknown"


def _assert_unchanged(
    path: Path,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(
                1024 * 1024
            ),
            b"",
        ):
            digest.update(chunk)

    if digest.hexdigest() != expected_sha256:
        raise ImportParseError(
            "Import file changed after security validation."
        )
