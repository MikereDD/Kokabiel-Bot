from __future__ import annotations

import asyncio
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

import handlers.imports as workflow

from tests.test_import_telegram_workflow import preview


class ImportSessionBoundaryTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.root = Path(temp.name)
        self.database = self.root / "kokabiel.db"

        self.pending = workflow.PendingImport(
            token="original",
            user_id=1,
            user_name="Synthetic Tester",
            chat_id=1,
            state="awaiting_upload",
        )

        self.context = SimpleNamespace(
            user_data={
                workflow.IMPORT_SESSION_KEY: self.pending
            },
            bot=SimpleNamespace(
                get_file=AsyncMock()
            ),
        )

        self.reply = AsyncMock()
        self.writer = MagicMock()

        patches = (
            patch.object(
                workflow,
                "reply_personal_text",
                new=self.reply,
            ),
            patch.object(
                workflow,
                "settings",
                return_value=SimpleNamespace(
                    database_path=self.database
                ),
            ),
            patch.object(
                workflow,
                "apply_database_import",
                new=self.writer,
            ),
        )

        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def current(self):
        return self.context.user_data.get(
            workflow.IMPORT_SESSION_KEY
        )

    def update(
        self,
        *,
        chat_id=1,
        chat_type=ChatType.PRIVATE,
        document=None,
        callback_query=None,
    ):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=chat_id,
                type=chat_type,
            ),
            effective_user=SimpleNamespace(
                id=1,
                full_name="Synthetic Tester",
                username=None,
            ),
            effective_message=SimpleNamespace(
                document=document,
            ),
            callback_query=callback_query,
        )

    def document(self):
        return SimpleNamespace(
            file_name="ratings.csv",
            file_size=100,
            file_id="synthetic-file",
        )

    async def test_group_commands_preserve_private_session(self):
        from dataclasses import replace

        for state in (
            "awaiting_upload",
            "processing",
            "ready",
            "preparing_verified",
            "verified_ready",
            "applying",
        ):
            for chat_type in (
                ChatType.GROUP,
                ChatType.SUPERGROUP,
            ):
                with self.subTest(
                    state=state,
                    chat_type=chat_type,
                ):
                    raw = self.root / "private.csv"
                    raw.write_text("synthetic private data")

                    pending = replace(
                        self.pending,
                        state=state,
                        path=raw,
                    )

                    self.context.user_data[
                        workflow.IMPORT_SESSION_KEY
                    ] = pending

                    with patch.object(
                        workflow,
                        "group_import_origin",
                        new=AsyncMock(return_value=None),
                    ):
                        await workflow.import_command(
                            self.update(
                                chat_id=-100123,
                                chat_type=chat_type,
                            ),
                            self.context,
                        )

                    self.assertIs(
                        self.current(),
                        pending,
                    )

                    self.assertTrue(raw.exists())

        self.writer.assert_not_called()

    async def test_processing_cannot_be_cancelled(self):
        from dataclasses import replace

        raw = self.root / "processing.csv"
        raw.write_text("synthetic private data")

        pending = replace(
            self.pending,
            state="processing",
            path=raw,
        )

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = pending

        query = SimpleNamespace(
            data="imp|cancel|original",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        with patch.object(
            workflow,
            "allowed",
            new=AsyncMock(return_value=True),
        ):
            await workflow.import_callback_router(
                self.update(callback_query=query),
                self.context,
            )

        self.assertIs(self.current(), pending)
        self.assertTrue(raw.exists())
        self.writer.assert_not_called()

        query.answer.assert_awaited_once()

        self.assertTrue(
            query.answer.await_args.kwargs[
                "show_alert"
            ]
        )

        query.edit_message_text.assert_not_awaited()

    async def test_stale_upload_authorization_is_rejected(self):
        from dataclasses import replace

        replacement = replace(
            self.pending,
            token="replacement",
        )

        async def authorize(*args, **kwargs):
            self.context.user_data[
                workflow.IMPORT_SESSION_KEY
            ] = replacement

            return True

        with patch.object(
            workflow,
            "allowed",
            new=AsyncMock(side_effect=authorize),
        ):
            await workflow.receive_import_document(
                self.update(
                    document=self.document()
                ),
                self.context,
            )

        self.assertIs(
            self.current(),
            replacement,
        )

        self.context.bot.get_file.assert_not_awaited()
        self.writer.assert_not_called()

    async def test_concurrent_uploads_are_processed_once(self):
        arrivals = 0
        both_arrived = asyncio.Event()

        async def authorize(*args, **kwargs):
            nonlocal arrivals

            arrivals += 1

            if arrivals == 2:
                both_arrived.set()

            await both_arrived.wait()

            return True

        async def download(*, custom_path):
            Path(custom_path).write_text(
                "synthetic tracker export"
            )

        fake_file = SimpleNamespace(
            download_to_drive=AsyncMock(
                side_effect=download
            )
        )

        self.context.bot.get_file.return_value = (
            fake_file
        )

        expected = preview()

        analyzer = MagicMock(
            return_value=object()
        )

        with (
            patch.object(
                workflow,
                "allowed",
                new=AsyncMock(
                    side_effect=authorize
                ),
            ),
            patch.object(
                workflow,
                "tmdb",
                return_value=object(),
            ),
            patch.object(
                workflow,
                "analyze_import",
                new=analyzer,
            ),
            patch.object(
                workflow,
                "resolve_import_plan",
                new=AsyncMock(
                    return_value=object()
                ),
            ),
            patch.object(
                workflow,
                "preview_selective_database_import",
                return_value=workflow.SelectiveImportPreview(
                    full=expected,
                    eligible=expected,
                    excluded=(),
                ),
            ),
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    workflow.receive_import_document(
                        self.update(
                            document=self.document()
                        ),
                        self.context,
                    ),
                    workflow.receive_import_document(
                        self.update(
                            document=self.document()
                        ),
                        self.context,
                    ),
                ),
                timeout=5,
            )

        self.assertEqual(arrivals, 2)

        self.context.bot.get_file.assert_awaited_once()
        fake_file.download_to_drive.assert_awaited_once()
        analyzer.assert_called_once()

        ready = self.current()

        self.assertEqual(ready.state, "ready")
        self.assertIsNone(ready.path)
        self.assertIs(ready.preview, expected)

        upload = workflow._quarantine_path(
            self.database,
            1,
            "original",
            "ratings.csv",
        )

        self.assertFalse(upload.exists())
        self.writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
