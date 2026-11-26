from __future__ import annotations

import asyncio
import tempfile
import unittest

from pathlib import Path
from time import monotonic
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from handlers import imports as workflow
from handlers import import_tasks as tasks
from handlers.import_access import ImportOrigin

from imports.db_apply import apply_database_import
from imports.models import NormalizedImportRecord
from imports.undo import list_import_batches

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class ImportUndoTelegramTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.db = self.root / "synthetic.db"

        create_database(self.db)

        resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Synthetic Film",
                    year=2000,
                    watched=True,
                    rating=8.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        for user_id in (20, 99):
            approved = make_preview(
                self.db,
                resolution,
                user_id=user_id,
            )

            apply_database_import(
                approved,
                database_path=self.db,
                backup_path=(
                    self.root / f"import-{user_id}.db"
                ),
                user_name=f"User {user_id}",
                origin_group_id=-100123,
            )

        self.manager = tasks.ImportTaskManager()
        self.running_tasks = []

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

        self.context = NS(
            user_data={
                workflow.IMPORT_SESSION_KEY:
                    workflow.PendingImport(
                        token="initial",
                        user_id=20,
                        user_name="Synthetic Member",
                        chat_id=20,
                        state="awaiting_upload",
                        origin=ImportOrigin(20, -100123),
                        expires_at=monotonic() + 3600,
                    ),
            },
            bot=NS(
                get_chat_member=AsyncMock(
                    return_value=self.member,
                ),
            ),
            application=NS(
                running=True,
                bot_data={
                    tasks.IMPORT_TASK_MANAGER_KEY:
                        self.manager,
                    "settings": self.settings,
                },
                create_task=self.create_task,
            ),
        )

        self.query = NS(
            data="",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        self.update = NS(
            effective_user=self.user,
            effective_chat=NS(
                id=20,
                type=ChatType.PRIVATE,
            ),
            callback_query=self.query,
        )

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

    async def click(self, action, token=None):
        token = (
            token if token is not None
            else self.current().token
        )

        self.query.data = f"imp|{action}|{token}"

        await tasks.dispatch_callback(
            self.update,
            self.context,
            workflow.import_callback_router,
        )

        if self.running_tasks:
            await asyncio.gather(*self.running_tasks)

        await asyncio.sleep(0)

    async def history(self):
        await self.click("history")

    async def preview(self):
        await self.history()
        await self.click("undo_1")

    async def test_history_is_user_scoped(self):
        await self.history()

        self.assertEqual(
            self.current().state,
            "undo_history",
        )

        markup = self.query.edit_message_text\
            .await_args.kwargs["reply_markup"]

        values = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

        self.assertTrue(
            any("imp|undo_1|" in value for value in values)
        )

        self.assertFalse(
            any("imp|undo_2|" in value for value in values)
        )

    async def test_preview_requires_confirmation(self):
        await self.preview()

        self.assertEqual(
            self.current().state,
            "undo_ready",
        )

        self.assertEqual(
            self.current().undo_preview.user_id,
            20,
        )

        self.assertEqual(
            self.current().undo_preview.restore_count,
            1,
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_confirm_reverses_only_own_batch(self):
        await self.preview()
        await self.click("undo_apply")

        self.assertIsNone(self.current())

        self.assertEqual(
            list_import_batches(self.db, user_id=20),
            (),
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=99
            )),
            1,
        )

    async def test_cannot_select_other_members_batch(self):
        await self.history()
        await self.click("undo_2")

        self.assertIsNone(self.current())

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=99
            )),
            1,
        )

    async def test_cancel_preserves_import(self):
        await self.preview()
        await self.click("cancel")

        self.assertIsNone(self.current())

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_stale_token_cannot_confirm(self):
        await self.history()
        old_token = self.current().token

        await self.click("undo_1")
        current = self.current()

        await self.click(
            "undo_apply",
            token=old_token,
        )

        self.assertIs(self.current(), current)

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_departed_member_cannot_confirm(self):
        await self.preview()

        self.member.status = "left"

        await self.click("undo_apply")

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_unreserved_undo_denied(self):
        await self.preview()

        self.query.data = (
            f"imp|undo_apply|{self.current().token}"
        )

        await workflow.import_callback_router(
            self.update,
            self.context,
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_group_undo_denied(self):
        await self.preview()

        self.update.effective_chat.type = (
            ChatType.SUPERGROUP
        )
        self.update.effective_chat.id = -100123

        await self.click("undo_apply")

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_no_session_history_denied(self):
        self.context.user_data.clear()

        await self.click(
            "history",
            token="forged",
        )

        self.query.edit_message_text.assert_not_awaited()

        self.assertIsNone(self.current())


    async def test_back_to_upload_rotates_token(self):
        await self.history()

        old = self.current()

        await self.click("upload")

        current = self.current()

        self.assertEqual(
            current.state,
            "awaiting_upload",
        )

        self.assertNotEqual(current.token, old.token)
        self.assertEqual(current.origin, old.origin)
        self.assertEqual(current.expires_at, old.expires_at)
        self.assertIsNone(current.undo_preview)

        await self.click("undo_1", token=old.token)

        self.assertIs(self.current(), current)

    async def test_back_from_undo_preview_preserves_import(self):
        await self.preview()

        old_token = self.current().token

        await self.click("upload")

        self.assertEqual(
            self.current().state,
            "awaiting_upload",
        )

        self.assertIsNone(self.current().undo_preview)

        await self.click(
            "undo_apply",
            token=old_token,
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_membership_revoked_while_loading_history(self):
        original = workflow._run_import_worker

        async def worker(function, *args, **kwargs):
            result = await original(
                function, *args, **kwargs
            )

            self.member.status = "left"

            return result

        with patch.object(
            workflow,
            "_run_import_worker",
            side_effect=worker,
        ):
            await self.history()

        self.assertIsNone(self.current())

        self.assertIn(
            "authorization is unavailable",
            self.query.edit_message_text.await_args.args[0],
        )

    async def test_import_session_does_not_grant_general_dm_access(self):
        from handlers.common import allowed

        self.update.callback_query = None

        self.update.effective_message = NS(
            reply_text=AsyncMock(),
        )

        self.assertFalse(
            await allowed(
                self.update,
                self.context,
            )
        )

        self.update.effective_message.reply_text.assert_awaited_once()

        self.assertIsNotNone(self.current())

        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_owner_can_manage_own_import(self):
        from dataclasses import replace

        self.settings.owner_user_ids.add(20)

        self.context.user_data[
            workflow.IMPORT_SESSION_KEY
        ] = replace(
            self.current(),
            origin=None,
            expires_at=None,
        )

        with patch.object(
            workflow,
            "allowed",
            new=AsyncMock(return_value=True),
        ):
            await self.preview()
            await self.click("undo_apply")

        self.assertIsNone(self.current())

        self.assertEqual(
            list_import_batches(
                self.db, user_id=20
            ),
            (),
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=99
            )),
            1,
        )

        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_largest_batch_callback_fits_telegram_limit(self):
        token = "a" * 32
        batch = 9223372036854775807

        for action in (
            f"history_{batch}",
            f"undo_{batch}",
            "undo_apply",
            "upload",
        ):
            self.assertLessEqual(
                len(f"imp|{action}|{token}".encode()),
                64,
            )



    async def test_history_pagination_is_complete_and_user_scoped(self):
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.db)) as db, db:
            for _ in range(10):
                cursor = db.execute(
                    """
                    INSERT INTO import_batches (
                        user_id, source, origin_group_id
                    ) VALUES (20, 'imdb', -100123)
                    """
                )

                db.execute(
                    """
                    INSERT INTO import_title_changes (
                        batch_id, media_type, tmdb_id,
                        before_json, after_json, invalidated
                    )
                    SELECT ?, media_type, tmdb_id,
                           before_json, after_json, invalidated
                    FROM import_title_changes
                    WHERE batch_id = 1
                    """,
                    (cursor.lastrowid,),
                )

        expected = list(range(12, 2, -1)) + [1]

        def buttons():
            markup = self.query.edit_message_text                .await_args.kwargs["reply_markup"]

            return [
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
            ]

        def batch_ids(values):
            return [
                int(value.split("|")[1].split("_")[1])
                for value in values
                if value.startswith("imp|undo_")
            ]

        await self.history()

        first = buttons()

        self.assertEqual(
            batch_ids(first), expected[:8]
        )

        older = next(
            value for value in first
            if value.startswith("imp|history_")
        )

        await self.click(older.split("|")[1])

        second = buttons()

        self.assertEqual(
            batch_ids(second), expected[8:]
        )

        self.assertEqual(
            batch_ids(first) + batch_ids(second),
            expected,
        )

        self.assertNotIn(
            2, batch_ids(first) + batch_ids(second)
        )

        await self.click("history")

        self.assertEqual(
            batch_ids(buttons()), expected[:8]
        )

    async def test_competing_undo_writers_are_serialized(self):
        from dataclasses import replace
        from imports.undo import preview_import_undo

        await self.preview()

        approved = self.current()

        other_pending = replace(
            approved,
            token="other-member",
            user_id=99,
            user_name="Other Member",
            chat_id=99,
            origin=ImportOrigin(99, -100123),
            undo_preview=preview_import_undo(
                self.db,
                user_id=99,
                batch_id=2,
            ),
        )

        other_user = NS(
            id=99,
            full_name="Other Member",
            username=None,
            is_bot=False,
        )

        other_query = NS(
            data="imp|undo_apply|other-member",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        other_update = NS(
            effective_user=other_user,
            effective_chat=NS(
                id=99,
                type=ChatType.PRIVATE,
            ),
            callback_query=other_query,
        )

        other_context = NS(
            user_data={
                workflow.IMPORT_SESSION_KEY: other_pending,
            },
            bot=NS(
                get_chat_member=AsyncMock(
                    return_value=NS(
                        user=other_user,
                        status="member",
                    ),
                ),
            ),
            application=self.context.application,
        )

        entered = asyncio.Event()
        release = asyncio.Event()
        original = workflow._run_import_worker
        writer_calls = []

        async def worker(function, *args, **kwargs):
            if function is workflow.apply_import_undo:
                writer_calls.append(args[0].user_id)
                entered.set()
                await release.wait()

            return await original(
                function, *args, **kwargs
            )

        with patch.object(
            workflow,
            "_run_import_worker",
            side_effect=worker,
        ):
            first = asyncio.create_task(
                self.click("undo_apply")
            )

            try:
                await asyncio.wait_for(
                    entered.wait(), timeout=5
                )

                self.assertEqual(
                    self.manager.writer_count, 1
                )

                # Replay the same confirmation while its
                # writer reservation is active.

                self.query.data = (
                    f"imp|undo_apply|{approved.token}"
                )

                await tasks.dispatch_callback(
                    self.update,
                    self.context,
                    workflow.import_callback_router,
                )

                self.assertIn(
                    "already in progress",
                    self.query.answer.await_args.args[0],
                )

                # Another user's valid approval cannot
                # acquire a second writer reservation.

                await tasks.dispatch_callback(
                    other_update,
                    other_context,
                    workflow.import_callback_router,
                )

                self.assertIn(
                    "Another import",
                    other_query.answer.await_args.args[0],
                )

                self.assertIs(
                    other_context.user_data[
                        workflow.IMPORT_SESSION_KEY
                    ],
                    other_pending,
                )

                self.assertEqual(writer_calls, [20])
                self.assertEqual(
                    self.manager.writer_count, 1
                )

            finally:
                release.set()
                await first

        self.assertEqual(writer_calls, [20])
        self.assertEqual(self.manager.writer_count, 0)

        self.assertEqual(
            list_import_batches(self.db, user_id=20),
            (),
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=99
            )),
            1,
        )

        # The second approval remains valid and may be
        # retried after the first writer has released.

        await tasks.dispatch_callback(
            other_update,
            other_context,
            workflow.import_callback_router,
        )

        await asyncio.gather(*self.running_tasks)
        await asyncio.sleep(0)

        self.assertEqual(
            list_import_batches(self.db, user_id=99),
            (),
        )

        self.assertEqual(self.manager.writer_count, 0)

    async def test_membership_revoked_during_undo_confirmation(self):
        await self.preview()

        async def acknowledge(*args, **kwargs):
            self.member.status = "left"

        self.query.answer.side_effect = acknowledge

        with patch.object(
            workflow,
            "apply_import_undo",
        ) as writer:
            await self.click("undo_apply")
            writer.assert_not_called()

        self.assertIsNone(self.current())

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

        self.assertEqual(
            list(self.root.rglob("preundo-*.db")),
            [],
        )

    async def test_session_expires_during_undo_confirmation(self):
        await self.preview()

        deadline = self.current().expires_at
        clock = [deadline - 1]

        async def acknowledge(*args, **kwargs):
            clock[0] = deadline + 1

        self.query.answer.side_effect = acknowledge

        with (
            patch.object(
                workflow,
                "monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(
                workflow,
                "apply_import_undo",
            ) as writer,
        ):
            await self.click("undo_apply")

            writer.assert_not_called()

        self.assertIsNone(self.current())

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_shutdown_rejects_new_undo_writer(self):
        await self.preview()

        approved = self.current()

        self.manager.close()

        with patch.object(
            workflow,
            "apply_import_undo",
        ) as writer:
            await self.click("undo_apply")

            writer.assert_not_called()

        self.assertIs(self.current(), approved)

        self.assertIn(
            "shutting down",
            self.query.answer.await_args.args[0],
        )

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

    async def test_stale_undo_approval_preserves_manual_rating(self):
        import sqlite3
        from contextlib import closing

        await self.preview()

        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                """
                UPDATE ratings
                SET rating = 6
                WHERE user_id = 20
                """
            )

        await self.click("undo_apply")

        with closing(sqlite3.connect(self.db)) as db:
            ratings = db.execute(
                """
                SELECT user_id, rating
                FROM ratings
                ORDER BY user_id
                """
            ).fetchall()

        self.assertEqual(
            ratings, [(20, 6), (99, 8)]
        )

        self.assertIsNone(self.current())

        self.assertEqual(
            len(list_import_batches(
                self.db, user_id=20
            )),
            1,
        )

        self.assertEqual(
            list(self.root.rglob("preundo-*.db")),
            [],
        )



if __name__ == "__main__":
    unittest.main()
