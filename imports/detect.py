from __future__ import annotations

import csv
import io
import zipfile

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from imports.models import ImportSource
from imports.security import (
    InspectionResult,
    ImportSecurityError,
    inspect_import_file,
)


class ImportDetectionError(ValueError):
    """Raised when a validated import cannot be identified safely."""


@dataclass(frozen=True, slots=True)
class DetectedImport:
    source: ImportSource
    inspection: InspectionResult
    evidence: tuple[str, ...]


TRAKT_PREFIXES = (
    "watched-history",
    "watched-movies",
    "watched-shows",
    "ratings-movies",
    "ratings-shows",
    "ratings-seasons",
    "ratings-episodes",
    "lists-watchlist",
    "lists-favorites",
    "lists-lists",
    "collection-movies",
    "collection-shows",
)

LETTERBOXD_BASENAMES = frozenset(
    {
        "profile.csv",
        "watched.csv",
        "ratings.csv",
        "diary.csv",
        "watchlist.csv",
        "reviews.csv",
        "comments.csv",
    }
)


def detect_import_source(
    path: str | Path,
) -> DetectedImport:
    """
    Validate an upload through the security gate and identify its source.

    Detection never writes files, never extracts archives, and never mutates
    application state.
    """

    candidate = Path(path)
    inspection = inspect_import_file(candidate)

    if inspection.kind == "zip":
        return _detect_zip(candidate, inspection)

    if inspection.kind == "csv":
        return _detect_csv(candidate, inspection)

    if inspection.kind == "json":
        return _detect_json(candidate, inspection)

    raise ImportDetectionError(
        f"Validated import type is not source-detectable: {inspection.kind}"
    )


def _detect_zip(
    path: Path,
    inspection: InspectionResult,
) -> DetectedImport:
    member_names = tuple(
        member.name
        for member in inspection.members
    )

    basenames = tuple(
        PurePosixPath(
            name.replace("\\", "/")
        ).name.casefold()
        for name in member_names
    )

    suffixes = {
        PurePosixPath(name).suffix.casefold()
        for name in basenames
    }

    # Trakt raw exports are ZIP bundles of JSON documents such as
    # watched-history-*.json, ratings-movies*.json, lists-watchlist*.json.
    if suffixes == {".json"}:
        matched = sorted(
            {
                basename
                for basename in basenames
                if any(
                    basename.startswith(prefix)
                    for prefix in TRAKT_PREFIXES
                )
            }
        )

        if matched:
            return DetectedImport(
                source="trakt",
                inspection=inspection,
                evidence=tuple(matched[:8]),
            )

    # Letterboxd account exports are ZIP bundles of CSV documents with
    # stable functional names such as watched.csv, ratings.csv and diary.csv.
    if suffixes == {".csv"}:
        matched = sorted(
            set(basenames)
            & LETTERBOXD_BASENAMES
        )

        if len(matched) >= 2:
            return DetectedImport(
                source="letterboxd",
                inspection=inspection,
                evidence=tuple(matched),
            )

    raise ImportDetectionError(
        "Validated ZIP archive does not match a supported tracker export."
    )


def _detect_csv(
    path: Path,
    inspection: InspectionResult,
) -> DetectedImport:
    header = _read_csv_header(path)
    folded = {
        value.casefold(): value
        for value in header
    }

    keys = set(folded)

    # IMDb exports consistently expose Const plus title-oriented metadata.
    if (
        "const" in keys
        and (
            "title" in keys
            or "original title" in keys
        )
        and (
            "your rating" in keys
            or "position" in keys
            or "url" in keys
            or "title type" in keys
        )
    ):
        return DetectedImport(
            source="imdb",
            inspection=inspection,
            evidence=tuple(header),
        )

    # Letterboxd CSVs identify their own URI and use Name/Year-oriented
    # columns.
    if (
        "letterboxd uri" in keys
        and "name" in keys
    ):
        return DetectedImport(
            source="letterboxd",
            inspection=inspection,
            evidence=tuple(header),
        )

    # Simkl's documented CSV format exposes its own IDs and status/progress
    # fields.
    simkl_identity = {
        "simkl_id",
        "tmdb_id",
        "imdb_id",
        "tvdb_id",
    }

    simkl_state = {
        "watchlist",
        "lastepwatched",
        "watcheddate",
        "rating",
    }

    if (
        keys & simkl_identity
        and keys & simkl_state
        and (
            "type" in keys
            or "title" in keys
        )
    ):
        return DetectedImport(
            source="simkl",
            inspection=inspection,
            evidence=tuple(header),
        )

    raise ImportDetectionError(
        "Validated CSV does not match a supported tracker export."
    )


def _detect_json(
    path: Path,
    inspection: InspectionResult,
) -> DetectedImport:
    name = path.name.casefold()

    if any(
        name.startswith(prefix)
        for prefix in TRAKT_PREFIXES
    ):
        return DetectedImport(
            source="trakt",
            inspection=inspection,
            evidence=(path.name,),
        )

    raise ImportDetectionError(
        "Validated JSON does not match a supported tracker export."
    )


def _read_csv_header(
    path: Path,
) -> tuple[str, ...]:
    data = path.read_bytes()

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # This should already have been blocked by the security layer.
        raise ImportSecurityError(
            "not_utf8",
            "Import CSV must be valid UTF-8.",
        ) from exc

    try:
        row = next(
            csv.reader(
                io.StringIO(text, newline=""),
                strict=True,
            )
        )
    except (StopIteration, csv.Error) as exc:
        raise ImportDetectionError(
            "Import CSV has no readable header."
        ) from exc

    return tuple(
        value.strip()
        for value in row
        if value.strip()
    )
