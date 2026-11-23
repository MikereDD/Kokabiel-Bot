from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from imports.merge import (
    ImportPlan,
    ImportPlanItem,
)
from models import MediaType, SearchResult


ResolutionStatus = Literal[
    "resolved",
    "needs_review",
    "unresolved",
]

ResolutionMethod = Literal[
    "tmdb_id",
    "imdb_id",
    "title_year",
]


class ResolutionTmdb(Protocol):
    async def lookup_result(
        self,
        media_type: MediaType,
        tmdb_id: int,
    ) -> SearchResult: ...

    async def find_by_imdb(
        self,
        imdb_id: str,
        limit: int = 8,
    ) -> list[SearchResult]: ...

    async def search_movie(
        self,
        query: str,
        limit: int = 8,
    ) -> list[SearchResult]: ...

    async def search_tv(
        self,
        query: str,
        limit: int = 8,
    ) -> list[SearchResult]: ...

    async def search_multi(
        self,
        query: str,
        limit: int = 8,
    ) -> list[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str


@dataclass(frozen=True, slots=True)
class ResolvedImportItem:
    plan_item: ImportPlanItem
    status: ResolutionStatus
    method: ResolutionMethod | None
    resolved: ResolutionCandidate | None
    candidates: tuple[ResolutionCandidate, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    source: str
    raw_record_count: int
    items: tuple[ResolvedImportItem, ...]

    @property
    def resolved_count(self) -> int:
        return sum(
            item.status == "resolved"
            for item in self.items
        )

    @property
    def review_count(self) -> int:
        return sum(
            item.status == "needs_review"
            for item in self.items
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            item.status == "unresolved"
            for item in self.items
        )


IDENTITY_CONFLICT_FIELDS = frozenset(
    {
        "media_type",
        "title",
        "year",
        "tmdb_id",
        "imdb_id",
        "tvdb_id",
        "trakt_id",
        "simkl_id",
    }
)


async def resolve_import_plan(
    plan: ImportPlan,
    tmdb: ResolutionTmdb,
) -> ResolutionPreview:
    """
    Resolve an import plan to canonical TMDB identities.

    This is preview-only. It performs read-only TMDB queries and never writes
    to Kokabiel's database or modifies the import plan.
    """

    resolved_items = []

    for item in plan.items:
        resolved_items.append(
            await _resolve_item(
                item,
                tmdb,
            )
        )

    return ResolutionPreview(
        source=plan.source,
        raw_record_count=plan.raw_record_count,
        items=tuple(resolved_items),
    )


async def _resolve_item(
    item: ImportPlanItem,
    tmdb: ResolutionTmdb,
) -> ResolvedImportItem:
    record = item.proposed

    identity_conflicts = {
        conflict.field
        for conflict in item.conflicts
        if conflict.field
        in IDENTITY_CONFLICT_FIELDS
    }

    if identity_conflicts:
        return _review(
            item,
            reason=(
                "Source records contain conflicting identity data: "
                + ", ".join(
                    sorted(identity_conflicts)
                )
            ),
        )

    expected_type = _canonical_media_type(
        record.media_type
    )

    # A supplied TMDB ID is the strongest identity, but verify that it
    # actually exists in the expected TMDB namespace before accepting it.
    if record.tmdb_id is not None:
        if expected_type is None:
            return _review(
                item,
                method="tmdb_id",
                reason=(
                    "TMDB ID is present but the media type is unknown; "
                    "movie versus TV must be confirmed."
                ),
            )

        result = await tmdb.lookup_result(
            expected_type,
            record.tmdb_id,
        )

        if not _claimed_identity_consistent(
            item,
            result,
        ):
            return _review(
                item,
                method="tmdb_id",
                candidates=(result,),
                reason=(
                    "Supplied TMDB ID exists, but its title/year "
                    "conflicts with the export record."
                ),
            )

        return _resolved(
            item,
            result,
            method="tmdb_id",
            reason="Supplied TMDB ID verified directly.",
        )

    # IMDb IDs are exact external identifiers. TMDB /find is preferable to
    # fuzzy title matching whenever one is available.
    if record.imdb_id:
        results = await tmdb.find_by_imdb(
            record.imdb_id,
            limit=8,
        )

        candidates = _filter_type(
            results,
            expected_type,
        )

        if len(candidates) == 1:
            candidate = candidates[0]

            if not _claimed_identity_consistent(
                item,
                candidate,
            ):
                return _review(
                    item,
                    method="imdb_id",
                    candidates=(candidate,),
                    reason=(
                        "IMDb ID resolved uniquely, but the TMDB "
                        "title/year conflicts with the export record."
                    ),
                )

            return _resolved(
                item,
                candidate,
                method="imdb_id",
                reason="IMDb external ID resolved uniquely through TMDB.",
            )

        if len(candidates) > 1:
            return _review(
                item,
                method="imdb_id",
                candidates=candidates,
                reason=(
                    "IMDb external ID returned multiple compatible "
                    "TMDB identities."
                ),
            )

        # Do not automatically replace a supplied-but-unresolved IMDb ID
        # with a fuzzy title match. Search may assist manual review only.
        fallback = await _title_candidates(
            item,
            tmdb,
            expected_type,
        )

        if fallback:
            return _review(
                item,
                method="title_year",
                candidates=fallback,
                reason=(
                    "IMDb ID did not resolve through TMDB; title/year "
                    "matches are shown for manual review only."
                ),
            )

        return _unresolved(
            item,
            method="imdb_id",
            reason="IMDb ID did not resolve to a compatible TMDB identity.",
        )

    # No strong external identity exists. Title/year can auto-resolve only
    # when there is exactly one exact compatible result.
    candidates = await _title_candidates(
        item,
        tmdb,
        expected_type,
    )

    if not candidates:
        return _unresolved(
            item,
            method="title_year",
            reason="No compatible TMDB title/year candidates were found.",
        )

    exact = tuple(
        candidate
        for candidate in candidates
        if _exact_title_year_match(
            item,
            candidate,
        )
    )

    if len(exact) == 1:
        return _resolved(
            item,
            exact[0],
            method="title_year",
            reason="Unique exact title/year match.",
        )

    return _review(
        item,
        method="title_year",
        candidates=(
            exact
            if exact
            else candidates
        ),
        reason=(
            "Title/year search was not uniquely exact; "
            "manual confirmation is required."
        ),
    )


async def _title_candidates(
    item: ImportPlanItem,
    tmdb: ResolutionTmdb,
    expected_type: MediaType | None,
) -> tuple[SearchResult, ...]:
    record = item.proposed

    if not record.title:
        return ()

    # IMDb episode rows can contain an episode title rather than a parent
    # series title. Never auto-search those as if they were TV series.
    if (
        record.source == "imdb"
        and record.media_type == "episode"
    ):
        return ()

    if expected_type == "movie":
        results = await tmdb.search_movie(
            record.title,
            limit=8,
        )

    elif expected_type == "tv":
        results = await tmdb.search_tv(
            record.title,
            limit=8,
        )

    else:
        results = await tmdb.search_multi(
            record.title,
            limit=8,
        )

    return tuple(results[:8])


def _canonical_media_type(
    media_type: str,
) -> MediaType | None:
    if media_type == "movie":
        return "movie"

    if media_type in {
        "tv",
        "season",
        "episode",
    }:
        return "tv"

    return None


def _filter_type(
    results: list[SearchResult],
    expected_type: MediaType | None,
) -> tuple[SearchResult, ...]:
    if expected_type is None:
        return tuple(results)

    return tuple(
        result
        for result in results
        if result.media_type == expected_type
    )


def _exact_title_year_match(
    item: ImportPlanItem,
    candidate: SearchResult,
) -> bool:
    record = item.proposed

    if not record.title:
        return False

    wanted = _normalize_title(
        record.title
    )

    titles = {
        _normalize_title(
            candidate.title
        ),
        _normalize_title(
            candidate.original_title
        ),
    }

    if wanted not in titles:
        return False

    # Title alone is not strong enough for automatic identity resolution.
    # Remakes, re-releases and similarly named titles must remain reviewable.
    if record.year is None:
        return False

    return candidate.year == str(
        record.year
    )


def _claimed_identity_consistent(
    item: ImportPlanItem,
    candidate: SearchResult,
) -> bool:
    record = item.proposed

    if record.title:
        wanted = _normalize_title(
            record.title
        )

        candidate_titles = {
            _normalize_title(
                candidate.title
            ),
            _normalize_title(
                candidate.original_title
            ),
        }

        if wanted not in candidate_titles:
            return False

    if (
        record.year is not None
        and candidate.year != str(
            record.year
        )
    ):
        return False

    return True


def _normalize_title(
    value: str,
) -> str:
    return " ".join(
        value.casefold().split()
    )


def _candidate(
    result: SearchResult,
) -> ResolutionCandidate:
    return ResolutionCandidate(
        media_type=result.media_type,
        tmdb_id=result.tmdb_id,
        title=result.title,
        year=result.year,
    )


def _resolved(
    item: ImportPlanItem,
    result: SearchResult,
    *,
    method: ResolutionMethod,
    reason: str,
) -> ResolvedImportItem:
    candidate = _candidate(result)

    return ResolvedImportItem(
        plan_item=item,
        status="resolved",
        method=method,
        resolved=candidate,
        candidates=(candidate,),
        reason=reason,
    )


def _review(
    item: ImportPlanItem,
    *,
    reason: str,
    method: ResolutionMethod | None = None,
    candidates=(),
) -> ResolvedImportItem:
    converted = tuple(
        _candidate(candidate)
        for candidate in candidates
    )

    return ResolvedImportItem(
        plan_item=item,
        status="needs_review",
        method=method,
        resolved=None,
        candidates=converted,
        reason=reason,
    )


def _unresolved(
    item: ImportPlanItem,
    *,
    method: ResolutionMethod,
    reason: str,
) -> ResolvedImportItem:
    return ResolvedImportItem(
        plan_item=item,
        status="unresolved",
        method=method,
        resolved=None,
        candidates=(),
        reason=reason,
    )
