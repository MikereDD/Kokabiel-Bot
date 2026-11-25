from __future__ import annotations

import asyncio
import time
import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType

from handlers import owner
from handlers import import_tasks as tasks
from handlers.common import allowed
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)


class ImportActivityBoundaryTests(
    unittest.IsolatedAsyncioTestCase
):

    async def asyncSetUp(self):
        self.manager = tasks.ImportTaskManager()

        self.release_writer = asyncio.Event()
        self.writer_started = asyncio.Event()
        self.release_flush = asyncio.Event()

        self.events = []
        self.created_tasks = []

        self.database = SimpleNamespace(
            record_chat=AsyncMock(),
            record_user_chat=AsyncMock(),
            mark_chat_inactive=AsyncMock(),
        )

        self.settings = SimpleNamespace(
            owner_user_ids={1},
            allowed_chat_ids={-100123},
        )

        self.application = SimpleNamespace(
            running=True,
            bot_data={
                tasks.IMPORT_TASK_MANAGER_KEY: self.manager,
                "settings": self.settings,
                "database": self.database,
            },
            create_task=self.create_task,
        )

        self.bot = SimpleNamespace(
            leave_chat=AsyncMock(),
            get_chat=AsyncMock(),
        )

    def create_task(self, coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        self.created_tasks.append(task)
        return task

    async def asyncTearDown(self):
        self.release_writer.set()
        self.release_flush.set()

        await self.manager.drain()

        if self.created_tasks:
            await asyncio.gather(
                *self.created_tasks,
                return_exceptions=True,
            )

    def fixture(
        self,
        *,
        user_id=2,
        chat_id=-100123,
        chat_type=ChatType.SUPERGROUP,
        callback=False,
    ):
        query = None

        if callback:
            query = SimpleNamespace(
                data="own|ok|confirmation",
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=user_id,
                full_name=f"User {user_id}",
                username=None,
            ),
            effective_chat=SimpleNamespace(
                id=chat_id,
                type=chat_type,
                title="Synthetic Group",
                username=None,
            ),
            effective_message=SimpleNamespace(
                reply_text=AsyncMock(),
            ),
            callback_query=query,
        )

        context = SimpleNamespace(
            application=self.application,
            user_data={},
            bot=self.bot,
            args=[str(-100123)],
        )

        return update, context

    async def hold_writer(self, *, fail=False):
        update, context = self.fixture(
            user_id=1,
            chat_id=1,
            chat_type=ChatType.PRIVATE,
        )

        pending = PendingImport(
            token="synthetic-writer-token",
            user_id=1,
            user_name="Owner",
            chat_id=1,
            state="ready",
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = pending

        async def writer_handler(update, context):
            self.writer_started.set()

            await self.release_writer.wait()

            if fail:
                raise RuntimeError(
                    "Synthetic writer failure"
                )

        error = self.manager.submit(
            update,
            context,
            writer_handler,
            pending,
            writer=True,
        )

        self.assertIsNone(error)

        await asyncio.wait_for(
            self.writer_started.wait(),
            timeout=2,
        )

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        return self.created_tasks[-1]

    async def finish_writer(self, task):
        self.release_writer.set()

        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=2,
        )

        await asyncio.sleep(0)

    async def test_queue_capacity_and_ordered_persistence(self):
        task = await self.hold_writer()

        limit = tasks.MAX_DEFERRED_ACTIVITY

        self.assertEqual(limit, 32)

        for index in range(limit):
            update, context = self.fixture(
                user_id=index + 100,
            )

            self.assertTrue(
                await allowed(
                    update,
                    context,
                    defer_activity=True,
                )
            )

        self.assertEqual(
            self.manager.deferred_activity_count,
            limit,
        )

        overflow, context = self.fixture(
            user_id=999,
        )

        self.assertFalse(
            await allowed(
                overflow,
                context,
                defer_activity=True,
            )
        )

        overflow.effective_message.reply_text.assert_awaited_once()

        self.assertIn(
            "try",
            overflow.effective_message
            .reply_text.await_args.args[0].casefold(),
        )

        self.database.record_chat.assert_not_awaited()
        self.database.record_user_chat.assert_not_awaited()

        await self.finish_writer(task)

        self.assertEqual(
            self.database.record_chat.await_count,
            limit,
        )

        self.assertEqual(
            [
                call.kwargs["user_id"]
                for call in self.database
                .record_user_chat.await_args_list
            ],
            list(range(100, 100 + limit)),
        )

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

    async def test_authorization_precedes_deferral(self):
        task = await self.hold_writer()

        denied_cases = (
            {
                "user_id": 999,
                "chat_id": 999,
                "chat_type": ChatType.PRIVATE,
            },
            {
                "user_id": 2,
                "chat_id": -100999,
                "chat_type": ChatType.SUPERGROUP,
            },
        )

        for arguments in denied_cases:
            with self.subTest(arguments=arguments):
                update, context = self.fixture(
                    **arguments
                )

                self.assertFalse(
                    await allowed(
                        update,
                        context,
                        defer_activity=True,
                    )
                )

                update.effective_message.reply_text.assert_awaited_once()

                self.assertIn(
                    "not enabled",
                    update.effective_message
                    .reply_text.await_args.args[0],
                )

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )

        await self.finish_writer(task)

        self.database.record_chat.assert_not_awaited()
        self.database.record_user_chat.assert_not_awaited()

    async def test_consistency_dependent_actions_remain_blocked(self):
        task = await self.hold_writer()

        for callback in (False, True):
            update, context = self.fixture(
                callback=callback,
            )

            self.assertFalse(
                await allowed(update, context)
            )

            if callback:
                update.callback_query.answer.assert_awaited_once()
            else:
                update.effective_message.reply_text.assert_awaited_once()

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )

        self.database.record_chat.assert_not_awaited()
        self.database.record_user_chat.assert_not_awaited()

        await self.finish_writer(task)

        update, context = self.fixture()

        self.assertTrue(
            await allowed(update, context)
        )

        self.database.record_chat.assert_awaited_once()
        self.database.record_user_chat.assert_awaited_once()

    async def test_writer_failure_still_flushes_accepted_activity(self):
        task = await self.hold_writer(
            fail=True,
        )

        update, context = self.fixture()

        self.assertTrue(
            await allowed(
                update,
                context,
                defer_activity=True,
            )
        )

        self.release_writer.set()

        with self.assertRaisesRegex(
            RuntimeError,
            "Synthetic writer failure",
        ):
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=2,
            )

        await asyncio.sleep(0)

        self.database.record_chat.assert_awaited_once()
        self.database.record_user_chat.assert_awaited_once()

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

    async def test_activity_failure_does_not_abandon_remaining_queue(self):
        task = await self.hold_writer()

        async def broken_record():
            self.events.append("failed")

            raise RuntimeError(
                "Synthetic activity failure"
            )

        async def next_record():
            self.events.append("continued")

        self.assertTrue(
            self.manager.defer_activity(
                broken_record
            )
        )

        self.assertTrue(
            self.manager.defer_activity(
                next_record
            )
        )

        with self.assertLogs(
            "handlers.import_tasks",
            level="ERROR",
        ):
            await self.finish_writer(task)

        self.assertEqual(
            self.events,
            ["failed", "continued"],
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

    async def test_owner_leave_waits_for_activity_flush(self):
        task = await self.hold_writer()

        group_update, group_context = self.fixture()

        self.assertTrue(
            await allowed(
                group_update,
                group_context,
                defer_activity=True,
            )
        )

        update, context = self.fixture(
            user_id=1,
        )

        await owner.leave(update, context)

        update.effective_message.reply_text.assert_awaited_once()

        self.assertNotIn(
            "kokabiel_owner_confirmations",
            context.user_data,
        )

        update, context = self.fixture(
            user_id=1,
            chat_id=1,
            chat_type=ChatType.PRIVATE,
            callback=True,
        )

        confirmations = {
            "confirmation": {
                "action": "leave",
                "target_chat_id": -100123,
                "target_title": "Synthetic Group",
                "created_at": time.time(),
            }
        }

        context.user_data[
            "kokabiel_owner_confirmations"
        ] = confirmations

        await owner.owner_callback_router(
            update,
            context,
        )

        self.bot.leave_chat.assert_not_awaited()

        self.database.mark_chat_inactive.assert_not_awaited()

        self.assertIn(
            "confirmation",
            confirmations,
        )

        await self.finish_writer(task)

        self.database.record_chat.assert_awaited_once()
        self.database.record_user_chat.assert_awaited_once()

        await owner.owner_callback_router(
            update,
            context,
        )

        self.bot.leave_chat.assert_awaited_once_with(
            -100123
        )

        self.database.mark_chat_inactive.assert_awaited_once_with(
            -100123
        )

        self.assertNotIn(
            "confirmation",
            confirmations,
        )

    async def test_cancelled_writer_finishes_accepted_activity(self):
        task = await self.hold_writer()

        flush_started = asyncio.Event()

        async def first_record():
            flush_started.set()

            await self.release_flush.wait()

            self.events.append("first")

        async def second_record():
            self.events.append("second")

        self.assertTrue(
            self.manager.defer_activity(
                first_record
            )
        )

        self.assertTrue(
            self.manager.defer_activity(
                second_record
            )
        )

        self.release_writer.set()

        await asyncio.wait_for(
            flush_started.wait(),
            timeout=2,
        )

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        self.assertFalse(
            self.manager.defer_activity(
                second_record
            )
        )

        task.cancel()

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        try:
            self.assertFalse(
                task.done(),
                "Writer cancellation interrupted "
                "accepted activity flushing.",
            )

            self.assertEqual(
                self.manager.writer_count,
                1,
            )

        finally:
            self.release_flush.set()

        with self.assertRaises(
            asyncio.CancelledError
        ):
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=2,
            )

        await asyncio.sleep(0)

        self.assertEqual(
            self.events,
            ["first", "second"],
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )


    async def test_repeated_writer_cancellation_during_flush(self):
        task = await self.hold_writer()

        flush_started = asyncio.Event()

        async def first_record():
            flush_started.set()
            await self.release_flush.wait()
            self.events.append("first")

        async def second_record():
            self.events.append("second")

        self.assertTrue(
            self.manager.defer_activity(first_record)
        )

        self.assertTrue(
            self.manager.defer_activity(second_record)
        )

        self.release_writer.set()

        await asyncio.wait_for(
            flush_started.wait(),
            timeout=2,
        )

        try:
            for index in range(5):
                task.cancel()

                await asyncio.sleep(0)
                await asyncio.sleep(0)

                self.assertFalse(
                    task.done(),
                    f"Writer released after cancellation {index + 1}",
                )

                self.assertEqual(
                    self.manager.writer_count,
                    1,
                )

                self.assertFalse(
                    self.manager.defer_activity(
                        second_record
                    )
                )

                self.assertEqual(
                    self.events,
                    [],
                )

        finally:
            self.release_flush.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=2,
            )

        await asyncio.sleep(0)

        self.assertEqual(
            self.events,
            ["first", "second"],
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )

    async def test_prestart_writer_cancellation_cannot_orphan_activity(self):
        update, context = self.fixture(
            user_id=1,
            chat_id=1,
            chat_type=ChatType.PRIVATE,
        )

        pending = PendingImport(
            token="prestart-cancellation-token",
            user_id=1,
            user_name="Owner",
            chat_id=1,
            state="ready",
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = pending

        async def writer_handler(update, context):
            self.writer_started.set()

        error = self.manager.submit(
            update,
            context,
            writer_handler,
            pending,
            writer=True,
        )

        self.assertIsNone(error)

        task = self.created_tasks[-1]

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        self.assertFalse(
            self.writer_started.is_set()
        )

        group_update, group_context = self.fixture()

        try:
            # Do not accept deferred records until the
            # writer has entered its protected lifecycle.
            accepted = await allowed(
                group_update,
                group_context,
                defer_activity=True,
            )

        finally:
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

            await asyncio.sleep(0)

        self.assertFalse(
            self.writer_started.is_set()
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

        self.assertFalse(
            accepted,
            "Activity was accepted before the writer "
            "entered its protected lifecycle.",
        )

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )

        group_update.effective_message.reply_text.assert_awaited_once()

        self.database.record_chat.assert_not_awaited()
        self.database.record_user_chat.assert_not_awaited()

        # A later writer must not inherit activity
        # belonging to the cancelled reservation.
        next_writer = await self.hold_writer()

        await self.finish_writer(next_writer)

        self.database.record_chat.assert_not_awaited()
        self.database.record_user_chat.assert_not_awaited()

        self.assertEqual(
            self.manager.deferred_activity_count,
            0,
        )


if __name__ == "__main__":
    unittest.main()
