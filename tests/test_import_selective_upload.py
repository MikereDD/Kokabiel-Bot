from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

import handlers.imports as workflow

from imports.db_preview import load_existing_user_snapshot
from imports.selective import build_selective_import_preview

from tests.test_import_db_apply import create_database
from tests.test_import_selective import (
    resolution,
    resolved,
    unresolved,
)


class SelectiveUploadTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.database = Path(temp.name) / "kokabiel.db"
        create_database(self.database)

        self.pending = workflow.PendingImport(
            token="original",
            user_id=1,
            user_name="Synthetic Tester",
            chat_id=1,
            state="awaiting_upload",
        )

        async def download(*, custom_path):
            Path(custom_path).write_text(
                "synthetic tracker export"
            )

        self.fake_file = SimpleNamespace(
            download_to_drive=AsyncMock(
                side_effect=download
            )
        )

        self.context = SimpleNamespace(
            user_data={
                workflow.IMPORT_SESSION_KEY: self.pending
            },
            bot=SimpleNamespace(
                get_file=AsyncMock(
                    return_value=self.fake_file
                )
            ),
        )

        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=1,
                type=ChatType.PRIVATE,
            ),
            effective_user=SimpleNamespace(
                id=1,
                full_name="Synthetic Tester",
                username=None,
            ),
            effective_message=SimpleNamespace(
                document=SimpleNamespace(
                    file_name="ratings.csv",
                    file_size=100,
                    file_id="synthetic-file",
                )
            ),
        )

        self.reply = AsyncMock()
        self.writer = MagicMock()

        patches = (
            patch.object(
                workflow,
                "allowed",
                new=AsyncMock(return_value=True),
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
                "tmdb",
                return_value=object(),
            ),
            patch.object(
                workflow,
                "analyze_import",
                return_value=object(),
            ),
            patch.object(
                workflow,
                "reply_personal_text",
                new=self.reply,
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

    async def upload(self, source):
        snapshot = load_existing_user_snapshot(
            self.database,
            user_id=1,
        )

        expected = build_selective_import_preview(
            source,
            snapshot,
        )

        before = self.database.read_bytes()

        with (
            patch.object(
                workflow,
                "resolve_import_plan",
                new=AsyncMock(return_value=source),
            ),
            patch.object(
                workflow,
                "load_existing_user_snapshot",
                wraps=load_existing_user_snapshot,
            ) as snapshot_loader,
        ):
            await workflow.receive_import_document(
                self.update,
                self.context,
            )

        snapshot_loader.assert_called_once_with(
            self.database,
            1,
        )

        ready = self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ]

        self.assertEqual(ready.state, "ready")
        self.assertEqual(ready.token, "original")
        self.assertEqual(ready.preview, expected.full)
        self.assertIsNone(ready.path)

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        upload = workflow._quarantine_path(
            self.database,
            1,
            "original",
            "ratings.csv",
        )

        self.assertFalse(upload.exists())
        self.writer.assert_not_called()
        self.fake_file.download_to_drive.assert_awaited_once()

        self.reply.assert_awaited_once()

        markup = self.reply.await_args.kwargs[
            "reply_markup"
        ]

        actions = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }

        return actions, expected

    async def test_mixed_upload_offers_verified_preparation(self):
        actions, expected = await self.upload(
            resolution(
                resolved(
                    "movie", 100, "Verified Film"
                ),
                unresolved(
                    media_type="movie",
                    title="Missing Film",
                ),
            )
        )

        self.assertGreater(
            expected.full.review_count, 0
        )

        self.assertEqual(
            expected.eligible.review_count, 0
        )

        self.assertIn(
            "imp|prepare_verified|original",
            actions,
        )

        self.assertIn(
            "imp|review|original",
            actions,
        )

        self.assertIn(
            "imp|cancel|original",
            actions,
        )

        self.assertNotIn(
            "imp|apply|original",
            actions,
        )

        self.assertNotIn(
            "imp|apply_verified|original",
            actions,
        )

    async def test_clean_upload_retains_ordinary_apply(self):
        actions, expected = await self.upload(
            resolution(
                resolved(
                    "movie", 100, "Verified Film"
                )
            )
        )

        self.assertEqual(
            expected.full.review_count, 0
        )

        self.assertEqual(
            actions,
            {
                "imp|apply|original",
                "imp|cancel|original",
            },
        )

    async def test_all_excluded_upload_has_no_apply_action(self):
        actions, expected = await self.upload(
            resolution(
                unresolved(
                    media_type="movie",
                    title="Missing Film",
                )
            )
        )

        self.assertFalse(
            expected.eligible.items
        )

        self.assertEqual(
            actions,
            {
                "imp|review|original",
                "imp|cancel|original",
            },
        )


    def snapshot(self):
        return load_existing_user_snapshot(
            self.database,
            user_id=1,
        )

    def mixed_source(self):
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

    async def callback(self, action, token):
        query = SimpleNamespace(
            data=f"imp|{action}|{token}",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        update = SimpleNamespace(
            callback_query=query,
            effective_chat=self.update.effective_chat,
            effective_user=self.update.effective_user,
        )

        await workflow.import_callback_router(
            update,
            self.context,
        )

        return query

    def current_session(self):
        return self.context.user_data.get(
            workflow.IMPORT_SESSION_KEY
        )

    def assert_rejected(self, query):
        query.answer.assert_awaited()

        self.assertTrue(
            query.answer.await_args.kwargs.get(
                "show_alert", False
            )
        )

    async def prepare_confirmation(self):
        before = self.database.read_bytes()

        query = await self.callback(
            "prepare_verified",
            "original",
        )

        confirmation = self.current_session()

        self.assertIsNotNone(confirmation)
        self.assertEqual(
            confirmation.state,
            "verified_ready",
        )
        self.assertNotEqual(
            confirmation.token,
            "original",
        )
        self.assertEqual(
            confirmation.preview.review_count,
            0,
        )

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.writer.assert_not_called()

        query.edit_message_text.assert_awaited_once()

        markup = (
            query.edit_message_text
            .await_args.kwargs["reply_markup"]
        )

        actions = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }

        self.assertEqual(
            actions,
            {
                f"imp|apply_verified|{confirmation.token}",
                f"imp|cancel|{confirmation.token}",
            },
        )

        return confirmation

    def enable_real_writer(self):
        from imports.db_apply import apply_database_import

        self.writer.side_effect = apply_database_import

    def apply_external_preview(self, preview):
        from imports.db_apply import apply_database_import

        return apply_database_import(
            preview,
            database_path=self.database,
            backup_path=(
                self.database.parent / "external-backup.db"
            ),
            user_name="Synthetic Tester",
            chat_id=1,
        )

    async def test_e2e_verified_import_writes_exact_subset(self):
        import sqlite3

        before = self.snapshot()

        actions, expected = await self.upload(
            self.mixed_source()
        )

        self.assertIn(
            "imp|prepare_verified|original",
            actions,
        )

        for action in (
            "apply",
            "apply_verified",
        ):
            self.assert_rejected(
                await self.callback(
                    action, "original"
                )
            )

        self.writer.assert_not_called()

        confirmation = (
            await self.prepare_confirmation()
        )

        self.assertEqual(
            confirmation.preview,
            expected.eligible,
        )

        self.assertIsNot(
            confirmation.preview,
            expected.full,
        )

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                "original",
            )
        )

        self.assert_rejected(
            await self.callback(
                "apply",
                confirmation.token,
            )
        )

        self.writer.assert_not_called()

        self.enable_real_writer()

        query = await self.callback(
            "apply_verified",
            confirmation.token,
        )

        self.writer.assert_called_once()

        self.assertIs(
            self.writer.call_args.args[0],
            confirmation.preview,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.assertIn(
            "Import complete",
            query.edit_message_text.await_args.args[0],
        )

        after = self.snapshot()

        self.assertEqual(
            after.media - before.media,
            frozenset({
                ("movie", 100),
                ("tv", 300),
            }),
        )

        self.assertEqual(
            set(after.states) - set(before.states),
            {
                ("movie", 100),
                ("tv", 300),
            },
        )

        self.assertEqual(
            set(after.ratings) - set(before.ratings),
            {
                ("movie", 100),
            },
        )

        self.assertEqual(
            after.episodes.get(300),
            frozenset({(1, 2)}),
        )

        self.assertNotIn(
            ("tv", 200),
            after.media,
        )

        self.assertNotIn(
            ("tv", 200),
            after.states,
        )

        self.assertNotIn(
            ("tv", 200),
            after.ratings,
        )

        self.assertNotIn(
            200,
            after.episodes,
        )

        backup = self.writer.call_args.kwargs[
            "backup_path"
        ]

        self.assertTrue(backup.is_file())

        self.assertEqual(
            load_existing_user_snapshot(
                backup,
                user_id=1,
            ),
            before,
        )

        committed = self.database.read_bytes()

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                confirmation.token,
            )
        )

        self.writer.assert_called_once()

        self.assertEqual(
            self.database.read_bytes(),
            committed,
        )

        connection = sqlite3.connect(
            self.database
        )

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

    async def test_e2e_cancel_confirmation_writes_nothing(self):
        await self.upload(
            self.mixed_source()
        )

        before = self.database.read_bytes()

        confirmation = (
            await self.prepare_confirmation()
        )

        await self.callback(
            "cancel",
            confirmation.token,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                confirmation.token,
            )
        )

        self.writer.assert_not_called()

    async def test_e2e_database_change_before_preparation(self):
        _, expected = await self.upload(
            self.mixed_source()
        )

        self.apply_external_preview(
            expected.eligible
        )

        changed = self.database.read_bytes()

        query = await self.callback(
            "prepare_verified",
            "original",
        )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            changed,
        )

        self.assertIn(
            "Run /import again",
            query.edit_message_text.await_args.args[0],
        )

    async def test_e2e_database_change_after_confirmation(self):
        await self.upload(
            self.mixed_source()
        )

        confirmation = (
            await self.prepare_confirmation()
        )

        external = build_selective_import_preview(
            resolution(
                resolved(
                    "movie", 100, "Verified Film",
                    rating=8.0,
                )
            ),
            self.snapshot(),
        )

        self.apply_external_preview(
            external.eligible
        )

        changed = self.snapshot()

        self.enable_real_writer()

        query = await self.callback(
            "apply_verified",
            confirmation.token,
        )

        self.writer.assert_called_once()

        self.assertIsNone(
            self.current_session()
        )

        self.assertIn(
            "changed after this preview",
            query.edit_message_text.await_args.args[0],
        )

        self.assertEqual(
            self.snapshot(),
            changed,
        )

        self.assertNotIn(
            ("tv", 300),
            changed.media,
        )

    async def test_e2e_clean_import_uses_ordinary_approval(self):
        actions, expected = await self.upload(
            resolution(
                resolved(
                    "movie", 100, "Verified Film"
                )
            )
        )

        self.assertEqual(
            actions,
            {
                "imp|apply|original",
                "imp|cancel|original",
            },
        )

        self.assert_rejected(
            await self.callback(
                "prepare_verified",
                "original",
            )
        )

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                "original",
            )
        )

        self.writer.assert_not_called()

        self.enable_real_writer()

        query = await self.callback(
            "apply",
            "original",
        )

        self.writer.assert_called_once()

        self.assertEqual(
            self.writer.call_args.args[0],
            expected.full,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.assertIn(
            "Import complete",
            query.edit_message_text.await_args.args[0],
        )

        self.assertIn(
            ("movie", 100),
            self.snapshot().media,
        )


    async def failing_callback(
        self,
        action,
        token,
        *,
        fail_answer=False,
        fail_edit=False,
    ):
        from telegram.error import TimedOut

        query = SimpleNamespace(
            data=f"imp|{action}|{token}",
            answer=AsyncMock(
                side_effect=(
                    TimedOut()
                    if fail_answer
                    else None
                )
            ),
            edit_message_text=AsyncMock(
                side_effect=(
                    TimedOut()
                    if fail_edit
                    else None
                )
            ),
        )

        update = SimpleNamespace(
            callback_query=query,
            effective_chat=self.update.effective_chat,
            effective_user=self.update.effective_user,
        )

        await workflow.import_callback_router(
            update,
            self.context,
        )

        return query

    async def test_failure_apply_acknowledgement(self):
        await self.upload(
            self.mixed_source()
        )

        confirmation = (
            await self.prepare_confirmation()
        )

        before = self.database.read_bytes()

        await self.failing_callback(
            "apply_verified",
            confirmation.token,
            fail_answer=True,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                confirmation.token,
            )
        )

    async def test_failure_prepare_acknowledgement(self):
        await self.upload(
            self.mixed_source()
        )

        before = self.database.read_bytes()

        await self.failing_callback(
            "prepare_verified",
            "original",
            fail_answer=True,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

    async def test_failure_confirmation_delivery(self):
        await self.upload(
            self.mixed_source()
        )

        before = self.database.read_bytes()

        await self.failing_callback(
            "prepare_verified",
            "original",
            fail_edit=True,
        )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.assert_rejected(
            await self.callback(
                "prepare_verified",
                "original",
            )
        )

    async def test_failure_result_delivery_preserves_commit(self):
        await self.upload(
            self.mixed_source()
        )

        confirmation = (
            await self.prepare_confirmation()
        )

        self.enable_real_writer()

        await self.failing_callback(
            "apply_verified",
            confirmation.token,
            fail_edit=True,
        )

        self.writer.assert_called_once()

        self.assertIsNone(
            self.current_session()
        )

        after = self.snapshot()

        self.assertIn(
            ("movie", 100),
            after.media,
        )

        self.assertIn(
            ("tv", 300),
            after.media,
        )

        self.assertNotIn(
            ("tv", 200),
            after.media,
        )

        self.assertEqual(
            after.episodes.get(300),
            frozenset({(1, 2)}),
        )

        committed = self.database.read_bytes()

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                confirmation.token,
            )
        )

        self.writer.assert_called_once()

        self.assertEqual(
            self.database.read_bytes(),
            committed,
        )

    async def test_failure_initial_preview_delivery(self):
        from telegram.error import TimedOut

        before = self.database.read_bytes()

        self.reply.side_effect = TimedOut()

        with patch.object(
            workflow,
            "resolve_import_plan",
            new=AsyncMock(
                return_value=self.mixed_source()
            ),
        ):
            await workflow.receive_import_document(
                self.update,
                self.context,
            )

        self.assertIsNone(
            self.current_session()
        )

        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        upload = workflow._quarantine_path(
            self.database,
            1,
            "original",
            "ratings.csv",
        )

        self.assertFalse(upload.exists())

        self.assert_rejected(
            await self.callback(
                "prepare_verified",
                "original",
            )
        )


    async def assert_initial_delivery_blocks(
        self,
        source,
        attempted_action,
    ):
        observed = []

        async def deliver(*args, **kwargs):
            current = self.current_session()

            self.assertEqual(
                current.state,
                "processing",
            )

            self.assertIsNone(current.path)

            observed.append(current.state)

            query = await self.callback(
                attempted_action,
                current.token,
            )

            self.assert_rejected(query)
            self.assertIs(
                self.current_session(),
                current,
            )

            self.writer.assert_not_called()

        self.reply.side_effect = deliver

        await self.upload(source)

        self.assertEqual(
            observed,
            ["processing"],
        )

        self.assertEqual(
            self.current_session().state,
            "ready",
        )

    async def test_delivery_clean_preview_blocks_early_apply(self):
        await self.assert_initial_delivery_blocks(
            resolution(
                resolved(
                    "movie", 100, "Verified Film"
                )
            ),
            "apply",
        )

    async def test_delivery_mixed_preview_blocks_early_prepare(self):
        await self.assert_initial_delivery_blocks(
            self.mixed_source(),
            "prepare_verified",
        )

    async def test_delivery_confirmation_blocks_early_apply(self):
        await self.upload(
            self.mixed_source()
        )

        before = self.database.read_bytes()
        observed = []

        async def deliver(*args, **kwargs):
            current = self.current_session()

            self.assertEqual(
                current.state,
                "preparing_verified",
            )

            markup = kwargs["reply_markup"]

            actions = {
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
            }

            approved_action = next(
                action
                for action in actions
                if action.startswith(
                    "imp|apply_verified|"
                )
            )

            token = approved_action.split("|")[2]

            self.assertNotEqual(
                token,
                current.token,
            )

            observed.append(token)

            query = await self.callback(
                "apply_verified",
                token,
            )

            self.assert_rejected(query)

            self.writer.assert_not_called()

            self.assertIs(
                self.current_session(),
                current,
            )

        query = SimpleNamespace(
            data="imp|prepare_verified|original",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(
                side_effect=deliver
            ),
        )

        update = SimpleNamespace(
            callback_query=query,
            effective_chat=self.update.effective_chat,
            effective_user=self.update.effective_user,
        )

        await workflow.import_callback_router(
            update,
            self.context,
        )

        self.assertEqual(len(observed), 1)

        confirmation = self.current_session()

        self.assertEqual(
            confirmation.state,
            "verified_ready",
        )

        self.assertEqual(
            confirmation.token,
            observed[0],
        )

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.writer.assert_not_called()

    async def test_delivery_acknowledgement_precedes_writer(self):
        await self.upload(
            self.mixed_source()
        )

        confirmation = (
            await self.prepare_confirmation()
        )

        before = self.database.read_bytes()

        self.enable_real_writer()

        with patch.object(
            workflow,
            "_prepare_backup_directory",
            wraps=workflow._prepare_backup_directory,
        ) as prepare_backup:

            async def acknowledge(*args, **kwargs):
                current = self.current_session()

                self.assertEqual(
                    current.state,
                    "applying",
                )

                prepare_backup.assert_not_called()
                self.writer.assert_not_called()

                self.assertEqual(
                    self.database.read_bytes(),
                    before,
                )

                for action in (
                    "apply_verified",
                    "cancel",
                ):
                    duplicate = await self.callback(
                        action,
                        confirmation.token,
                    )

                    self.assert_rejected(duplicate)

                    self.assertIs(
                        self.current_session(),
                        current,
                    )

                prepare_backup.assert_not_called()
                self.writer.assert_not_called()

            query = SimpleNamespace(
                data=(
                    "imp|apply_verified|"
                    + confirmation.token
                ),
                answer=AsyncMock(
                    side_effect=acknowledge
                ),
                edit_message_text=AsyncMock(),
            )

            update = SimpleNamespace(
                callback_query=query,
                effective_chat=self.update.effective_chat,
                effective_user=self.update.effective_user,
            )

            await workflow.import_callback_router(
                update,
                self.context,
            )

            prepare_backup.assert_called_once()

        self.writer.assert_called_once()

        self.assertIsNone(
            self.current_session()
        )

        self.assertIn(
            "Import complete",
            query.edit_message_text.await_args.args[0],
        )

        after = self.snapshot()

        self.assertIn(
            ("movie", 100),
            after.media,
        )

        self.assertIn(
            ("tv", 300),
            after.media,
        )

        self.assertNotIn(
            ("tv", 200),
            after.media,
        )


    async def test_cancellation_initial_preview(self):
        import asyncio

        before = self.database.read_bytes()

        self.reply.side_effect = asyncio.CancelledError()

        with patch.object(
            workflow,
            "resolve_import_plan",
            new=AsyncMock(
                return_value=self.mixed_source()
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await workflow.receive_import_document(
                    self.update,
                    self.context,
                )

        self.assertIsNone(self.current_session())
        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

        self.assertFalse(
            workflow._quarantine_path(
                self.database,
                1,
                "original",
                "ratings.csv",
            ).exists()
        )

    async def test_cancellation_document_processing(self):
        import asyncio

        before = self.database.read_bytes()

        async def interrupted_download(*, custom_path):
            Path(custom_path).write_text(
                "synthetic partial export"
            )

            raise asyncio.CancelledError()

        self.fake_file.download_to_drive.side_effect = (
            interrupted_download
        )

        with self.assertRaises(asyncio.CancelledError):
            await workflow.receive_import_document(
                self.update,
                self.context,
            )

        self.assertIsNone(self.current_session())
        self.writer.assert_not_called()

        self.assertFalse(
            workflow._quarantine_path(
                self.database,
                1,
                "original",
                "ratings.csv",
            ).exists()
        )

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

    def cancellation_update(self, query):
        return SimpleNamespace(
            callback_query=query,
            effective_chat=self.update.effective_chat,
            effective_user=self.update.effective_user,
        )

    async def test_cancellation_verified_preparation(self):
        import asyncio

        await self.upload(self.mixed_source())

        before = self.database.read_bytes()

        query = SimpleNamespace(
            data="imp|prepare_verified|original",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(
                side_effect=asyncio.CancelledError()
            ),
        )

        with self.assertRaises(asyncio.CancelledError):
            await workflow.import_callback_router(
                self.cancellation_update(query),
                self.context,
            )

        self.assertIsNone(self.current_session())
        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

    async def test_cancellation_apply_acknowledgement(self):
        import asyncio

        await self.upload(self.mixed_source())

        confirmation = await self.prepare_confirmation()

        before = self.database.read_bytes()

        query = SimpleNamespace(
            data=(
                "imp|apply_verified|"
                + confirmation.token
            ),
            answer=AsyncMock(
                side_effect=asyncio.CancelledError()
            ),
            edit_message_text=AsyncMock(),
        )

        with patch.object(
            workflow,
            "_prepare_backup_directory",
        ) as prepare_backup:

            with self.assertRaises(asyncio.CancelledError):
                await workflow.import_callback_router(
                    self.cancellation_update(query),
                    self.context,
                )

            prepare_backup.assert_not_called()

        self.assertIsNone(self.current_session())
        self.writer.assert_not_called()

        self.assertEqual(
            self.database.read_bytes(),
            before,
        )

    async def test_cancellation_waits_for_transaction(self):
        import asyncio
        import threading

        from imports.db_apply import apply_database_import

        await self.upload(self.mixed_source())

        confirmation = await self.prepare_confirmation()

        loop = asyncio.get_running_loop()

        started = asyncio.Event()
        release = threading.Event()

        def controlled_writer(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)

            if not release.wait(timeout=10):
                raise RuntimeError(
                    "Synthetic writer release timed out"
                )

            return apply_database_import(
                *args,
                **kwargs,
            )

        self.writer.side_effect = controlled_writer

        query = SimpleNamespace(
            data=(
                "imp|apply_verified|"
                + confirmation.token
            ),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        task = asyncio.create_task(
            workflow.import_callback_router(
                self.cancellation_update(query),
                self.context,
            )
        )

        try:
            await asyncio.wait_for(
                started.wait(),
                timeout=5,
            )

            task.cancel()

            await asyncio.sleep(0)

            self.assertFalse(task.done())

            current = self.current_session()

            self.assertEqual(
                current.state,
                "applying",
            )

            self.assert_rejected(
                await self.callback(
                    "apply_verified",
                    confirmation.token,
                )
            )

            self.assertIs(
                self.current_session(),
                current,
            )

            self.writer.assert_called_once()

        finally:
            release.set()

            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=10,
                )

            except asyncio.CancelledError:
                pass

        self.assertTrue(task.cancelled())

        self.assertIsNone(self.current_session())

        after = self.snapshot()

        self.assertIn(
            ("movie", 100),
            after.media,
        )

        self.assertIn(
            ("tv", 300),
            after.media,
        )

        self.assertNotIn(
            ("tv", 200),
            after.media,
        )

        self.assert_rejected(
            await self.callback(
                "apply_verified",
                confirmation.token,
            )
        )

        self.writer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
