from __future__ import annotations

import sqlite3
import tempfile
import unittest

from pathlib import Path

from imports.db_apply import (
    ImportPreviewRejectedError,
    ImportPreviewStaleError,
    apply_database_import,
)
from imports.db_preview import load_existing_user_snapshot
from imports.selective import build_selective_import_preview

from tests.test_import_db_apply import create_database
from tests.test_import_selective import (
    resolution,
    resolved,
    unresolved,
)


class SelectiveImportApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        self.database = self.root / "kokabiel.db"
        self.backup = self.root / "backup.db"

        create_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, database=None):
        return load_existing_user_snapshot(
            database or self.database,
            user_id=1,
        )

    def source(self):
        return resolution(
            resolved(
                "movie", 100, "Verified Film",
                rating=9.0,
            ),
            resolved(
                "tv", 200, "Excluded Series",
                status="dropped",
                rating=8.0,
            ),
            resolved(
                "episode", 200, "Excluded Series",
                season=1,
                episode=1,
            ),
            unresolved(
                media_type="movie",
                title="Missing Film",
                imdb_id="tt1000001",
            ),
            resolved(
                "tv", 300, "Verified Series",
                status="completed",
            ),
            resolved(
                "episode", 300, "Verified Series",
                season=1,
                episode=2,
            ),
        )

    def preview(self):
        return build_selective_import_preview(
            self.source(),
            self.snapshot(),
        )

    def apply(self, approved, backup=None):
        return apply_database_import(
            approved,
            database_path=self.database,
            backup_path=backup or self.backup,
            user_name="Synthetic Tester",
            chat_id=123,
        )

    def assert_database_integrity(self):
        connection = sqlite3.connect(self.database)

        try:
            self.assertEqual(
                connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone(),
                ("ok",),
            )

            self.assertEqual(
                connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(),
                [],
            )

        finally:
            connection.close()

    def test_only_verified_subset_is_written(self):
        before = self.snapshot()
        preview = self.preview()

        self.assertEqual(preview.full.review_count, 2)
        self.assertEqual(len(preview.excluded), 3)
        self.assertEqual(preview.eligible.review_count, 0)

        result = self.apply(preview.eligible)

        self.assertFalse(result.already_applied)
        self.assertEqual(result.media_added, 2)
        self.assertEqual(result.states_added, 2)
        self.assertEqual(result.states_updated, 0)
        self.assertEqual(result.episodes_added, 1)
        self.assertEqual(result.ratings_added, 1)

        after = self.snapshot()

        self.assertEqual(
            after.media,
            frozenset({
                ("movie", 100),
                ("tv", 300),
            }),
        )

        self.assertEqual(
            set(after.states),
            {
                ("movie", 100),
                ("tv", 300),
            },
        )

        self.assertEqual(
            after.states[("movie", 100)].viewing_status,
            "watched",
        )

        self.assertEqual(
            after.states[("tv", 300)].viewing_status,
            "completed",
        )

        self.assertEqual(
            after.episodes,
            {
                300: frozenset({(1, 2)}),
            },
        )

        self.assertEqual(
            set(after.ratings),
            {("movie", 100)},
        )

        self.assertEqual(
            after.ratings[("movie", 100)].rating,
            9,
        )

        self.assertTrue(self.backup.is_file())
        self.assertEqual(self.snapshot(self.backup), before)

        self.assert_database_integrity()

    def test_full_review_preview_remains_rejected(self):
        before = self.snapshot()
        preview = self.preview()

        with self.assertRaises(ImportPreviewRejectedError):
            self.apply(preview.full)

        self.assertFalse(self.backup.exists())
        self.assertEqual(self.snapshot(), before)

    def test_approved_subset_replay_is_idempotent(self):
        preview = self.preview()

        self.apply(preview.eligible)

        before = self.snapshot()

        result = self.apply(
            preview.eligible,
            self.root / "replay-backup.db",
        )

        self.assertTrue(result.already_applied)
        self.assertEqual(result.media_added, 0)
        self.assertEqual(result.states_added, 0)
        self.assertEqual(result.states_updated, 0)
        self.assertEqual(result.episodes_added, 0)
        self.assertEqual(result.ratings_added, 0)

        self.assertEqual(self.snapshot(), before)
        self.assert_database_integrity()

    def test_stale_subset_cannot_overwrite_new_rating(self):
        preview = self.preview()

        intervening = build_selective_import_preview(
            resolution(
                resolved(
                    "movie", 100, "Verified Film",
                    rating=6.0,
                )
            ),
            self.snapshot(),
        )

        self.apply(
            intervening.eligible,
            self.root / "intervening-backup.db",
        )

        before = self.snapshot()

        with self.assertRaises(ImportPreviewStaleError):
            self.apply(preview.eligible)

        self.assertEqual(self.snapshot(), before)

        self.assertEqual(
            before.ratings[("movie", 100)].rating,
            6,
        )

        self.assertNotIn(("tv", 300), before.media)
        self.assertEqual(before.episodes, {})

        self.assert_database_integrity()

    def test_reimport_retains_exclusions_without_duplicate_changes(self):
        first = self.preview()

        self.apply(first.eligible)

        second = self.preview()

        self.assertEqual(len(second.excluded), 3)
        self.assertEqual(second.eligible.review_count, 0)
        self.assertEqual(second.eligible.media_add_count, 0)
        self.assertEqual(second.eligible.state_add_count, 0)
        self.assertEqual(second.eligible.state_update_count, 0)
        self.assertEqual(second.eligible.episode_add_count, 0)
        self.assertEqual(second.eligible.rating_add_count, 0)


if __name__ == "__main__":
    unittest.main()
