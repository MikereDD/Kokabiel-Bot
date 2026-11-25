from __future__ import annotations

import asyncio
import unittest

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from handlers import import_tasks as tasks
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)


class ImportTaskManagerTests(
    unittest.IsolatedAsyncioTestCase
):

    async def asyncSetUp(self):
        self.manager = tasks.ImportTaskManager()
        self.release = asyncio.Event()
        self.scheduled = []
        self.started = []

        self.application = SimpleNamespace(
            running=True,
            bot_data={
                tasks.IMPORT_TASK_MANAGER_KEY: self.manager,
                "settings": SimpleNamespace(
                    owner_user_ids={1, 2, 3, 4},
                ),
            },
            create_task=self.create_task,
        )

        self.reply = AsyncMock()

        patcher = patch.object(
            tasks,
            "reply_personal_text",
            self.reply,
        )

        patcher.start()
        self.addCleanup(patcher.stop)

        self.handler = AsyncMock(
            side_effect=self.slow_handler
        )

    async def asyncTearDown(self):
        self.release.set()

        await self.manager.drain()

        if self.scheduled:
            await asyncio.gather(
                *self.scheduled,
                return_exceptions=True,
            )

    def create_task(self, coroutine, **kwargs):
        task = asyncio.create_task(coroutine)

        self.scheduled.append(task)

        return task

    async def slow_handler(self, update, context):
        self.started.append(
            update.effective_user.id
        )

        await self.release.wait()

    def fixture(
        self,
        user_id,
        *,
        state="awaiting_upload",
        action="apply",
        token=None,
        chat_type=ChatType.PRIVATE,
    ):
        pending = PendingImport(
            token=f"token-{user_id}",
            user_id=user_id,
            user_name=f"User {user_id}",
            chat_id=user_id,
            state=state,
            preview=None,
        )

        context = SimpleNamespace(
            application=self.application,
            user_data={
                IMPORT_SESSION_KEY: pending,
            },
        )

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=user_id
            ),
            effective_chat=SimpleNamespace(
                id=user_id,
                type=chat_type,
            ),
            callback_query=SimpleNamespace(
                data=(
                    f"imp|{action}|"
                    f"{token or pending.token}"
                ),
                answer=AsyncMock(),
            ),
        )

        return update, context, pending

    async def submit_document(
        self,
        update,
        context,
    ):
        await tasks.dispatch_import_document(
            update,
            context,
            self.handler,
        )

    async def submit_callback(
        self,
        update,
        context,
    ):
        await tasks.dispatch_callback(
            update,
            context,
            self.handler,
        )

    async def finish_operations(self):
        self.release.set()

        await asyncio.gather(
            *self.scheduled,
            return_exceptions=True,
        )

        await asyncio.sleep(0)

    async def test_global_capacity_rejects_third_import(self):
        first = self.fixture(1)
        second = self.fixture(2)
        third = self.fixture(3)

        await self.submit_document(*first[:2])
        await self.submit_document(*second[:2])
        await self.submit_document(*third[:2])

        self.assertEqual(
            self.manager.active_count,
            2,
        )

        self.assertEqual(
            len(self.scheduled),
            2,
        )

        self.assertIn(
            "processing other imports",
            self.reply.await_args.args[2],
        )

        self.assertIs(
            third[1].user_data[IMPORT_SESSION_KEY],
            third[2],
        )

        await self.finish_operations()

        self.assertCountEqual(
            self.started,
            [1, 2],
        )

        self.assertEqual(
            self.manager.active_count,
            0,
        )

        # Capacity becomes available again.
        await self.submit_document(*third[:2])

        await self.finish_operations()

        self.assertCountEqual(
            self.started,
            [1, 2, 3],
        )

    async def test_writer_capacity_preserves_second_approval(self):
        first = self.fixture(
            1,
            state="ready",
        )

        second = self.fixture(
            2,
            state="verified_ready",
            action="apply_verified",
        )

        reader = self.fixture(3)

        await self.submit_callback(*first[:2])
        await self.submit_callback(*second[:2])

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

        self.assertIn(
            "Another import is being applied",
            second[0].callback_query.answer.await_args.args[0],
        )

        self.assertIs(
            second[1].user_data[IMPORT_SESSION_KEY],
            second[2],
        )

        # A preview operation may coexist with one writer.
        await self.submit_document(*reader[:2])

        self.assertEqual(
            self.manager.active_count,
            2,
        )

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        await self.finish_operations()

        self.assertCountEqual(
            self.started,
            [1, 3],
        )

        self.assertEqual(
            self.manager.writer_count,
            0,
        )

        # The rejected approval remains available for retry.
        await self.submit_callback(*second[:2])

        await self.finish_operations()

        self.assertCountEqual(
            self.started,
            [1, 2, 3],
        )

    async def test_same_user_reservation_precedes_task_start(self):
        update, context, pending = self.fixture(
            1
        )

        await self.submit_document(
            update,
            context,
        )

        # The scheduled coroutine has not started yet.
        self.assertEqual(
            self.started,
            [],
        )

        self.assertTrue(
            self.manager.busy(1)
        )

        await self.submit_document(
            update,
            context,
        )

        command_handler = AsyncMock()

        await tasks.dispatch_import_command(
            update,
            context,
            command_handler,
        )

        command_handler.assert_not_called()

        update.callback_query.data = (
            f"imp|cancel|{pending.token}"
        )

        await self.submit_callback(
            update,
            context,
        )

        update.callback_query.answer.assert_awaited_once()

        self.assertIn(
            "already in progress",
            update.callback_query.answer.await_args.args[0],
        )

        self.assertIs(
            context.user_data[IMPORT_SESSION_KEY],
            pending,
        )

        self.assertEqual(
            len(self.scheduled),
            1,
        )

        await self.finish_operations()

        self.handler.assert_awaited_once()

    async def test_replaced_session_never_reaches_handler(self):
        update, context, pending = self.fixture(
            1
        )

        await self.submit_document(
            update,
            context,
        )

        newer = replace(
            pending,
            token="replacement-token",
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = newer

        await self.finish_operations()

        self.handler.assert_not_called()

        self.assertIs(
            context.user_data[IMPORT_SESSION_KEY],
            newer,
        )

        self.assertEqual(
            self.manager.active_count,
            0,
        )

    async def test_closed_or_stopped_application_rejects_work(self):
        update, context, pending = self.fixture(
            1
        )

        self.application.running = False

        await self.submit_document(
            update,
            context,
        )

        self.application.running = True
        self.manager.close()

        await self.submit_document(
            update,
            context,
        )

        self.handler.assert_not_called()

        self.assertEqual(
            len(self.scheduled),
            0,
        )

        self.assertEqual(
            self.reply.await_count,
            2,
        )

        for call in self.reply.await_args_list:
            self.assertIn(
                "shutting down",
                call.args[2],
            )

        self.assertIs(
            context.user_data[IMPORT_SESSION_KEY],
            pending,
        )

    async def test_noneligible_actions_remain_sequential(self):
        unauthorized = self.fixture(
            99,
            state="ready",
        )

        wrong_token = self.fixture(
            1,
            state="ready",
            token="wrong-token",
        )

        group = self.fixture(
            2,
            state="ready",
            chat_type=ChatType.SUPERGROUP,
        )

        normal = self.fixture(
            3,
            state="ready",
        )

        normal[0].callback_query.data = (
            "act|watchlist|m|348"
        )

        fallback = AsyncMock()

        for update, context, _ in (
            unauthorized,
            wrong_token,
            group,
            normal,
        ):
            await tasks.dispatch_callback(
                update,
                context,
                fallback,
            )

        self.assertEqual(
            fallback.await_count,
            4,
        )

        self.assertEqual(
            len(self.scheduled),
            0,
        )

        self.assertEqual(
            self.manager.active_count,
            0,
        )

    async def test_drain_waits_and_rejects_new_work(self):
        first = self.fixture(
            1,
            state="ready",
        )

        second = self.fixture(2)

        await self.submit_callback(*first[:2])

        drain = asyncio.create_task(
            self.manager.drain()
        )

        try:
            await asyncio.sleep(0)

            self.assertFalse(
                self.manager.accepting
            )

            self.assertFalse(
                drain.done()
            )

            await self.submit_document(
                *second[:2]
            )

            self.assertEqual(
                len(self.scheduled),
                1,
            )

            self.assertEqual(
                self.manager.writer_count,
                1,
            )

        finally:
            self.release.set()

            await asyncio.wait_for(
                drain,
                timeout=2,
            )

        self.assertEqual(
            self.started,
            [1],
        )

        self.assertEqual(
            self.manager.active_count,
            0,
        )

    async def test_failed_task_releases_reservation(self):
        update, context, pending = self.fixture(
            1
        )

        failing_handler = AsyncMock(
            side_effect=RuntimeError(
                "Synthetic operation failure"
            )
        )

        error = self.manager.submit(
            update,
            context,
            failing_handler,
            pending,
        )

        self.assertIsNone(error)
        self.assertTrue(self.manager.busy(1))

        results = await asyncio.gather(
            *self.scheduled,
            return_exceptions=True,
        )

        await asyncio.sleep(0)

        self.assertIsInstance(
            results[0],
            RuntimeError,
        )

        self.assertFalse(
            self.manager.busy(1)
        )

        self.assertEqual(
            self.manager.active_count,
            0,
        )

        # A failed task must not consume capacity permanently.
        await self.submit_document(
            update,
            context,
        )

        await self.finish_operations()

        self.handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
