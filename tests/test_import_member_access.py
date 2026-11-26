from __future__ import annotations

import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType
from telegram.error import Forbidden, TimedOut

from handlers import import_access as access


class ImportMemberAccessTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        self.user = SimpleNamespace(
            id=20,
            is_bot=False,
        )

        self.chat = SimpleNamespace(
            id=-100123,
            type=ChatType.SUPERGROUP,
        )

        self.message = SimpleNamespace(
            sender_chat=None,
        )

        self.update = SimpleNamespace(
            effective_user=self.user,
            effective_chat=self.chat,
            effective_message=self.message,
        )

        self.settings = SimpleNamespace(
            allowed_chat_ids={-100123},
            owner_user_ids={1},
        )

        self.member = SimpleNamespace(
            user=self.user,
            status="member",
            is_member=True,
        )

        self.lookup = AsyncMock(
            return_value=self.member,
        )

        self.context = SimpleNamespace(
            bot=SimpleNamespace(
                get_chat_member=self.lookup,
            )
        )

        patcher = patch.object(
            access,
            "settings",
            return_value=self.settings,
        )

        patcher.start()
        self.addCleanup(patcher.stop)

    async def origin(self):
        return await access.group_import_origin(
            self.update,
            self.context,
        )

    async def test_regular_group_member(self):
        result = await self.origin()

        self.assertEqual(
            result,
            access.ImportOrigin(
                user_id=20,
                group_id=-100123,
            ),
        )

        self.lookup.assert_awaited_once_with(
            chat_id=-100123,
            user_id=20,
        )

    async def test_group_admin_and_creator(self):
        for status in (
            "administrator",
            "creator",
        ):
            with self.subTest(status=status):
                self.member.status = status
                self.assertIsNotNone(
                    await self.origin()
                )

    async def test_unauthorized_group(self):
        self.chat.id = -100999

        self.assertIsNone(await self.origin())
        self.lookup.assert_not_awaited()

    async def test_empty_allowlist_denies_member(self):
        self.settings.allowed_chat_ids = set()

        self.assertIsNone(await self.origin())
        self.lookup.assert_not_awaited()

    async def test_random_private_user(self):
        self.chat.type = ChatType.PRIVATE
        self.chat.id = self.user.id

        self.assertIsNone(await self.origin())
        self.lookup.assert_not_awaited()

    async def test_departed_and_banned_members(self):
        for status in ("left", "kicked"):
            with self.subTest(status=status):
                self.member.status = status
                self.assertIsNone(
                    await self.origin()
                )

    async def test_restricted_member(self):
        self.member.status = "restricted"
        self.member.is_member = True

        self.assertIsNotNone(await self.origin())

        self.member.is_member = False

        self.assertIsNone(await self.origin())

    async def test_unknown_member_status(self):
        self.member.status = "unknown"

        self.assertIsNone(await self.origin())

    async def test_membership_lookup_failure(self):
        for error in (
            Forbidden("Membership unavailable"),
            TimedOut(),
        ):
            with self.subTest(error=type(error)):
                self.lookup.side_effect = error
                self.assertIsNone(
                    await self.origin()
                )

    async def test_mismatched_membership_identity(self):
        self.member.user = SimpleNamespace(
            id=999,
            is_bot=False,
        )

        self.assertIsNone(await self.origin())

    async def test_anonymous_group_sender(self):
        self.message.sender_chat = self.chat

        self.assertIsNone(await self.origin())
        self.lookup.assert_not_awaited()

    async def test_bot_cannot_import(self):
        self.user.is_bot = True

        self.assertIsNone(await self.origin())
        self.lookup.assert_not_awaited()

    async def test_grant_not_transferable(self):
        origin = await self.origin()

        self.assertIsNotNone(origin)

        self.lookup.reset_mock()

        self.assertFalse(
            await access.verify_member_origin(
                self.context,
                origin,
                999,
            )
        )

        self.lookup.assert_not_awaited()

    async def test_group_authorization_revoked(self):
        origin = await self.origin()

        self.assertIsNotNone(origin)

        self.settings.allowed_chat_ids.clear()

        self.lookup.reset_mock()

        self.assertFalse(
            await access.verify_member_origin(
                self.context,
                origin,
                20,
            )
        )

        self.lookup.assert_not_awaited()

    async def test_membership_revoked_after_admission(self):
        origin = await self.origin()

        self.assertIsNotNone(origin)

        self.member.status = "left"

        self.assertFalse(
            await access.verify_member_origin(
                self.context,
                origin,
                20,
            )
        )

    async def test_private_identity_boundary(self):
        self.assertIsNone(
            access.private_import_identity(
                self.update
            )
        )

        self.chat.type = ChatType.PRIVATE
        self.chat.id = self.user.id

        self.assertEqual(
            access.private_import_identity(
                self.update
            ),
            20,
        )

        self.chat.id = 999

        self.assertIsNone(
            access.private_import_identity(
                self.update
            )
        )

        self.chat.id = 20
        self.user.is_bot = True

        self.assertIsNone(
            access.private_import_identity(
                self.update
            )
        )



    async def test_allowlist_removed_during_membership_lookup(self):
        async def lookup(**kwargs):
            self.settings.allowed_chat_ids.clear()
            return self.member

        self.lookup.side_effect = lookup

        self.assertIsNone(await self.origin())

        self.lookup.assert_awaited_once()



if __name__ == "__main__":
    unittest.main()
