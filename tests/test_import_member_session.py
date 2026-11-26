from __future__ import annotations

import asyncio
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

from handlers import imports as workflow
from handlers import import_tasks as tasks
from handlers.import_access import ImportOrigin
from tests.test_import_telegram_workflow import preview


class MemberImportSessionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.db = Path(temporary.name) / "kokabiel.db"

        self.user = NS(
            id=20,
            full_name="Synthetic Member",
            username=None,
            is_bot=False,
        )

        self.member = NS(
            user=self.user,
            status="member",
        )

        self.settings = NS(
            owner_user_ids={1},
            allowed_chat_ids={-100123},
            database_path=self.db,
        )

        self.manager = tasks.ImportTaskManager()
        self.running_tasks = []

        self.context = NS(
            user_data={},
            bot=NS(
                get_chat_member=AsyncMock(
                    return_value=self.member,
                ),
                get_file=AsyncMock(),
            ),
            application=NS(
                running=True,
                bot_data={
                    tasks.IMPORT_TASK_MANAGER_KEY: self.manager,
                    "settings": self.settings,
                },
                create_task=self.create_task,
            ),
        )

        self.pending = workflow.PendingImport(
            token="member-token",
            user_id=20,
            user_name="Synthetic Member",
            chat_id=20,
            state="ready",
            preview=preview(user_id=20),
            origin=ImportOrigin(20, -100123),
            expires_at=monotonic() + 3600,
        )

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = self.pending

        self.query = NS(
            data="imp|apply|member-token",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        self.update = NS(
            effective_chat=NS(
                id=20,
                type=ChatType.PRIVATE,
            ),
            effective_user=self.user,
            effective_message=NS(
                document=None,
                reply_text=AsyncMock(),
            ),
            callback_query=self.query,
        )

        self.writer = MagicMock(
            return_value=NS(
                already_applied=False,
                media_added=1,
                states_added=1,
                states_updated=0,
                episodes_added=10,
                ratings_added=0,
            )
        )

        patcher = patch.object(
            workflow,
            "apply_database_import",
            new=self.writer,
        )

        patcher.start()
        self.addCleanup(patcher.stop)

    def create_task(self, coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        self.running_tasks.append(task)
        return task

    async def asyncTearDown(self):
        await self.manager.drain()

    def current(self):
        return self.context.user_data.get(
            workflow.IMPORT_SESSION_KEY
        )

    async def callback(self):
        await tasks.dispatch_callback(
            self.update,
            self.context,
            workflow.import_callback_router,
        )

        if self.running_tasks:
            await asyncio.gather(*self.running_tasks)

    async def test_member_apply_uses_supervised_writer(self):
        await self.callback()

        self.assertEqual(len(self.running_tasks), 1)

        self.writer.assert_called_once()

        self.assertEqual(
            self.writer.call_args.args[0].user_id,
            20,
        )

        self.assertEqual(
            self.context.bot.get_chat_member.await_count,
            2,
        )

        self.assertEqual(
            self.writer.call_args.kwargs["origin_group_id"],
            -100123,
        )

        self.assertIsNone(self.current())

    async def test_departed_member_cannot_apply(self):
        self.member.status = "left"

        await self.callback()

        self.writer.assert_not_called()
        self.assertIs(self.current(), self.pending)

    async def test_removed_group_cannot_apply(self):
        self.settings.allowed_chat_ids.clear()

        await self.callback()

        self.writer.assert_not_called()

        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_revoked_during_confirmation(self):
        async def acknowledge(*args, **kwargs):
            self.member.status = "left"

        self.query.answer.side_effect = acknowledge

        await self.callback()

        self.writer.assert_not_called()
        self.assertIsNone(self.current())

        self.assertIn(
            "No import changes were made",
            self.query.edit_message_text.await_args.args[0],
        )

    async def test_wrong_private_user_cannot_apply(self):
        self.user.id = 21
        self.update.effective_chat.id = 21

        await self.callback()

        self.writer.assert_not_called()

        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_wrong_token_cannot_apply(self):
        self.query.data = "imp|apply|forged"

        await self.callback()

        self.writer.assert_not_called()

        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_group_callback_cannot_apply(self):
        self.update.effective_chat.type = ChatType.SUPERGROUP
        self.update.effective_chat.id = -100123

        await self.callback()

        self.writer.assert_not_called()
        self.assertEqual(len(self.running_tasks), 0)

    async def test_member_can_cancel_pending_import(self):
        self.query.data = "imp|cancel|member-token"

        await self.callback()

        self.writer.assert_not_called()
        self.assertIsNone(self.current())

    async def test_unreserved_member_writer_denied(self):
        # Direct invocation must not bypass the dispatcher.
        await workflow.import_callback_router(
            self.update,
            self.context,
        )

        self.writer.assert_not_called()

    async def test_session_replaced_during_authorization(self):
        replacement = replace(
            self.pending,
            token="new-session",
        )

        async def authorize(**kwargs):
            self.context.user_data[
                workflow.IMPORT_SESSION_KEY
            ] = replacement

            return self.member

        self.context.bot.get_chat_member.side_effect = authorize

        await self.callback()

        self.writer.assert_not_called()
        self.assertIs(self.current(), replacement)

    async def test_private_member_upload_reaches_intake(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(
            self.pending,
            state="awaiting_upload",
            preview=None,
        )

        self.update.callback_query = None

        # Harmless invalid document verifies authorization
        # and intake without downloading or parsing a file.
        self.update.effective_message.document = NS(
            file_name="invalid.exe",
            file_size=10,
        )

        await tasks.dispatch_import_document(
            self.update,
            self.context,
            workflow.receive_import_document,
        )

        await asyncio.gather(*self.running_tasks)

        self.assertEqual(len(self.running_tasks), 1)

        self.update.effective_message.reply_text\
            .assert_awaited_once()

        self.context.bot.get_file.assert_not_awaited()
        self.writer.assert_not_called()

    async def test_departed_member_upload_denied(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(
            self.pending,
            state="awaiting_upload",
            preview=None,
        )

        self.member.status = "left"

        self.update.callback_query = None

        self.update.effective_message.document = NS(
            file_name="export.csv",
            file_size=10,
        )

        await tasks.dispatch_import_document(
            self.update,
            self.context,
            workflow.receive_import_document,
        )

        await asyncio.gather(*self.running_tasks)

        self.context.bot.get_file.assert_not_awaited()
        self.writer.assert_not_called()

    async def test_group_document_ignored(self):
        self.update.effective_chat.type = ChatType.SUPERGROUP
        self.update.effective_chat.id = -100123
        self.update.callback_query = None

        self.update.effective_message.document = NS(
            file_name="export.csv",
            file_size=10,
        )

        await tasks.dispatch_import_document(
            self.update,
            self.context,
            workflow.receive_import_document,
        )

        self.context.bot.get_file.assert_not_awaited()
        self.writer.assert_not_called()

        self.assertEqual(len(self.running_tasks), 0)


    async def test_expired_member_preview_cannot_apply(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(self.pending, expires_at=0)

        await self.callback()

        self.writer.assert_not_called()
        self.assertIsNone(self.current())
        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_missing_member_expiry_fails_closed(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(self.pending, expires_at=None)

        await self.callback()

        self.writer.assert_not_called()
        self.assertIsNone(self.current())

    async def test_expiry_during_membership_check(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(self.pending, expires_at=100)

        with patch.object(
            workflow,
            "monotonic",
            side_effect=[99, 101, 101],
        ):
            await self.callback()

        self.writer.assert_not_called()
        self.assertIsNone(self.current())

    async def test_expired_member_upload_is_discarded(self):
        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(
            self.pending,
            state="awaiting_upload",
            preview=None,
            expires_at=0,
        )

        self.update.callback_query = None

        self.update.effective_message.document = NS(
            file_name="export.csv",
            file_size=10,
        )

        await tasks.dispatch_import_document(
            self.update,
            self.context,
            workflow.receive_import_document,
        )

        await asyncio.gather(*self.running_tasks)

        self.context.bot.get_file.assert_not_awaited()
        self.writer.assert_not_called()
        self.assertIsNone(self.current())

    async def test_expired_active_session_not_discarded(self):
        active = replace(
            self.pending,
            state="applying",
            expires_at=0,
        )

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = active

        workflow._discard_expired_member_session(
            self.context,
            active,
        )

        self.assertIs(self.current(), active)
        self.writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
