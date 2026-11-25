from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest

from concurrent.futures import ThreadPoolExecutor
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


class ImportBackupDeadlineTests(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.database = self.root / "synthetic.db"
        self.backup = self.root / "preimport.db"
        self.historical = self.root / "historical.db"

        create_database(self.database)

        # An exclusive lock must block backup reads.
        # Use rollback-journal mode for this fixture.
        with closing(
            sqlite3.connect(self.database)
        ) as connection:
            mode = connection.execute(
                "PRAGMA journal_mode=DELETE"
            ).fetchone()[0]

            self.assertEqual(mode.casefold(), "delete")

        self.historical.write_bytes(
            b"historical-backup-sentinel"
        )

        resolution = make_resolution(
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
            resolution,
        )

    def snapshot(self, path=None):
        return load_existing_user_snapshot(
            path or self.database,
            user_id=1,
        )

    def apply(self):
        return writer.apply_database_import(
            self.approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Synthetic Tester",
            chat_id=123,
        )

    def assert_historical_preserved(self):
        self.assertEqual(
            self.historical.read_bytes(),
            b"historical-backup-sentinel",
        )

    def test_backup_deadline_under_real_sqlite_contention(self):
        before = self.snapshot()

        lock_ready = threading.Event()
        release_lock = threading.Event()

        real_connect = sqlite3.connect
        original_backup = writer._create_verified_backup

        def hold_exclusive_lock():
            with closing(
                real_connect(
                    self.database,
                    timeout=1,
                    isolation_level=None,
                )
            ) as blocker:

                blocker.execute("BEGIN EXCLUSIVE")
                lock_ready.set()

                try:
                    # Test-only watchdog: the RED candidate
                    # cannot remain blocked indefinitely.
                    released = release_lock.wait(2.0)

                    return released

                finally:
                    blocker.rollback()

        def short_connect(*args, **kwargs):
            kwargs["timeout"] = 0.05

            return real_connect(
                *args,
                **kwargs,
            )

        with ThreadPoolExecutor(
            max_workers=1
        ) as executor:

            lock_future = None

            def backup_under_contention(database, backup):
                nonlocal lock_future

                # Inject contention only after writer
                # preflight has completed.
                lock_future = executor.submit(
                    hold_exclusive_lock
                )

                self.assertTrue(
                    lock_ready.wait(1),
                    "Synthetic lock was not acquired.",
                )

                return original_backup(
                    database,
                    backup,
                )

            try:
                with (
                    patch.object(
                        writer,
                        "BACKUP_DEADLINE_SECONDS",
                        0.15,
                        create=True,
                    ),
                    patch.object(
                        writer.sqlite3,
                        "connect",
                        side_effect=short_connect,
                    ),
                    patch.object(
                        writer,
                        "_create_verified_backup",
                        side_effect=backup_under_contention,
                    ),
                    patch.object(
                        writer,
                        "_apply_item",
                        wraps=writer._apply_item,
                    ) as mutate,
                ):

                    with self.assertRaises(
                        writer.ImportBackupError
                    ):
                        self.apply()

                    mutate.assert_not_called()

            finally:
                release_lock.set()

                if lock_future is not None:
                    released_before_watchdog = (
                        lock_future.result(timeout=3)
                    )

        self.assertTrue(
            released_before_watchdog,
            "Backup waited for the test watchdog "
            "instead of enforcing its own deadline.",
        )

        self.assertFalse(self.backup.exists())

        self.assertEqual(
            self.snapshot(),
            before,
        )

        self.assert_historical_preserved()

        # A fresh attempt must succeed once contention
        # has been removed.
        result = self.apply()

        self.assertFalse(result.already_applied)
        self.assertTrue(self.backup.is_file())

        self.assertEqual(
            self.snapshot(self.backup),
            before,
        )

        self.assert_historical_preserved()

    def test_uncontended_backup_preserves_preimport_state(self):
        before = self.snapshot()

        result = self.apply()

        self.assertFalse(result.already_applied)
        self.assertEqual(result.backup_path, self.backup)

        self.assertEqual(
            self.snapshot(self.backup),
            before,
        )

        self.assertNotEqual(
            self.snapshot(),
            before,
        )

        with closing(
            sqlite3.connect(self.backup)
        ) as connection:
            self.assertEqual(
                connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
                "ok",
            )

        self.assert_historical_preserved()


if __name__ == "__main__":
    unittest.main()
