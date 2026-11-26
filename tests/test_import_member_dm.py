from __future__ import annotations

import unittest

from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType
from telegram.error import Forbidden, TimedOut

from handlers import imports as workflow
from handlers.import_access import ImportOrigin
from handlers.import_tasks import (
    IMPORT_TASK_MANAGER_KEY,
    ImportTaskManager,
)


class MemberImportDMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = NS(
            id=20, full_name="Synthetic Member",
            username=None, is_bot=False,
        )

        self.origin = ImportOrigin(20, -100123)
        self.manager = ImportTaskManager()
        self.send = AsyncMock()
        self.reply = AsyncMock()

        self.context = NS(
            user_data={},
            args=[],
            bot=NS(
                username="SyntheticTestBot",
                send_message=self.send,
            ),
            application=NS(
                running=True,
                bot_data={
                    IMPORT_TASK_MANAGER_KEY: self.manager,
                },
            ),
        )

        self.update = NS(
            effective_user=self.user,
            effective_chat=NS(
                id=-100123,
                type=ChatType.SUPERGROUP,
            ),
            effective_message=NS(sender_chat=None),
        )

        self.admission = AsyncMock(return_value=self.origin)
        self.revalidation = AsyncMock(return_value=True)

        for name, mock in (
            ("group_import_origin", self.admission),
            ("verify_member_origin", self.revalidation),
            ("reply_personal_text", self.reply),
        ):
            patcher = patch.object(workflow, name, new=mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def pending(self):
        return self.context.user_data.get(
            workflow.IMPORT_SESSION_KEY
        )

    def activation(self):
        return self.context.user_data.get(
            workflow.IMPORT_ACTIVATION_KEY
        )

    async def request(self):
        await workflow.import_command(
            self.update, self.context
        )

    async def request_activation(self):
        self.send.side_effect = Forbidden("DM unavailable")
        await self.request()

        activation = self.activation()

        self.assertIsNotNone(activation)
        self.assertIsNone(self.pending())

        self.send.side_effect = None
        self.send.reset_mock()

        self.update.effective_chat.type = ChatType.PRIVATE
        self.update.effective_chat.id = self.user.id
        self.context.args = [f"imp_{activation.token}"]

        return activation

    async def activate(self):
        return await workflow.handle_member_import_start(
            self.update, self.context
        )

    async def test_group_import_sent_only_to_member_dm(self):
        await self.request()

        self.send.assert_awaited_once()
        self.reply.assert_not_awaited()

        self.assertEqual(
            self.send.await_args.kwargs["chat_id"], 20
        )

        pending = self.pending()

        self.assertEqual(pending.user_id, 20)
        self.assertEqual(pending.chat_id, 20)
        self.assertEqual(pending.origin, self.origin)
        self.assertEqual(pending.state, "awaiting_upload")

    async def test_unauthorized_group_silent(self):
        self.admission.return_value = None
        await self.request()

        self.send.assert_not_awaited()
        self.reply.assert_not_awaited()
        self.assertIsNone(self.pending())

    async def test_activation_link_after_forbidden_dm(self):
        activation = await self.request_activation()

        self.reply.assert_awaited_once()

        keyboard = self.reply.await_args.kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]

        self.assertTrue(
            button.url.endswith(f"?start=imp_{activation.token}")
        )

        self.assertEqual(activation.origin, self.origin)

    async def test_activation_is_single_use(self):
        await self.request_activation()

        self.assertTrue(await self.activate())
        self.assertIsNone(self.activation())
        self.assertIsNotNone(self.pending())

        pending = self.pending()

        self.assertTrue(await self.activate())
        self.assertIs(self.pending(), pending)

        self.send.assert_awaited_once()
        self.revalidation.assert_awaited_once()

    async def test_activation_cannot_transfer(self):
        await self.request_activation()

        self.user.id = 99
        self.update.effective_chat.id = 99

        self.assertTrue(await self.activate())

        self.send.assert_not_awaited()
        self.revalidation.assert_not_awaited()
        self.assertIsNone(self.pending())

    async def test_expired_activation(self):
        activation = await self.request_activation()

        self.context.user_data[
            workflow.IMPORT_ACTIVATION_KEY
        ] = replace(activation, expires_at=0)

        self.assertTrue(await self.activate())
        self.assertIsNone(self.activation())
        self.assertIsNone(self.pending())
        self.send.assert_not_awaited()

    async def test_membership_revoked_before_activation(self):
        await self.request_activation()
        self.revalidation.return_value = False

        self.assertTrue(await self.activate())
        self.assertIsNone(self.activation())
        self.assertIsNone(self.pending())
        self.send.assert_not_awaited()

    async def test_ambiguous_dm_failure_does_not_arm(self):
        self.send.side_effect = TimedOut()
        await self.request()

        self.assertIsNone(self.pending())
        self.assertIsNone(self.activation())
        self.reply.assert_not_awaited()

    async def test_busy_import_not_replaced(self):
        await self.request()

        active = replace(self.pending(), state="applying")

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = active

        await self.request()

        self.assertIs(self.pending(), active)
        self.assertIn(
            "already in progress",
            self.send.await_args.kwargs["text"],
        )

        self.reply.assert_not_awaited()

    async def test_random_start_has_no_import_authorization(self):
        self.update.effective_chat.type = ChatType.PRIVATE
        self.update.effective_chat.id = self.user.id
        self.context.args = ["imp_forged"]

        self.assertTrue(await self.activate())
        self.assertIsNone(self.pending())
        self.send.assert_not_awaited()

    async def test_normal_start_not_intercepted(self):
        self.assertFalse(await self.activate())
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
