from __future__ import annotations

import unittest
from dataclasses import replace

from imports.db_preview import ExistingRating
from imports.merge import ImportConflict
from imports.resolve import ResolutionCandidate
from imports.selective import build_selective_import_preview
from tests.test_import_selective import (
    empty_snapshot,
    resolution,
    resolved,
    unresolved,
)


class SelectiveImportBoundaryTests(unittest.TestCase):
    def test_transitive_identifier_dependencies(self):
        blocked = unresolved(
            media_type="tv",
            title="First Alias",
            imdb_id="tt1000001",
        )

        bridge = resolved(
            "tv", 200, "Second Alias",
            imdb_id="tt1000001",
            simkl_id=500,
            status="completed",
        )

        episode = resolved(
            "episode", 201, "Third Alias",
            simkl_id=500,
            season=1,
            episode=1,
        )

        unrelated = resolved(
            "movie", 100, "Unrelated Film"
        )

        preview = build_selective_import_preview(
            resolution(blocked, bridge, episode, unrelated),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 3)
        self.assertEqual(preview.dependency_excluded_count, 2)
        self.assertEqual(preview.eligible.review_count, 0)
        self.assertEqual(preview.eligible.episode_add_count, 0)
        self.assertEqual(
            preview.eligible.items[0].resolution,
            unrelated,
        )

    def test_conflicting_identifier_values_are_linked(self):
        blocked = unresolved(
            media_type="tv",
            title="Conflicting Series",
            imdb_id="tt1000001",
        )

        blocked = replace(
            blocked,
            plan_item=replace(
                blocked.plan_item,
                conflicts=(
                    ImportConflict(
                        field="imdb_id",
                        values=("tt1000001", "tt1000002"),
                        message="Conflicting external identifiers.",
                    ),
                ),
            ),
        )

        related = resolved(
            "tv", 200, "Alternate Series",
            imdb_id="tt1000002",
            status="completed",
        )

        preview = build_selective_import_preview(
            resolution(blocked, related),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.items, ())

    def test_ambiguous_candidates_exclude_matching_identities(self):
        blocked = unresolved(
            media_type="tv",
            title="Ambiguous Alias",
        )

        blocked = replace(
            blocked,
            status="needs_review",
            candidates=(
                ResolutionCandidate(
                    media_type="tv",
                    tmdb_id=200,
                    title="Series A",
                    year="2020",
                ),
                ResolutionCandidate(
                    media_type="tv",
                    tmdb_id=201,
                    title="Series B",
                    year="2020",
                ),
            ),
        )

        first = resolved(
            "tv", 200, "Series A",
            status="completed",
        )

        second = resolved(
            "tv", 201, "Series B",
            status="completed",
        )

        unrelated = resolved(
            "movie", 200, "Film"
        )

        preview = build_selective_import_preview(
            resolution(blocked, first, second, unrelated),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 3)
        self.assertEqual(len(preview.eligible.items), 1)
        self.assertEqual(
            preview.eligible.items[0].resolution,
            unrelated,
        )

    def test_rating_conflict_excludes_dependent_episodes(self):
        parent = resolved(
            "tv", 200, "Series",
            status="completed",
            rating=8.0,
        )

        episode = resolved(
            "episode", 200, "Series",
            season=1,
            episode=1,
        )

        snapshot = replace(
            empty_snapshot(),
            ratings={
                ("tv", 200): ExistingRating(
                    rating=6,
                    rated_at="2020-01-01",
                ),
            },
        )

        preview = build_selective_import_preview(
            resolution(parent, episode),
            snapshot,
        )

        self.assertEqual(preview.full.review_count, 1)
        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.items, ())
        self.assertEqual(snapshot.ratings[("tv", 200)].rating, 6)

    def test_normalized_title_links_dependencies(self):
        blocked = unresolved(
            media_type="tv",
            title="  EXAMPLE   SERIES ",
        )

        related = resolved(
            "episode", 200, "Example Series",
            season=1,
            episode=1,
        )

        preview = build_selective_import_preview(
            resolution(blocked, related),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 2)
        self.assertEqual(preview.eligible.items, ())

    def test_unknown_namespace_links_possible_movie_and_tv(self):
        blocked = unresolved(
            media_type="unknown",
            tmdb_id=200,
        )

        movie = resolved(
            "movie", 200, "Film"
        )

        series = resolved(
            "tv", 200, "Series",
            status="completed",
        )

        preview = build_selective_import_preview(
            resolution(blocked, movie, series),
            empty_snapshot(),
        )

        self.assertEqual(len(preview.excluded), 3)
        self.assertEqual(preview.eligible.items, ())

    def test_exclusion_preserves_exact_source_record_counts(self):
        blocked = resolved(
            "tv", 200, "Series",
            record_count=3,
            status="dropped",
        )

        episode = resolved(
            "episode", 200, "Series",
            record_count=2,
            season=1,
            episode=1,
        )

        eligible = resolved(
            "movie", 100, "Film",
            record_count=4,
        )

        preview = build_selective_import_preview(
            resolution(blocked, eligible, episode),
            empty_snapshot(),
        )

        self.assertEqual(preview.full.raw_record_count, 9)
        self.assertEqual(preview.eligible.raw_record_count, 4)
        self.assertEqual(preview.excluded_raw_record_count, 5)
        self.assertEqual(preview.dependency_excluded_count, 1)

        self.assertEqual(
            tuple(i.resolution for i in preview.excluded),
            (blocked, episode),
        )

    def test_invalid_source_count_rejected_for_partial_import(self):
        source = resolution(
            resolved("movie", 100, "Film"),
            unresolved(
                media_type="tv",
                title="Missing Series",
            ),
        )

        source = replace(
            source,
            raw_record_count=999,
        )

        with self.assertRaises(RuntimeError):
            build_selective_import_preview(
                source,
                empty_snapshot(),
            )

    def test_invalid_source_count_rejected_for_clean_import(self):
        source = resolution(
            resolved("movie", 100, "Film"),
        )

        source = replace(
            source,
            raw_record_count=999,
        )

        with self.assertRaises(RuntimeError):
            build_selective_import_preview(
                source,
                empty_snapshot(),
            )


    def test_nonpositive_record_counts_are_rejected(self):
        for count in (0, -1):
            with self.subTest(count=count):
                source = resolution(
                    resolved(
                        "movie", 100, "Film",
                        record_count=count,
                    ),
                    resolved(
                        "movie", 101, "Other Film",
                        record_count=3,
                    ),
                )

                with self.assertRaises(RuntimeError):
                    build_selective_import_preview(
                        source,
                        empty_snapshot(),
                    )

    def test_empty_import_preserves_zero_record_count(self):
        preview = build_selective_import_preview(
            resolution(),
            empty_snapshot(),
        )

        self.assertEqual(preview.full.raw_record_count, 0)
        self.assertEqual(preview.eligible.raw_record_count, 0)
        self.assertEqual(preview.eligible.items, ())
        self.assertEqual(preview.excluded, ())
        self.assertEqual(preview.excluded_raw_record_count, 0)


if __name__ == "__main__":
    unittest.main()
