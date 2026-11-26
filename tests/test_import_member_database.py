from __future__ import annotations

import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path

from imports.db_apply import apply_database_import
from imports.models import NormalizedImportRecord

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class MemberImportDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.db = self.root / "kokabiel.db"
        self.backup = self.root / "backup.db"

        create_database(self.db)

        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """
                INSERT INTO user_chats (
                    chat_id, user_id, user_name
                ) VALUES (?, ?, ?)
                """,
                (-100999, 99, "Other Member"),
            )

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

        self.approved = make_preview(
            self.db,
            self.resolution,
            user_id=20,
        )

    def apply(self, **kwargs):
        return apply_database_import(
            self.approved,
            database_path=self.db,
            backup_path=self.backup,
            user_name="Synthetic Member",
            chat_id=20,
            **kwargs,
        )

    def rows(self, path, sql):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute(sql).fetchall()

    def associations(self, path=None):
        return self.rows(
            path or self.db,
            """
            SELECT chat_id, user_id, user_name
            FROM user_chats
            ORDER BY chat_id, user_id
            """,
        )

    def test_member_import_associates_originating_group(self):
        result = self.apply(origin_group_id=-100123)

        self.assertFalse(result.already_applied)

        self.assertEqual(
            self.associations(),
            [
                (-100999, 99, "Other Member"),
                (-100123, 20, "Synthetic Member"),
            ],
        )

        self.assertEqual(
            self.rows(
                self.db,
                """
                SELECT user_id, last_chat_id
                FROM user_media_state
                """,
            ),
            [(20, -100123)],
        )

        self.assertEqual(
            self.associations(self.backup),
            [(-100999, 99, "Other Member")],
        )

        self.assertEqual(
            self.rows(self.db, "PRAGMA integrity_check"),
            [("ok",)],
        )

        self.assertEqual(
            self.rows(self.db, "PRAGMA foreign_key_check"),
            [],
        )

    def test_owner_import_does_not_create_group_association(self):
        self.apply()

        self.assertEqual(
            self.associations(),
            [(-100999, 99, "Other Member")],
        )

        self.assertEqual(
            self.rows(
                self.db,
                "SELECT user_id, last_chat_id FROM user_media_state",
            ),
            [(20, 20)],
        )

    def test_invalid_group_rejected_before_mutation(self):
        with self.assertRaises(ValueError):
            self.apply(origin_group_id=20)

        self.assertFalse(self.backup.exists())

        self.assertEqual(
            self.rows(
                self.db,
                "SELECT COUNT(*) FROM user_media_state",
            ),
            [(0,)],
        )

    def test_association_failure_rolls_back_import(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript(
                """
                CREATE TRIGGER reject_member_association
                BEFORE INSERT ON user_chats
                WHEN NEW.user_id = 20
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic failure');
                END;
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(origin_group_id=-100123)

        for table in (
            "media",
            "user_media_state",
            "ratings",
            "episode_progress",
        ):
            self.assertEqual(
                self.rows(
                    self.db,
                    f"SELECT COUNT(*) FROM {table}",
                ),
                [(0,)],
            )

        self.assertEqual(
            self.associations(),
            [(-100999, 99, "Other Member")],
        )

    def test_replayed_import_does_not_associate_another_group(self):
        self.apply(origin_group_id=-100123)

        before = self.associations()

        self.backup = self.root / "replay.db"

        result = self.apply(origin_group_id=-100456)

        self.assertTrue(result.already_applied)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.associations(), before)


if __name__ == "__main__":
    unittest.main()
