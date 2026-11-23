from __future__ import annotations

import unittest

from imports.merge import build_import_plan
from imports.models import NormalizedImportRecord
from imports.parse import ParsedImport


class ImportMergeTests(unittest.TestCase):
    def plan(self, *records):
        return build_import_plan(
            ParsedImport(
                source=records[0].source,
                records=records,
                source_files=tuple(
                    sorted(
                        {
                            record.source_file
                            for record in records
                            if record.source_file
                        }
                    )
                ),
            )
        )

    def test_letterboxd_watched_and_rating_collapse(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="Alien",
                year=1979,
                watched=True,
                source_file="watched.csv",
            ),
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="Alien",
                year=1979,
                watched=True,
                rating=9.0,
                rated_at="2026-01-01",
                source_file="ratings.csv",
            ),
        )

        self.assertEqual(
            result.raw_record_count,
            2,
        )
        self.assertEqual(
            len(result.items),
            1,
        )

        item = result.items[0]

        self.assertEqual(
            item.source_record_count,
            2,
        )
        self.assertTrue(
            item.proposed.watched
        )
        self.assertEqual(
            item.proposed.rating,
            9.0,
        )
        self.assertFalse(
            item.conflicts
        )

    def test_watched_state_clears_watchlist(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="imdb",
                media_type="movie",
                title="Alien",
                year=1979,
                imdb_id="tt0078748",
                watchlist=True,
                status="plan_to_watch",
                source_file="watchlist.csv",
            ),
            NormalizedImportRecord(
                source="imdb",
                media_type="movie",
                title="Alien",
                year=1979,
                imdb_id="tt0078748",
                watched=True,
                source_file="checkins.csv",
            ),
        )

        item = result.items[0]

        self.assertTrue(
            item.proposed.watched
        )
        self.assertFalse(
            item.proposed.watchlist
        )
        self.assertIsNone(
            item.proposed.status
        )

    def test_trakt_history_and_rating_merge_by_tmdb(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                title="Alien",
                year=1979,
                tmdb_id=348,
                watched=True,
                watched_at="2025-01-01T00:00:00Z",
                source_file="watched-history-1.json",
            ),
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                title="Alien",
                year=1979,
                tmdb_id=348,
                rating=10.0,
                rated_at="2026-01-01T00:00:00Z",
                source_file="ratings-movies.json",
            ),
        )

        self.assertEqual(
            len(result.items),
            1,
        )

        item = result.items[0]

        self.assertEqual(
            item.identity_basis,
            "tmdb",
        )
        self.assertTrue(
            item.proposed.watched
        )
        self.assertEqual(
            item.proposed.rating,
            10.0,
        )

    def test_conflicting_ratings_require_review(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                rating=8.0,
            ),
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                rating=10.0,
            ),
        )

        item = result.items[0]

        self.assertIsNone(
            item.proposed.rating
        )
        self.assertEqual(
            item.conflicts[0].field,
            "rating",
        )
        self.assertEqual(
            result.conflicted_item_count,
            1,
        )

    def test_episode_numbers_do_not_collapse(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example Show",
                tmdb_id=200,
                season=1,
                episode=1,
                watched=True,
            ),
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example Show",
                tmdb_id=200,
                season=1,
                episode=2,
                watched=True,
            ),
        )

        self.assertEqual(
            len(result.items),
            2,
        )

    def test_title_year_fallback_is_case_insensitive(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="Alien",
                year=1979,
                watched=True,
            ),
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="  ALIEN  ",
                year=1979,
                rating=9.0,
            ),
        )

        self.assertEqual(
            len(result.items),
            1,
        )
        self.assertEqual(
            result.items[0].identity_basis,
            "title_year",
        )

    def test_same_title_different_year_stays_separate(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="Example",
                year=1980,
            ),
            NormalizedImportRecord(
                source="letterboxd",
                media_type="movie",
                title="Example",
                year=2020,
            ),
        )

        self.assertEqual(
            len(result.items),
            2,
        )

    def test_conflicting_secondary_id_is_reported(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                imdb_id="tt0078748",
            ),
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                imdb_id="tt9999999",
            ),
        )

        fields = {
            conflict.field
            for conflict
            in result.items[0].conflicts
        }

        self.assertIn(
            "imdb_id",
            fields,
        )

    def test_completed_state_implies_watched(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="simkl",
                media_type="tv",
                tmdb_id=200,
                status="completed",
                watchlist=True,
            ),
        )

        item = result.items[0]

        self.assertTrue(
            item.proposed.watched
        )
        self.assertFalse(
            item.proposed.watchlist
        )
        self.assertEqual(
            item.proposed.status,
            "completed",
        )

    def test_latest_watched_timestamp_is_preserved(self) -> None:
        result = self.plan(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                watched=True,
                watched_at="2025-01-01T00:00:00Z",
            ),
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                tmdb_id=348,
                watched=True,
                watched_at="2026-01-01T00:00:00Z",
            ),
        )

        self.assertEqual(
            result.items[0].proposed.watched_at,
            "2026-01-01T00:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
