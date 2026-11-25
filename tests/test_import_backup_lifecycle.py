from __future__ import annotations

import os

import sqlite3
import stat
import tempfile
import unittest

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import imports.db_apply as writer

from imports.db_preview import load_existing_user_snapshot
from imports.models import NormalizedImportRecord

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class ImportBackupLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.root = Path(self.temp.name)
        self.database = self.root / "kokabiel.db"
        self.backup = self.root / "backup.db"

        create_database(self.database)

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
            self.database,
            self.resolution,
        )

    def snapshot(self, database=None):
        return load_existing_user_snapshot(
            database or self.database,
            user_id=1,
        )

    def apply(self, approved=None, backup=None):
        return writer.apply_database_import(
            approved or self.approved,
            database_path=self.database,
            backup_path=backup or self.backup,
            user_name="Synthetic Tester",
            chat_id=123,
        )

    def test_stale_preview_creates_no_backup(self):
        with closing(
            sqlite3.connect(self.database)
        ) as connection:
            connection.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year
                ) VALUES ('movie', 348, 'Alien', '1979')
                """
            )
            connection.commit()

        before = self.snapshot()

        with patch.object(
            writer,
            "_create_verified_backup",
            wraps=writer._create_verified_backup,
        ) as create_backup:
            with self.assertRaises(
                writer.ImportPreviewStaleError
            ):
                self.apply()

            create_backup.assert_not_called()

        self.assertFalse(self.backup.exists())
        self.assertEqual(self.snapshot(), before)

    def test_replay_creates_no_additional_backup(self):
        before = self.snapshot()

        first = self.apply()

        self.assertFalse(first.already_applied)
        self.assertTrue(self.backup.is_file())
        self.assertEqual(self.snapshot(self.backup), before)

        committed = self.snapshot()
        original_backup = self.backup.read_bytes()
        replay_path = self.root / "replay.db"

        with patch.object(
            writer,
            "_create_verified_backup",
            wraps=writer._create_verified_backup,
        ) as create_backup:
            replay = self.apply(backup=replay_path)

            self.assertTrue(replay.already_applied)
            create_backup.assert_not_called()

        self.assertIsNone(replay.backup_path)
        self.assertFalse(replay_path.exists())

        self.assertEqual(
            self.backup.read_bytes(),
            original_backup,
        )

        self.assertEqual(self.snapshot(), committed)

    def test_unchanged_preview_creates_no_backup(self):
        self.apply()

        unchanged = make_preview(
            self.database,
            self.resolution,
        )

        self.assertFalse(
            any(item.has_changes for item in unchanged.items)
        )

        before = self.snapshot()
        unused = self.root / "unchanged.db"

        with patch.object(
            writer,
            "_create_verified_backup",
            wraps=writer._create_verified_backup,
        ) as create_backup:
            result = self.apply(
                approved=unchanged,
                backup=unused,
            )

            create_backup.assert_not_called()

        self.assertIsNone(result.backup_path)
        self.assertFalse(unused.exists())

        self.assertEqual(
            (
                result.media_added,
                result.states_added,
                result.states_updated,
                result.episodes_added,
                result.ratings_added,
            ),
            (0, 0, 0, 0, 0),
        )

        self.assertEqual(self.snapshot(), before)

    def test_existing_backup_collision_is_classified(self):
        sentinel = b"Existing backup must not be modified"
        self.backup.write_bytes(sentinel)

        before = self.snapshot()

        with self.assertRaises(writer.ImportBackupError):
            self.apply()

        self.assertEqual(
            self.backup.read_bytes(),
            sentinel,
        )

        self.assertEqual(self.snapshot(), before)

    def test_locked_database_fails_before_backup(self):
        before = self.snapshot()

        real_connect = sqlite3.connect

        def short_timeout(*args, **kwargs):
            kwargs["timeout"] = 0.05
            return real_connect(*args, **kwargs)

        with closing(
            real_connect(self.database)
        ) as blocker:
            blocker.execute("BEGIN IMMEDIATE")

            try:
                with patch.object(
                    writer.sqlite3,
                    "connect",
                    side_effect=short_timeout,
                ), patch.object(
                    writer,
                    "_create_verified_backup",
                    wraps=writer._create_verified_backup,
                ) as create_backup:

                    with self.assertRaises(
                        writer.ImportApplyError
                    ):
                        self.apply()

                    create_backup.assert_not_called()
            finally:
                blocker.rollback()

        self.assertFalse(self.backup.exists())
        self.assertEqual(self.snapshot(), before)

    def test_backup_is_private_before_sqlite_opens_it(self):
        real_connect = sqlite3.connect
        observed = []

        def inspect_destination(*args, **kwargs):
            target = args[0] if args else kwargs.get("database")

            if target is not None and str(target) == str(self.backup):
                if not self.backup.exists():
                    observed.append(None)
                else:
                    observed.append(
                        stat.S_IMODE(
                            self.backup.stat().st_mode
                        )
                    )

            return real_connect(*args, **kwargs)

        with patch.object(
            writer.sqlite3,
            "connect",
            side_effect=inspect_destination,
        ):
            result = self.apply()

        self.assertFalse(result.already_applied)

        # Every platform must reserve the destination
        # before SQLite opens it.
        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0])

        # POSIX mode bits do not establish Windows ACLs.
        if os.name == "posix":
            self.assertEqual(observed, [0o600])

        self.assertTrue(self.backup.is_file())

    def test_dangling_backup_symlink_is_rejected(self):
        destination = self.root / "unexpected-target.db"

        try:
            self.backup.symlink_to(destination)

        except OSError as exc:
            if (
                os.name == "nt"
                and getattr(exc, "winerror", None) == 1314
            ):
                self.skipTest(
                    "Windows account lacks symlink creation privilege."
                )

            raise

        before = self.snapshot()

        with self.assertRaises(writer.ImportBackupError):
            self.apply()

        self.assertTrue(self.backup.is_symlink())
        self.assertFalse(destination.exists())

        self.assertEqual(self.snapshot(), before)


    def test_extended_missing_database(self):
        self.database.unlink()

        with patch.object(
            writer,
            "_create_verified_backup",
            wraps=writer._create_verified_backup,
        ) as create_backup:

            with self.assertRaises(writer.ImportPreWriteError):
                self.apply()

            create_backup.assert_not_called()

        self.assertFalse(self.database.exists())
        self.assertFalse(self.backup.exists())

    def test_extended_destination_open_failure(self):
        before = self.snapshot()

        sentinel = self.root / "historical-backup.db"
        sentinel.write_bytes(b"Historical backup sentinel")

        real_connect = sqlite3.connect

        def fail_destination(*args, **kwargs):
            target = args[0] if args else kwargs.get("database")

            if str(target) == str(self.backup):
                self.assertTrue(self.backup.exists())

                raise sqlite3.OperationalError(
                    "Synthetic destination open failure"
                )

            return real_connect(*args, **kwargs)

        with patch.object(
            writer.sqlite3,
            "connect",
            side_effect=fail_destination,
        ):
            with self.assertRaises(writer.ImportBackupError):
                self.apply()

        self.assertFalse(self.backup.exists())

        self.assertEqual(
            sentinel.read_bytes(),
            b"Historical backup sentinel",
        )

        self.assertEqual(self.snapshot(), before)

    def test_extended_contention_after_backup(self):
        before = self.snapshot()

        real_connect = sqlite3.connect
        real_backup = writer._create_verified_backup

        def short_timeout(*args, **kwargs):
            kwargs["timeout"] = 0.05
            return real_connect(*args, **kwargs)

        with closing(
            real_connect(self.database)
        ) as blocker:

            def backup_then_lock(database, backup):
                result = real_backup(database, backup)

                self.assertTrue(backup.is_file())

                blocker.execute("BEGIN IMMEDIATE")

                return result

            try:
                with patch.object(
                    writer.sqlite3,
                    "connect",
                    side_effect=short_timeout,
                ), patch.object(
                    writer,
                    "_create_verified_backup",
                    side_effect=backup_then_lock,
                ), patch.object(
                    writer,
                    "_apply_item",
                    wraps=writer._apply_item,
                ) as apply_item:

                    with self.assertRaises(
                        writer.ImportPreWriteError
                    ):
                        self.apply()

                    apply_item.assert_not_called()

            finally:
                blocker.rollback()

        self.assertFalse(self.backup.exists())
        self.assertEqual(self.snapshot(), before)

    def test_extended_unused_backup_cleanup_failure(self):
        before = self.snapshot()

        with patch.object(
            writer,
            "_expected_final_state",
            side_effect=RuntimeError(
                "Synthetic pre-mutation failure"
            ),
        ), patch.object(
            writer,
            "_remove_owned_backup",
            side_effect=writer.ImportBackupError(
                "Synthetic cleanup failure"
            ),
        ) as cleanup, patch.object(
            writer,
            "_apply_item",
            wraps=writer._apply_item,
        ) as apply_item:

            with self.assertRaises(writer.ImportBackupError):
                self.apply()

            cleanup.assert_called_once()
            apply_item.assert_not_called()

        # Failed cleanup must not result in database writes.
        # The unused verified backup remains recoverable.

        self.assertTrue(self.backup.is_file())
        self.assertEqual(self.snapshot(self.backup), before)
        self.assertEqual(self.snapshot(), before)

    def test_extended_verified_backup_replacement(self):
        before = self.snapshot()

        real_backup = writer._create_verified_backup

        preserved = self.root / "preserved-original.db"
        sentinel = b"Unexpected replacement file"

        def backup_then_replace(database, backup):
            result = real_backup(database, backup)

            backup.rename(preserved)
            backup.write_bytes(sentinel)

            return result

        with patch.object(
            writer,
            "_create_verified_backup",
            side_effect=backup_then_replace,
        ), patch.object(
            writer,
            "_apply_item",
            wraps=writer._apply_item,
        ) as apply_item:

            with self.assertRaises(writer.ImportBackupError):
                self.apply()

            apply_item.assert_not_called()

        # Neither the replacement nor the original verified
        # backup belongs to automatic cleanup at this point.

        self.assertEqual(self.backup.read_bytes(), sentinel)
        self.assertTrue(preserved.is_file())
        self.assertEqual(self.snapshot(preserved), before)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
