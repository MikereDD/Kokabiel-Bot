from __future__ import annotations

import sqlite3
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from database import Database
from tests.test_import_db_apply import create_database


class DatabaseConnectionLifecycleTests(
    unittest.IsolatedAsyncioTestCase
):

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.path = (
            Path(temporary.name)
            / "synthetic.db"
        )

        create_database(self.path)

        self.database = Database(self.path)

        self.connections = []

        original_connect = self.database._connect
        real_sqlite_connect = sqlite3.connect

        def cross_thread_connect(*args, **kwargs):
            # Test-only relaxation allows the main test
            # thread to inspect executor-created connections.
            kwargs["check_same_thread"] = False

            return real_sqlite_connect(*args, **kwargs)

        def tracked_connect():
            with patch.object(
                sqlite3,
                "connect",
                side_effect=cross_thread_connect,
            ):
                connection = original_connect()

            self.connections.append(connection)

            # Clean up intentionally leaked connections
            # even when a RED assertion fails.
            self.addCleanup(connection.close)

            return connection

        self.patcher = patch.object(
            self.database,
            "_connect",
            side_effect=tracked_connect,
        )

        self.patcher.start()

        self.addCleanup(
            self.patcher.stop
        )

    def assert_connections_closed(self):
        self.assertTrue(
            self.connections,
            "No database connections were exercised.",
        )

        for connection in self.connections:
            # SQLite's context manager commits or rolls
            # back but does not close its connection.
            with self.assertRaisesRegex(
                sqlite3.ProgrammingError,
                "closed database",
            ):
                connection.execute("SELECT 1")

    async def test_initialize_closes_connection(self):
        await self.database.initialize()

        self.assert_connections_closed()

    async def test_read_closes_connection(self):
        count = await self.database.count_active_groups()

        self.assertEqual(count, 0)

        self.assert_connections_closed()

    async def test_write_closes_connection(self):
        await self.database.record_chat(
            chat_id=-100123,
            chat_type="supergroup",
            title="Synthetic Group",
        )

        self.assert_connections_closed()

        with sqlite3.connect(self.path) as verification:
            row = verification.execute(
                "SELECT title FROM known_chats "
                "WHERE chat_id = ?",
                (-100123,),
            ).fetchone()

            self.assertEqual(
                row,
                ("Synthetic Group",),
            )

        verification.close()

    async def test_failed_write_rolls_back_and_closes(self):
        def operation(connection):
            connection.execute(
                """
                INSERT INTO known_chats (
                    chat_id,
                    chat_type,
                    title
                ) VALUES (?, ?, ?)
                """,
                (
                    -100456,
                    "supergroup",
                    "Rollback Group",
                ),
            )

            raise RuntimeError(
                "Synthetic database write failure"
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "Synthetic database write failure",
        ):
            await self.database._write(
                operation
            )

        verification = sqlite3.connect(
            self.path
        )

        try:
            row = verification.execute(
                "SELECT chat_id FROM known_chats "
                "WHERE chat_id = ?",
                (-100456,),
            ).fetchone()

            self.assertIsNone(row)

        finally:
            verification.close()

        self.assert_connections_closed()


if __name__ == "__main__":
    unittest.main()
