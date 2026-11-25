from __future__ import annotations

import asyncio
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

import handlers.imports as workflow

from imports.db_apply import ImportPreviewStaleError
from tests.test_import_selective_telegram import selection


class SelectiveCallbackAuthorizationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.database = Path(temp.name) / "kokabiel.db"

        self.initial = selection()

        self.fresh = replace(
            self.initial,
            full=replace(self.initial.full),
            eligible=replace(self.initial.eligible),
        )

        self.user_id = self.initial.full.user_id
        self.chat_id = self.user_id

        self.pending = workflow.PendingImport(
            token="original",
            user_id=self.user_id,
            user_name="Test User",
            chat_id=self.chat_id,
            state="ready",
            preview=self.initial.full,
        )

        self.context = SimpleNamespace(
            user_data={
                workflow.IMPORT_SESSION_KEY: self.pending
            }
        )

        self.writer = MagicMock(
            return_value=SimpleNamespace(
                already_applied=False,
                media_added=1,
                states_added=1,
                states_updated=0,
                episodes_added=0,
                ratings_added=0,
            )
        )

        self.refresh = MagicMock(
            return_value=self.fresh
        )

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
                "apply_database_import",
                new=self.writer,
            ),
            patch.object(
                workflow,
                "refresh_selective_import_preview",
                new=self.refresh,
                create=True,
            ),
        )

        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def current(self):
        return self.context.user_data.get(
            workflow.IMPORT_SESSION_KEY
        )

    def arm_confirmation(self):
        pending = replace(
            self.pending,
            token="confirmation",
            state="verified_ready",
            preview=self.fresh.eligible,
        )

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = pending

        return pending

    async def callback(
        self,
        action,
        token,
        *,
        user_id=None,
        chat_id=None,
        chat_type=ChatType.PRIVATE,
    ):
        query = SimpleNamespace(
            data=f"imp|{action}|{token}",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(
                id=self.user_id if user_id is None else user_id,
                full_name="Test User",
                username=None,
            ),
            effective_chat=SimpleNamespace(
                id=self.chat_id if chat_id is None else chat_id,
                type=chat_type,
            ),
        )

        await workflow.import_callback_router(
            update,
            self.context,
        )

        return query

    def assert_rejected(self, query):
        query.answer.assert_awaited()

        self.assertTrue(
            query.answer.await_args.kwargs.get(
                "show_alert", False
            )
        )

    async def test_initial_token_cannot_apply_verified(self):
        query = await self.callback(
            "apply_verified", "original"
        )

        self.assert_rejected(query)
        self.writer.assert_not_called()
        self.assertIs(self.current(), self.pending)

    async def test_initial_mixed_preview_cannot_apply_full(self):
        query = await self.callback(
            "apply", "original"
        )

        self.assert_rejected(query)
        self.writer.assert_not_called()
        self.assertIs(self.current(), self.pending)

    async def test_prepare_rejects_wrong_token(self):
        query = await self.callback(
            "prepare_verified", "incorrect"
        )

        self.assert_rejected(query)
        self.refresh.assert_not_called()
        self.writer.assert_not_called()

    async def test_prepare_rejects_wrong_user(self):
        query = await self.callback(
            "prepare_verified",
            "original",
            user_id=self.user_id + 1,
        )

        self.assert_rejected(query)
        self.refresh.assert_not_called()
        self.writer.assert_not_called()

    async def test_prepare_rejects_wrong_chat(self):
        query = await self.callback(
            "prepare_verified",
            "original",
            chat_id=self.chat_id + 1,
        )

        self.assert_rejected(query)
        self.refresh.assert_not_called()
        self.writer.assert_not_called()

    async def test_prepare_generates_fresh_confirmation(self):
        query = await self.callback(
            "prepare_verified", "original"
        )

        self.refresh.assert_called_once()
        self.writer.assert_not_called()

        confirmation = self.current()

        self.assertIsNotNone(confirmation)
        self.assertIsNot(confirmation, self.pending)
        self.assertNotEqual(confirmation.token, "original")
        self.assertEqual(confirmation.state, "verified_ready")

        self.assertIs(
            confirmation.preview,
            self.fresh.eligible,
        )

        self.assertEqual(
            confirmation.preview.review_count, 0
        )

        query.edit_message_text.assert_awaited_once()

        markup = query.edit_message_text.await_args.kwargs[
            "reply_markup"
        ]

        actions = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }

        self.assertIn(
            f"imp|apply_verified|{confirmation.token}",
            actions,
        )

        self.assertNotIn(
            f"imp|apply|{confirmation.token}",
            actions,
        )

    async def test_original_token_rejected_after_preparation(self):
        confirmation = self.arm_confirmation()

        for action in (
            "apply",
            "apply_verified",
            "prepare_verified",
            "cancel",
        ):
            with self.subTest(action=action):
                query = await self.callback(
                    action, "original"
                )

                self.assert_rejected(query)
                self.assertIs(self.current(), confirmation)

        self.writer.assert_not_called()

    async def test_confirmation_token_cannot_apply_ordinary(self):
        confirmation = self.arm_confirmation()

        query = await self.callback(
            "apply", confirmation.token
        )

        self.assert_rejected(query)
        self.writer.assert_not_called()
        self.assertIs(self.current(), confirmation)

    async def test_verified_apply_rejects_wrong_token(self):
        self.arm_confirmation()

        query = await self.callback(
            "apply_verified", "incorrect"
        )

        self.assert_rejected(query)
        self.writer.assert_not_called()

    async def test_verified_apply_writes_only_eligible_preview(self):
        confirmation = self.arm_confirmation()

        await self.callback(
            "apply_verified", confirmation.token
        )

        self.writer.assert_called_once()

        call = self.writer.call_args

        self.assertIs(
            call.args[0], self.fresh.eligible
        )

        self.assertIsNot(
            call.args[0], self.initial.full
        )

        self.assertEqual(
            call.kwargs["database_path"],
            self.database,
        )

        self.assertEqual(
            call.kwargs["chat_id"],
            self.chat_id,
        )

        self.assertIsNone(self.current())

    async def test_verified_confirmation_cannot_be_replayed(self):
        confirmation = self.arm_confirmation()

        await self.callback(
            "apply_verified", confirmation.token
        )

        query = await self.callback(
            "apply_verified", confirmation.token
        )

        self.assert_rejected(query)
        self.writer.assert_called_once()
        self.assertIsNone(self.current())

    async def test_verified_stale_preview_is_discarded(self):
        confirmation = self.arm_confirmation()

        self.writer.side_effect = ImportPreviewStaleError(
            "stale"
        )

        query = await self.callback(
            "apply_verified", confirmation.token
        )

        self.writer.assert_called_once()
        self.assertIsNone(self.current())

        self.assertIn(
            "changed after this preview",
            query.edit_message_text.await_args.args[0],
        )

    async def test_verified_confirmation_can_be_cancelled(self):
        confirmation = self.arm_confirmation()

        await self.callback(
            "cancel", confirmation.token
        )

        self.writer.assert_not_called()
        self.assertIsNone(self.current())

    async def test_concurrent_confirmation_writes_once(self):
        confirmation = self.arm_confirmation()

        await asyncio.gather(
            self.callback(
                "apply_verified", confirmation.token
            ),
            self.callback(
                "apply_verified", confirmation.token
            ),
        )

        self.writer.assert_called_once()
        self.assertIsNone(self.current())

    async def test_applying_session_cannot_be_cancelled(self):
        applying = replace(
            self.arm_confirmation(),
            state="applying",
        )

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = applying

        query = await self.callback(
            "cancel", applying.token
        )

        self.assert_rejected(query)
        self.assertIs(self.current(), applying)
        self.writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
