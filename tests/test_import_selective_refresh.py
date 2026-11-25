from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

import handlers.imports as workflow

from imports.db_apply import apply_database_import
from imports.db_preview import load_existing_user_snapshot

from tests.test_import_db_apply import create_database
from tests.test_import_selective_telegram import selection


class SelectiveRefreshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.database = Path(temp.name) / "kokabiel.db"
        self.backup = Path(temp.name) / "backup.db"

        create_database(self.database)

        self.user_id = 1

        self.initial = selection(
            load_existing_user_snapshot(
                self.database,
                self.user_id,
            )
        )

    def refresh(self):
        return workflow.refresh_selective_import_preview(
            self.initial.full,
            database_path=self.database,
            user_id=self.user_id,
        )

    def test_refresh_rebuilds_exact_subset(self):
        refreshed = self.refresh()

        self.assertEqual(
            refreshed.full,
            self.initial.full,
        )

        self.assertEqual(
            refreshed.eligible,
            self.initial.eligible,
        )

        self.assertEqual(
            refreshed.excluded,
            self.initial.excluded,
        )

        self.assertIsNot(
            refreshed.full,
            self.initial.full,
        )

        self.assertIsNot(
            refreshed.eligible,
            self.initial.eligible,
        )

        self.assertTrue(
            workflow.selective_import_available(
                refreshed
            )
        )

    def test_refresh_is_read_only(self):
        before = self.database.read_bytes()

        self.refresh()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.assertFalse(self.backup.exists())

    def test_refresh_rejects_different_user(self):
        with self.assertRaises(ValueError):
            workflow.refresh_selective_import_preview(
                self.initial.full,
                database_path=self.database,
                user_id=self.user_id + 1,
            )

    def test_refresh_cannot_create_missing_database(self):
        missing = self.database.parent / "missing.db"

        with self.assertRaises(FileNotFoundError):
            workflow.refresh_selective_import_preview(
                self.initial.full,
                database_path=missing,
                user_id=self.user_id,
            )

        self.assertFalse(missing.exists())

    def test_refresh_detects_changed_database(self):
        apply_database_import(
            self.initial.eligible,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Synthetic Tester",
            chat_id=self.user_id,
        )

        refreshed = self.refresh()

        self.assertNotEqual(
            refreshed.full,
            self.initial.full,
        )

        self.assertFalse(
            workflow.selective_import_available(
                refreshed
            )
        )

        self.assertFalse(
            any(
                item.has_changes
                for item in refreshed.eligible.items
            )
        )


class ImportSessionLifecycleTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_import_command_preserves_active_operation(self):
        for state in (
            "processing",
            "preparing_verified",
            "applying",
        ):
            with self.subTest(state=state):
                pending = workflow.PendingImport(
                    token="original",
                    user_id=1,
                    user_name="Synthetic Tester",
                    chat_id=1,
                    state=state,
                )

                context = SimpleNamespace(
                    user_data={
                        workflow.IMPORT_SESSION_KEY: pending
                    }
                )

                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(
                        id=1,
                        type=ChatType.PRIVATE,
                    ),
                    effective_user=SimpleNamespace(
                        id=1,
                        full_name="Synthetic Tester",
                        username=None,
                    ),
                )

                with (
                    patch.object(
                        workflow,
                        "allowed",
                        new=AsyncMock(return_value=True),
                    ),
                    patch.object(
                        workflow,
                        "reply_personal_text",
                        new=AsyncMock(),
                    ),
                ):
                    await workflow.import_command(
                        update,
                        context,
                    )

                self.assertIs(
                    context.user_data.get(
                        workflow.IMPORT_SESSION_KEY
                    ),
                    pending,
                )


if __name__ == "__main__":
    unittest.main()
