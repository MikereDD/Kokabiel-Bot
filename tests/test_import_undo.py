from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from imports import undo
from imports.db_apply import apply_database_import
from imports.journal import capture_personal_titles
from imports.models import NormalizedImportRecord

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class ImportUndoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.db = self.root / "test.db"
        self.backup = self.root / "undo.db"

        create_database(self.db)

        self.movie = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                title="Example Film",
                year=2000,
                watched=True,
                rating=9.0,
            ),
            media_type="movie",
            tmdb_id=348,
        )

        self.episode = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example Series",
                season=1,
                episode=1,
                watched=True,
                watched_at="2026-01-01T01:00:00Z",
            ),
            media_type="tv",
            tmdb_id=200,
        )

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(
            self.db
        )) as connection, connection:
            connection.execute(sql, params)

    def rows(self, sql, params=(), path=None):
        with closing(sqlite3.connect(
            path or self.db
        )) as connection:
            return connection.execute(
                sql, params
            ).fetchall()

    def snapshot(self, user_id=20, path=None):
        with closing(sqlite3.connect(
            path or self.db
        )) as connection:
            return capture_personal_titles(
                connection, user_id
            )

    def import_titles(self, *items, user_id=20):
        resolution = make_resolution(*items)

        approved = make_preview(
            self.db,
            resolution,
            user_id=user_id,
        )

        number = len(self.rows(
            "SELECT batch_id FROM import_batches"
        ))

        apply_database_import(
            approved,
            database_path=self.db,
            backup_path=self.root / f"import-{number}.db",
            user_name=f"User {user_id}",
            chat_id=user_id,
            origin_group_id=-100123,
        )

        return self.rows(
            """
            SELECT MAX(batch_id)
            FROM import_batches
            WHERE user_id = ?
            """,
            (user_id,),
        )[0][0]

    def preview(self, batch=1, user_id=20):
        return undo.preview_import_undo(
            self.db,
            user_id=user_id,
            batch_id=batch,
        )

    def apply(self, approved):
        return undo.apply_import_undo(
            approved,
            database_path=self.db,
            backup_path=self.backup,
        )

    def test_undo_new_movie_and_episode(self):
        batch = self.import_titles(
            self.movie, self.episode
        )

        before = self.snapshot()

        plan = self.preview(batch)

        self.assertEqual(plan.restore_count, 2)
        self.assertEqual(plan.skipped_count, 0)

        result = self.apply(plan)

        self.assertEqual(result.restored_count, 2)
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(
            self.snapshot(path=self.backup),
            before,
        )

        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM media"),
            [(2,)],
        )

        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM user_chats"),
            [(1,)],
        )

        self.assertEqual(
            self.backup.stat().st_mode & 0o777,
            0o600,
        )

        self.assertEqual(
            self.rows("PRAGMA integrity_check"),
            [("ok",)],
        )

        self.assertEqual(
            self.rows("PRAGMA foreign_key_check"),
            [],
        )

    def test_restores_preexisting_state_exactly(self):
        self.execute(
            """
            INSERT INTO media (
                media_type, tmdb_id, title, year
            ) VALUES ('movie', 348, 'Example Film', '2000')
            """
        )

        self.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id, user_name,
                watchlist, viewing_status, last_chat_id,
                created_at, updated_at
            ) VALUES (
                20, 'movie', 348, 'Old Name',
                1, 'none', -100999,
                '2020-01-01', '2021-01-01'
            )
            """
        )

        original = self.snapshot()

        self.import_titles(self.movie)
        self.apply(self.preview())

        self.assertEqual(
            self.snapshot(), original
        )

    def test_partial_undo_preserves_manual_activity(self):
        self.import_titles(
            self.movie, self.episode
        )

        self.execute(
            "UPDATE ratings SET rating = 7 WHERE user_id = 20"
        )

        retained = self.snapshot()[("movie", 348)]

        plan = self.preview()

        self.assertEqual(plan.restore_count, 1)
        self.assertEqual(plan.skipped_count, 1)

        result = self.apply(plan)

        self.assertEqual(result.restored_count, 1)
        self.assertEqual(result.skipped_count, 1)

        self.assertEqual(
            self.snapshot(),
            {("movie", 348): retained},
        )

        self.assertEqual(
            self.rows(
                """
                SELECT media_type, invalidated
                FROM import_title_changes
                ORDER BY media_type
                """
            ),
            [("movie", 1), ("tv", 0)],
        )

    def test_other_member_and_unrelated_title_preserved(self):
        batch = self.import_titles(self.movie)

        self.import_titles(
            self.movie, user_id=99
        )

        other = self.snapshot(99)

        self.execute(
            """
            INSERT INTO media (
                media_type, tmdb_id, title, year
            ) VALUES ('movie', 999, 'Unrelated', '2000')
            """
        )

        self.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id, user_name,
                watchlist
            ) VALUES (20, 'movie', 999, 'User 20', 1)
            """
        )

        retained = self.snapshot()[("movie", 999)]

        self.apply(self.preview(batch))

        self.assertEqual(
            self.snapshot(99), other
        )

        self.assertEqual(
            self.snapshot(),
            {("movie", 999): retained},
        )

    def test_other_member_cannot_preview_batch(self):
        self.import_titles(self.movie)

        with self.assertRaises(
            undo.ImportUndoUnavailable
        ):
            self.preview(user_id=99)

        self.assertFalse(self.backup.exists())

    def test_stale_preview_rejected(self):
        self.import_titles(self.movie)

        plan = self.preview()

        self.execute(
            """
            UPDATE ratings SET rating = 8
            WHERE user_id = 20
            """
        )

        before = self.snapshot()

        with self.assertRaises(
            undo.ImportUndoStaleError
        ):
            self.apply(plan)

        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.backup.exists())

    def test_all_invalidated_is_safe_noop(self):
        self.import_titles(self.movie)

        self.execute(
            """
            UPDATE user_media_state
            SET watchlist = watchlist
            WHERE user_id = 20
            """
        )

        before = self.snapshot()

        result = self.apply(self.preview())

        self.assertEqual(result.restored_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.backup.exists())

    def test_completed_undo_cannot_replay(self):
        self.import_titles(self.movie)

        plan = self.preview()

        self.apply(plan)

        with self.assertRaises(
            undo.ImportUndoUnavailable
        ):
            self.preview()

        with self.assertRaises(
            undo.ImportUndoUnavailable
        ):
            self.apply(plan)

    def test_corrupted_cross_user_image_rejected(self):
        self.import_titles(self.movie)

        raw = self.rows(
            """
            SELECT after_json
            FROM import_title_changes
            """
        )[0][0]

        image = json.loads(raw)
        image["state"][0] = 99

        self.execute(
            """
            UPDATE import_title_changes
            SET before_json = ?
            """,
            (json.dumps(image),),
        )

        with self.assertRaises(
            undo.ImportUndoError
        ):
            self.preview()

        self.assertFalse(self.backup.exists())

    def test_failure_after_restoration_rolls_back(self):
        self.import_titles(
            self.movie, self.episode
        )

        before = self.snapshot()

        journal = self.rows(
            "SELECT * FROM import_title_changes"
        )

        plan = self.preview()

        original = undo._restore_title

        def fail(connection, user_id, title):
            original(connection, user_id, title)

            raise RuntimeError("Synthetic undo failure")

        with patch.object(
            undo, "_restore_title", side_effect=fail
        ):
            with self.assertRaises(RuntimeError):
                self.apply(plan)

        self.assertEqual(self.snapshot(), before)

        self.assertEqual(
            self.rows(
                "SELECT * FROM import_title_changes"
            ),
            journal,
        )

        self.assertEqual(
            self.rows(
                "SELECT undone_at FROM import_batches"
            ),
            [(None,)],
        )

        self.assertTrue(self.backup.is_file())

    def test_database_change_during_backup_rejected(self):
        self.import_titles(self.movie)

        plan = self.preview()

        original = undo._create_verified_backup

        def backup_then_change(database, backup):
            identity = original(database, backup)

            self.execute(
                """
                UPDATE ratings SET rating = 6
                WHERE user_id = 20
                """
            )

            return identity

        with patch.object(
            undo,
            "_create_verified_backup",
            side_effect=backup_then_change,
        ):
            with self.assertRaises(
                undo.ImportUndoStaleError
            ):
                self.apply(plan)

        self.assertEqual(
            self.rows("SELECT rating FROM ratings"),
            [(6,)],
        )

        self.assertFalse(self.backup.exists())

    def test_existing_backup_never_overwritten(self):
        self.import_titles(self.movie)

        before = self.snapshot()

        self.backup.write_bytes(b"protected sentinel")

        from imports.db_apply import ImportBackupError

        with self.assertRaises(ImportBackupError):
            self.apply(self.preview())

        self.assertEqual(
            self.backup.read_bytes(),
            b"protected sentinel",
        )

        self.assertEqual(self.snapshot(), before)



    def test_overlapping_imports_preserve_earlier_activity(self):
        first = self.import_titles(self.episode)

        original = self.snapshot()

        second_episode = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example Series",
                season=1,
                episode=2,
                watched=True,
                watched_at="2026-01-02T01:00:00Z",
            ),
            media_type="tv",
            tmdb_id=200,
        )

        second = self.import_titles(second_episode)

        self.assertNotEqual(first, second)

        older = self.preview(first)

        self.assertEqual(older.restore_count, 0)
        self.assertEqual(older.skipped_count, 1)

        newer = self.preview(second)

        self.assertEqual(newer.restore_count, 1)

        self.apply(newer)

        self.assertEqual(self.snapshot(), original)

        # Reversing the newer import must not silently
        # reactivate an older invalidated journal.

        older = self.preview(first)

        self.assertEqual(older.restore_count, 0)
        self.assertEqual(older.skipped_count, 1)




    def test_read_only_progress_sync_preserves_undo_eligibility(self):
        import asyncio

        from database import Database
        from tmdb import TmdbClient

        batch = self.import_titles(self.episode)

        before = self.snapshot()
        approved = self.preview(batch)

        self.assertEqual(
            approved.restore_count, 1
        )

        details = TmdbClient(
            None, "synthetic", region="US"
        )._parse_details(
            "tv",
            {
                "id": 200,
                "name": "Example Series",
                "first_air_date": "2010-01-01",
                "status": "Ended",
                "number_of_seasons": 1,
                "number_of_episodes": 5,
            },
        )

        # Merely opening progress from a different chat,
        # or with a changed display name, must not modify
        # any persisted personal fields.

        state = asyncio.run(
            Database(self.db).sync_series_progress(
                -100999,
                details,
                20,
                "Changed Display Name",
                {(1, e) for e in range(1, 6)},
            )
        )

        self.assertEqual(
            state.viewing_status, "watching"
        )

        self.assertEqual(
            state.watched_episodes, 1
        )

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.preview(batch), approved)

        result = self.apply(approved)

        self.assertEqual(result.restored_count, 1)
        self.assertEqual(self.snapshot(), {})

    def test_real_progress_transition_invalidates_undo(self):
        import asyncio

        from database import Database
        from tmdb import TmdbClient

        batch = self.import_titles(self.episode)

        details = TmdbClient(
            None, "synthetic", region="US"
        )._parse_details(
            "tv",
            {
                "id": 200,
                "name": "Example Series",
                "first_air_date": "2010-01-01",
                "status": "Ended",
                "number_of_seasons": 1,
                "number_of_episodes": 1,
            },
        )

        # Unlike a no-op view, this synchronization
        # calculates a different persisted viewing status.

        state = asyncio.run(
            Database(self.db).sync_series_progress(
                -100123,
                details,
                20,
                "User 20",
                {(1, 1)},
            )
        )

        self.assertEqual(
            state.viewing_status, "completed"
        )

        plan = self.preview(batch)

        self.assertEqual(plan.restore_count, 0)
        self.assertEqual(plan.skipped_count, 1)

        self.assertEqual(
            self.rows(
                """
                SELECT invalidated
                FROM import_title_changes
                WHERE batch_id = ?
                """,
                (batch,),
            ),
            [(1,)],
        )


if __name__ == "__main__":
    unittest.main()
