from __future__ import annotations

import unittest
from dataclasses import replace

from imports.db_preview import (
    ExistingUserSnapshot,
    build_database_preview,
)
from imports.models import NormalizedImportRecord
from imports.resolve import (
    ResolutionCandidate,
    ResolutionPreview,
    ResolvedImportItem,
)
from imports.selective import build_selective_import_preview
from tests.test_import_resolution import item


def resolved(
    media_type,
    tmdb_id,
    title,
    *,
    record_count=1,
    **fields,
):
    record = NormalizedImportRecord(
        source="simkl",
        media_type=media_type,
        title=title,
        year=2020,
        tmdb_id=tmdb_id,
        watched=True,
        **fields,
    )

    candidate = ResolutionCandidate(
        media_type=(
            "movie" if media_type == "movie" else "tv"
        ),
        tmdb_id=tmdb_id,
        title=title,
        year="2020",
    )

    return ResolvedImportItem(
        plan_item=replace(
            item(record),
            source_record_count=record_count,
        ),
        status="resolved",
        method="tmdb_id",
        resolved=candidate,
        candidates=(candidate,),
        reason="Synthetic verified identity.",
    )


def unresolved(**fields):
    record = NormalizedImportRecord(
        source="simkl",
        **fields,
    )

    return ResolvedImportItem(
        plan_item=item(record),
        status="unresolved",
        method="imdb_id",
        resolved=None,
        candidates=(),
        reason="Synthetic unresolved identity.",
    )


def resolution(*items):
    return ResolutionPreview(
        source="simkl",
        raw_record_count=sum(
            value.plan_item.source_record_count
            for value in items
        ),
        items=tuple(items),
    )


def empty_snapshot():
    return ExistingUserSnapshot(
        user_id=1,
        states={},
        ratings={},
        episodes={},
    )


class SelectiveImportTests(unittest.TestCase):
    def test_clean_import_retains_every_record(self):
        source = resolution(
            resolved("movie", 100, "Film"),
            resolved(
                "tv", 200, "Series",
                status="completed",
            ),
        )

        preview = build_selective_import_preview(
            source, empty_snapshot()
        )

        self.assertEqual(preview.full, preview.eligible)
        self.assertEqual(preview.excluded, ())

    def test_unresolved_record_does_not_block_unrelated_movie(self):
        good = resolved("movie", 100, "Film")
        bad = unresolved(
            media_type="tv",
            title="Missing Series",
            imdb_id="tt1000001",
        )

        preview = build_selective_import_preview(
            resolution(good, bad),
            empty_snapshot(),
        )

        self.assertEqual(preview.full.review_count, 1)
        self.assertEqual(preview.eligible.review_count, 0)
        self.assertEqual(preview.eligible.media_add_count, 1)
        self.assertEqual(len(preview.excluded), 1)
        self.assertEqual(
            preview.excluded[0].resolution, bad
        )

    def test_blocked_parent_excludes_all_series_episodes(self):
        parent = resolved(
            "tv", 200, "Series",
            status="dropped",
        )
        episode = resolved(
            "episode", 200, "Series",
            season=1,
            episode=1,
        )
        movie = resolved("movie", 100, "Film")

        preview = build_selective_import_preview(
            resolution(parent, episode, movie),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.episode_add_count, 0)
        self.assertEqual(preview.eligible.state_add_count, 1)
        self.assertEqual(preview.eligible.review_count, 0)

    def test_unresolved_parent_links_through_source_identifier(self):
        parent = unresolved(
            media_type="tv",
            title="Alternate Series Name",
            imdb_id="tt1000001",
        )
        episode = resolved(
            "episode", 200, "Series",
            imdb_id="tt1000001",
            season=1,
            episode=1,
        )

        preview = build_selective_import_preview(
            resolution(parent, episode),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.items, ())

    def test_duplicate_identity_excludes_entire_media_group(self):
        first = resolved("movie", 100, "Film")
        duplicate = resolved("movie", 100, "Film")
        unrelated = resolved("movie", 101, "Other Film")

        preview = build_selective_import_preview(
            resolution(first, duplicate, unrelated),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.review_count, 0)
        self.assertEqual(preview.eligible.media_add_count, 1)

    def test_movie_and_tv_namespaces_remain_separate(self):
        movie = resolved("movie", 200, "Film")
        series = resolved(
            "tv", 200, "Series",
            status="dropped",
        )

        preview = build_selective_import_preview(
            resolution(movie, series),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 1)
        self.assertEqual(len(preview.eligible.items), 1)
        self.assertEqual(
            preview.eligible.items[0].media_type,
            "movie",
        )

    def test_all_blocked_produces_empty_subset(self):
        source = resolution(
            unresolved(
                media_type="movie",
                title="Missing Film",
            ),
            unresolved(
                media_type="tv",
                title="Missing Series",
            ),
        )

        preview = build_selective_import_preview(
            source, empty_snapshot()
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.items, ())
        self.assertEqual(preview.eligible.raw_record_count, 0)

    def test_subset_is_replanned_with_exact_source_counts(self):
        good = resolved(
            "movie", 100, "Film",
            record_count=4,
        )
        bad = resolved(
            "tv", 200, "Series",
            status="on_hold",
            record_count=3,
        )
        snapshot = empty_snapshot()

        preview = build_selective_import_preview(
            resolution(good, bad),
            snapshot,
        )

        expected = build_database_preview(
            resolution(good),
            snapshot,
        )

        self.assertEqual(preview.full.raw_record_count, 7)
        self.assertEqual(preview.eligible.raw_record_count, 4)
        self.assertEqual(preview.eligible, expected)
        self.assertEqual(snapshot.states, {})
        self.assertEqual(snapshot.ratings, {})
        self.assertEqual(snapshot.episodes, {})
        self.assertEqual(snapshot.media, frozenset())


if __name__ == "__main__":
    unittest.main()
