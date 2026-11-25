from __future__ import annotations

import asyncio
import time
import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.constants import ChatType

import app

from handlers import owner
from handlers import import_tasks as tasks
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)


RESTART_REQUESTED_KEY = "kokabiel_restart_requested"


class OwnerLifecycleTests(
    unittest.IsolatedAsyncioTestCase
):

    async def asyncSetUp(self):
        self.manager = tasks.ImportTaskManager()

        self.application = SimpleNamespace(
            running=True,
            bot_data={
                tasks.IMPORT_TASK_MANAGER_KEY: self.manager,
                "settings": SimpleNamespace(
                    owner_user_ids={1},
                ),
            },
            create_task=self.create_task,
            stop_running=Mock(),
        )

        self.query = SimpleNamespace(
            data="own|ok|confirmation",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(
                id=1,
                type=ChatType.PRIVATE,
            ),
            effective_message=SimpleNamespace(
                reply_text=AsyncMock(),
            ),
            callback_query=self.query,
        )

        self.context = SimpleNamespace(
            application=self.application,
            user_data={},
        )

        self.release = asyncio.Event()
        self.started = asyncio.Event()

    @staticmethod
    def create_task(coroutine, **kwargs):
        return asyncio.create_task(coroutine)

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.drain()

    async def hold_import(self):
        pending = PendingImport(
            token="synthetic-token",
            user_id=1,
            user_name="Owner",
            chat_id=1,
            state="ready",
        )

        self.context.user_data[
            IMPORT_SESSION_KEY
        ] = pending

        async def active_import(update, context):
            self.started.set()
            await self.release.wait()

        error = self.manager.submit(
            self.update,
            self.context,
            active_import,
            pending,
            writer=True,
        )

        self.assertIsNone(error)

        await asyncio.wait_for(
            self.started.wait(),
            timeout=2,
        )

    def confirmation(self, action):
        self.context.user_data[
            "kokabiel_owner_confirmations"
        ] = {
            "confirmation": {
                "action": action,
                "target_chat_id": 1,
                "target_title": "Kokabiel",
                "created_at": time.time(),
            },
        }

    async def test_reload_refused_during_import(self):
        await self.hold_import()

        self.update.callback_query = None

        old_settings = self.application.bot_data[
            "settings"
        ]

        with patch.object(
            owner,
            "load_settings",
            side_effect=owner.ConfigError(
                "Synthetic settings load"
            ),
        ) as load, patch.object(
            owner.aiohttp,
            "ClientSession",
        ) as session:

            await owner.reload_config(
                self.update,
                self.context,
            )

            load.assert_not_called()
            session.assert_not_called()

        self.assertIs(
            self.application.bot_data["settings"],
            old_settings,
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

        message = (
            self.update.effective_message
            .reply_text.await_args.args[0]
        )

        self.assertIn(
            "import",
            message.casefold(),
        )

    async def exercise_stop_request(
        self,
        action,
    ):
        await self.hold_import()

        self.confirmation(action)

        def verify_stop():
            self.assertFalse(
                self.manager.accepting
            )

            self.assertEqual(
                self.manager.active_count,
                1,
            )

            self.assertFalse(
                self.release.is_set()
            )

        self.application.stop_running.side_effect = (
            verify_stop
        )

        # Prevent the legacy delayed process actions
        # from ever executing during the RED tests.

        def suppress_legacy_task(
            coroutine,
            *args,
            **kwargs,
        ):
            coroutine.close()
            return Mock()

        with patch.object(
            owner.asyncio,
            "create_task",
            side_effect=suppress_legacy_task,
        ) as legacy_task, patch(
            "os.execv"
        ) as execute, patch(
            "os.kill"
        ) as signal_process:

            await owner.owner_callback_router(
                self.update,
                self.context,
            )

            legacy_task.assert_not_called()
            execute.assert_not_called()
            signal_process.assert_not_called()

        self.application.stop_running.assert_called_once()

        self.assertFalse(
            self.manager.accepting
        )

        self.assertEqual(
            self.application.bot_data.get(
                RESTART_REQUESTED_KEY,
                False,
            ),
            action == "restart",
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

    async def test_restart_uses_graceful_stop(self):
        await self.exercise_stop_request(
            "restart"
        )

    async def test_shutdown_uses_graceful_stop(self):
        await self.exercise_stop_request(
            "shutdown"
        )

    async def test_cancelled_drain_preserves_active_import(self):
        await self.hold_import()

        drain = asyncio.create_task(
            self.manager.drain()
        )

        await asyncio.sleep(0)

        self.assertFalse(
            self.manager.accepting
        )

        drain.cancel()

        await asyncio.gather(
            drain,
            return_exceptions=True,
        )

        self.assertEqual(
            self.manager.active_count,
            1,
            "Cancelling drain interrupted an admitted import.",
        )

        self.assertEqual(
            self.manager.writer_count,
            1,
        )

        self.release.set()

        await self.manager.drain()

        self.assertEqual(
            self.manager.active_count,
            0,
        )


    async def test_extended_first_stop_request_wins(self):
        await self.hold_import()

        self.confirmation("restart")

        await owner.owner_callback_router(
            self.update,
            self.context,
        )

        self.assertTrue(
            self.application.bot_data[RESTART_REQUESTED_KEY]
        )

        self.assertFalse(self.manager.accepting)

        self.confirmation("shutdown")

        await owner.owner_callback_router(
            self.update,
            self.context,
        )

        self.application.stop_running.assert_called_once()

        self.assertTrue(
            self.application.bot_data[RESTART_REQUESTED_KEY]
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

    async def test_extended_unauthorized_owner_action(self):
        await self.hold_import()

        self.confirmation("restart")

        self.update.effective_user.id = 99

        await owner.owner_callback_router(
            self.update,
            self.context,
        )

        self.application.stop_running.assert_not_called()

        self.assertTrue(self.manager.accepting)

        self.assertFalse(
            self.application.bot_data.get(
                RESTART_REQUESTED_KEY,
                False,
            )
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

    async def test_extended_group_confirmation_rejected(self):
        await self.hold_import()

        self.confirmation("restart")

        self.update.effective_chat.type = (
            ChatType.SUPERGROUP
        )

        self.update.effective_chat.id = -100123

        await owner.owner_callback_router(
            self.update,
            self.context,
        )

        self.application.stop_running.assert_not_called()

        self.assertTrue(self.manager.accepting)

        self.assertFalse(
            self.application.bot_data.get(
                RESTART_REQUESTED_KEY,
                False,
            )
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

    async def test_extended_failed_delivery_does_not_stop(self):
        await self.hold_import()

        for method in (
            self.query.answer,
            self.query.edit_message_text,
        ):
            with self.subTest(method=method):
                self.confirmation("restart")

                method.side_effect = RuntimeError(
                    "Synthetic confirmation delivery failure"
                )

                try:
                    with self.assertRaises(RuntimeError):
                        await owner.owner_callback_router(
                            self.update,
                            self.context,
                        )

                finally:
                    method.side_effect = None

                self.application.stop_running.assert_not_called()

                self.assertTrue(self.manager.accepting)

                self.assertFalse(
                    self.application.bot_data.get(
                        RESTART_REQUESTED_KEY,
                        False,
                    )
                )

                self.assertEqual(
                    self.manager.active_count,
                    1,
                )

    async def test_extended_external_stop_during_confirmation(self):
        await self.hold_import()

        self.confirmation("restart")

        async def external_stop(*args, **kwargs):
            self.application.running = False

        self.query.edit_message_text.side_effect = (
            external_stop
        )

        await owner.owner_callback_router(
            self.update,
            self.context,
        )

        self.application.stop_running.assert_not_called()

        self.assertFalse(
            self.application.bot_data.get(
                RESTART_REQUESTED_KEY,
                False,
            )
        )

        self.assertEqual(
            self.manager.active_count,
            1,
        )

    async def test_extended_reload_refused_when_stopping(self):
        self.manager.close()

        self.update.callback_query = None

        with patch.object(
            owner,
            "load_settings",
        ) as load, patch.object(
            owner.aiohttp,
            "ClientSession",
        ) as session:

            await owner.reload_config(
                self.update,
                self.context,
            )

            load.assert_not_called()
            session.assert_not_called()

        self.assertIn(
            "shutting down",
            self.update.effective_message
            .reply_text.await_args.args[0],
        )


class RestartEntrypointTests(unittest.TestCase):

    def exercise_main(
        self,
        *,
        restart,
        polling_failure=False,
    ):
        events = []

        def polling(**kwargs):
            events.append("polling")

            if polling_failure:
                raise RuntimeError(
                    "Synthetic shutdown failure"
                )

            events.append(
                "shutdown-complete"
            )

        application = SimpleNamespace(
            bot_data={
                RESTART_REQUESTED_KEY: restart,
            },
            run_polling=Mock(
                side_effect=polling
            ),
        )

        def execute(*args):
            events.append("exec")

            self.assertIn(
                "shutdown-complete",
                events,
            )

        with patch.object(
            app,
            "build_application",
            return_value=application,
        ), patch(
            "os.execv",
            side_effect=execute,
        ) as execv:

            if polling_failure:
                with self.assertRaises(RuntimeError):
                    app.main()
            else:
                app.main()

            if restart and not polling_failure:
                execv.assert_called_once()
            else:
                execv.assert_not_called()

        return events

    def test_restart_executes_after_polling_returns(self):
        self.assertEqual(
            self.exercise_main(restart=True),
            [
                "polling",
                "shutdown-complete",
                "exec",
            ],
        )

    def test_normal_shutdown_does_not_restart(self):
        self.assertEqual(
            self.exercise_main(restart=False),
            [
                "polling",
                "shutdown-complete",
            ],
        )

    def test_failed_shutdown_does_not_restart(self):
        self.assertEqual(
            self.exercise_main(
                restart=True,
                polling_failure=True,
            ),
            ["polling"],
        )


if __name__ == "__main__":
    unittest.main()
