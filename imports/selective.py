from __future__ import annotations

from dataclasses import dataclass

from imports.db_preview import (
    ExistingUserSnapshot,
    ImportDatabasePreview,
    ImportDbPreviewItem,
    build_database_preview,
)
from imports.resolve import (
    ResolutionPreview,
    ResolvedImportItem,
)


IDENTIFIER_FIELDS = (
    "tmdb_id",
    "imdb_id",
    "tvdb_id",
    "trakt_id",
    "simkl_id",
)

IdentityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class SelectiveImportPreview:
    full: ImportDatabasePreview
    eligible: ImportDatabasePreview
    excluded: tuple[ImportDbPreviewItem, ...]

    @property
    def excluded_raw_record_count(self) -> int:
        return sum(
            item.resolution.plan_item.source_record_count
            for item in self.excluded
        )

    @property
    def dependency_excluded_count(self) -> int:
        return sum(
            not item.needs_review
            for item in self.excluded
        )


def _media_namespaces(
    media_type: str,
) -> tuple[str, ...]:
    if media_type == "movie":
        return ("movie",)

    if media_type in {"tv", "season", "episode"}:
        return ("tv",)

    # An unknown media type must not evade a dependency
    # simply because its namespace cannot be established.
    return ("movie", "tv")


def _identity_value(
    value: object,
) -> str:
    return " ".join(
        str(value).casefold().split()
    )


def _dependency_keys(
    item: ResolvedImportItem,
) -> set[IdentityKey]:
    record = item.plan_item.proposed

    namespaces = set(
        _media_namespaces(record.media_type)
    )

    if item.resolved is not None:
        namespaces.add(item.resolved.media_type)

    for conflict in item.plan_item.conflicts:
        if conflict.field == "media_type":
            for value in conflict.values:
                namespaces.update(
                    _media_namespaces(value)
                )

    keys: set[IdentityKey] = set()

    def add_claim(field, value):
        if value is None:
            return

        normalized = _identity_value(value)

        if not normalized:
            return

        for namespace in namespaces:
            keys.add(
                (namespace, field, normalized)
            )

    # Include secondary identifiers, not only the strongest
    # identifier used by the original merge planner.
    for field in IDENTIFIER_FIELDS:
        add_claim(
            field,
            getattr(record, field),
        )

    # Titles are used only to establish exclusion dependencies.
    # They never authorize identity resolution or database writes.
    add_claim("title", record.title)

    for conflict in item.plan_item.conflicts:
        if conflict.field in IDENTIFIER_FIELDS or conflict.field == "title":
            for value in conflict.values:
                add_claim(
                    conflict.field,
                    value,
                )

    # Link canonical identities and possible review candidates.
    candidates = item.candidates

    if item.resolved is not None:
        candidates = (
            *candidates,
            item.resolved,
        )

    for candidate in candidates:
        keys.add(
            (
                candidate.media_type,
                "tmdb_id",
                str(candidate.tmdb_id),
            )
        )

    return keys


def _dependency_components(
    items: tuple[ResolvedImportItem, ...],
) -> list[int]:
    # Union-find avoids comparing every record with every
    # other record in large tracker exports.
    parents = list(range(len(items)))
    sizes = [1] * len(items)

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]

        return index

    def union(left, right):
        left = find(left)
        right = find(right)

        if left == right:
            return

        if sizes[left] < sizes[right]:
            left, right = right, left

        parents[right] = left
        sizes[left] += sizes[right]

    owners: dict[IdentityKey, int] = {}

    for index, item in enumerate(items):
        for key in _dependency_keys(item):
            previous = owners.setdefault(
                key,
                index,
            )

            union(index, previous)

    return [
        find(index)
        for index in range(len(items))
    ]


def build_selective_import_preview(
    resolution: ResolutionPreview,
    snapshot: ExistingUserSnapshot,
) -> SelectiveImportPreview:
    """Build a review-free, independently replanned import subset."""

    # Validate source accounting before either the clean-import
    # fast path or selective dependency processing.
    counts = tuple(
        item.plan_item.source_record_count
        for item in resolution.items
    )

    if (
        any(count <= 0 for count in counts)
        or sum(counts) != resolution.raw_record_count
    ):
        raise RuntimeError(
            "Import source record accounting failed."
        )

    full = build_database_preview(
        resolution,
        snapshot,
    )

    if not full.review_count:
        return SelectiveImportPreview(
            full=full,
            eligible=full,
            excluded=(),
        )

    components = _dependency_components(
        resolution.items
    )

    blocked = {
        components[index]
        for index, item in enumerate(full.items)
        if item.needs_review
    }

    included = tuple(
        item
        for index, item in enumerate(resolution.items)
        if components[index] not in blocked
    )

    excluded = tuple(
        item
        for index, item in enumerate(full.items)
        if components[index] in blocked
    )

    subset = ResolutionPreview(
        source=resolution.source,
        raw_record_count=sum(
            item.plan_item.source_record_count
            for item in included
        ),
        items=included,
    )

    # Recalculate from the original database snapshot.
    # Never reuse actions simulated by the full preview.
    eligible = build_database_preview(
        subset,
        snapshot,
    )

    if eligible.review_count:
        raise RuntimeError(
            "Selective import replanning produced review-required items."
        )

    result = SelectiveImportPreview(
        full=full,
        eligible=eligible,
        excluded=excluded,
    )

    if (
        eligible.raw_record_count
        + result.excluded_raw_record_count
        != full.raw_record_count
    ):
        raise RuntimeError(
            "Selective import source record accounting failed."
        )

    return result
