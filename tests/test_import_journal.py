from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path

from database import Database
from imports.db_apply import apply_database_import
from imports.models import NormalizedImportRecord

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class ImportJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.db = self.root / "kokabiel.db"
        self.backup = self.root / "backup.db"

        create_database(self.db)

        self.resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

    def execute(self, sql, parameters=()):
        with closing(
            sqlite3.connect(self.db)
        ) as connection, connection:
            connection.execute(sql, parameters)

    def rows(self, sql, parameters=(), path=None):
        with closing(
            sqlite3.connect(path or self.db)
        ) as connection:
            return connection.execute(
                sql, parameters
            ).fetchall()

    def apply(self, user_id=20, approved=None):
        if approved is None:
            approved = make_preview(
                self.db,
                self.resolution,
                user_id=user_id,
            )

        return apply_database_import(
            approved,
            database_path=self.db,
            backup_path=self.backup,
            user_name="Synthetic Member",
            chat_id=user_id,
            origin_group_id=-100123,
        )

    def changes(self):
        return self.rows(
            """
            SELECT b.user_id, c.media_type, c.tmdb_id,
                   c.before_json, c.after_json,
                   c.invalidated
            FROM import_title_changes c
            JOIN import_batches b USING (batch_id)
            ORDER BY b.batch_id, c.media_type, c.tmdb_id
            """
        )

    def test_successful_import_records_exact_personal_delta(self):
        result = self.apply()

        self.assertFalse(result.already_applied)

        user_id, kind, tmdb_id, before, after, dirty = (
            self.changes()[0]
        )

        self.assertEqual(
            (user_id, kind, tmdb_id, dirty),
            (20, "movie", 348, 0),
        )

        self.assertIsNone(json.loads(before))

        recorded = json.loads(after)

        self.assertEqual(
            recorded["state"],
            list(self.rows(
                """
                SELECT user_id, media_type, tmdb_id,
                       user_name, watchlist, viewing_status,
                       last_chat_id, created_at, updated_at
                FROM user_media_state
                WHERE user_id = 20
                """
            )[0]),
        )

        self.assertEqual(
            recorded["rating"],
            list(self.rows(
                """
                SELECT user_id, media_type, tmdb_id,
                       user_name, rating, rated_at
                FROM ratings
                WHERE user_id = 20
                """
            )[0]),
        )

        self.assertEqual(recorded["episodes"], [])

        self.assertEqual(
            self.rows(
                """
                SELECT user_id, source, origin_group_id
                FROM import_batches
                """
            ),
            [(20, "imdb", -100123)],
        )

        self.assertEqual(
            self.rows(
                "SELECT COUNT(*) FROM import_batches",
                path=self.backup,
            ),
            [(0,)],
        )

    def test_existing_personal_state_preserved_in_before_image(self):
        self.execute(
            """
            INSERT INTO media (
                media_type, tmdb_id, title, year
            ) VALUES ('movie', 348, 'Alien', '1979')
            """
        )

        self.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id, user_name,
                watchlist, viewing_status, last_chat_id
            ) VALUES (
                20, 'movie', 348, 'Original Name',
                1, 'none', -100999
            )
            """
        )

        original = list(self.rows(
            """
            SELECT user_id, media_type, tmdb_id,
                   user_name, watchlist, viewing_status,
                   last_chat_id, created_at, updated_at
            FROM user_media_state
            WHERE user_id = 20
            """
        )[0])

        self.apply()

        change = self.changes()[0]

        self.assertEqual(
            json.loads(change[3])["state"],
            original,
        )

        self.assertNotEqual(
            json.loads(change[4])["state"],
            original,
        )

    def test_noop_replay_creates_no_second_batch(self):
        approved = make_preview(
            self.db,
            self.resolution,
            user_id=20,
        )

        self.apply(approved=approved)

        before = self.changes()

        self.backup = self.root / "replay.db"

        result = self.apply(approved=approved)

        self.assertTrue(result.already_applied)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.changes(), before)

    def test_same_value_manual_update_invalidates_title(self):
        self.apply()

        self.execute(
            """
            UPDATE user_media_state
            SET watchlist = watchlist
            WHERE user_id = 20
              AND media_type = 'movie'
              AND tmdb_id = 348
            """
        )

        self.assertEqual(self.changes()[0][-1], 1)

    def test_manual_rating_removal_invalidates_title(self):
        self.apply()

        self.execute(
            """
            DELETE FROM ratings
            WHERE user_id = 20
              AND media_type = 'movie'
              AND tmdb_id = 348
            """
        )

        self.assertEqual(self.changes()[0][-1], 1)

    def test_unrelated_user_and_title_do_not_invalidate(self):
        self.apply()

        self.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id, user_name
            ) VALUES (99, 'movie', 348, 'Other Member')
            """
        )

        self.execute(
            """
            INSERT INTO media (
                media_type, tmdb_id, title, year
            ) VALUES ('movie', 999, 'Other Film', '2000')
            """
        )

        self.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id, user_name
            ) VALUES (20, 'movie', 999, 'Synthetic Member')
            """
        )

        self.assertEqual(self.changes()[0][-1], 0)

    def test_movie_and_tv_identity_are_distinct(self):
        self.apply()

        self.execute(
            """
            INSERT INTO episode_progress (
                user_id, tmdb_id, season_number,
                episode_number
            ) VALUES (20, 348, 1, 1)
            """
        )

        self.assertEqual(self.changes()[0][-1], 0)

    def test_journal_failure_rolls_back_import(self):
        self.execute(
            """
            CREATE TRIGGER reject_journal
            BEFORE INSERT ON import_title_changes
            BEGIN
                SELECT RAISE(ABORT, 'synthetic failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.apply()

        for table in (
            "media",
            "user_media_state",
            "ratings",
            "episode_progress",
            "user_chats",
            "import_batches",
            "import_title_changes",
        ):
            self.assertEqual(
                self.rows(f"SELECT COUNT(*) FROM {table}"),
                [(0,)],
            )

        self.assertTrue(self.backup.is_file())

    def test_rollback_does_not_invalidate_existing_journal(self):
        self.apply()

        with closing(
            sqlite3.connect(self.db)
        ) as connection:
            connection.execute(
                """
                UPDATE ratings SET rating = 5
                WHERE user_id = 20
                """
            )

            connection.rollback()

        self.assertEqual(self.changes()[0][-1], 0)

    def test_initialization_preserves_journal_and_triggers(self):
        self.apply()

        before = self.changes()

        asyncio.run(Database(self.db).initialize())
        asyncio.run(Database(self.db).initialize())

        self.assertEqual(self.changes(), before)

        self.assertEqual(
            self.rows(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name LIKE 'import_journal_%'
                """
            ),
            [(9,)],
        )

        self.assertEqual(
            self.rows("PRAGMA integrity_check"),
            [("ok",)],
        )

        self.assertEqual(
            self.rows("PRAGMA foreign_key_check"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
