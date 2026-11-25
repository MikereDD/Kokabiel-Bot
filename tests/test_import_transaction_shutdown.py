from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import CallbackQuery, Message
from telegram.ext import CallbackContext, ExtBot

import app
import imports.db_apply as writer

from handlers.commands import HELP_TEXT
from handlers.import_tasks import import_task_manager
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)

from imports.db_preview import load_existing_user_snapshot
from imports.models import NormalizedImportRecord

from tests import test_import_dispatch_responsiveness as dispatch
from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
)


class ImportTransactionShutdownTests(
    unittest.IsolatedAsyncioTestCase
):

    async def exercise_transaction(
        self,
        *,
        fail,
        real_bookkeeping=False,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        root = Path(temporary.name)
        database = root / "synthetic.db"

        create_database(database)

        before = load_existing_user_snapshot(
            database,
            user_id=1,
        )

        resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        approved = make_preview(
            database,
            resolution,
        )

        config = SimpleNamespace(
            bot_token="123456:synthetic-test-token",
            database_path=database,
            log_path=root / "synthetic.log",
            log_level="INFO",
            local_bot_api_url=None,
            local_bot_api_file_url=None,
            owner_user_ids={1},
            allowed_chat_ids=set(),
        )

        entered = threading.Event()
        release = threading.Event()

        help_completed = asyncio.Event()
        resource_closed = asyncio.Event()

        original_apply_item = writer._apply_item
        original_help = app.help_command

        def controlled_mutation(*args, **kwargs):
            result = original_apply_item(
                *args,
                **kwargs,
            )

            # Pause after an actual SQLite mutation,
            # while the transaction remains open.
            entered.set()

            if not release.wait(10):
                raise RuntimeError(
                    "Transaction test watchdog expired."
                )

            if fail:
                raise writer.ImportApplyError(
                    "Synthetic failure after mutation."
                )

            return result

        async def normal_help(update, context):
            self.assertEqual(
                update.effective_user.id,
                2,
            )

            if real_bookkeeping:
                await original_help(update, context)

            help_completed.set()

        fixture = (
            dispatch.ImportDispatchResponsivenessTests
        )

        with (
            patch.object(
                app,
                "load_settings",
                return_value=config,
            ),
            patch.object(
                app,
                "configure_logging",
            ),
            patch.object(
                ExtBot,
                "initialize",
                new=fixture.initialize_bot,
            ),
            patch.object(
                ExtBot,
                "shutdown",
                new_callable=AsyncMock,
            ),
            patch.object(
                ExtBot,
                "_post",
                new_callable=AsyncMock,
                side_effect=AssertionError(
                    "Unexpected Telegram network operation"
                ),
            ) as network,
            patch.object(
                CallbackQuery,
                "answer",
                new_callable=AsyncMock,
            ) as answer,
            patch.object(
                CallbackQuery,
                "edit_message_text",
                new_callable=AsyncMock,
            ) as edit,
            patch.object(
                Message,
                "reply_text",
                new_callable=AsyncMock,
            ) as help_reply,
            patch.object(
                app,
                "help_command",
                side_effect=normal_help,
            ),
            patch.object(
                writer,
                "_apply_item",
                side_effect=controlled_mutation,
            ) as mutate,
        ):
            application = app.build_application()

            manager = import_task_manager(
                application
            )

            # Isolate activity bookkeeping from the
            # real import transaction. Authorization
            # itself remains in the real handler.
            if not real_bookkeeping:
                application.bot_data["database"] = (
                    SimpleNamespace(
                        record_chat=AsyncMock(),
                    )
                )

            async def close_resource():
                self.assertEqual(
                    manager.active_count,
                    0,
                )

                resource_closed.set()

            application.bot_data["http_session"] = (
                SimpleNamespace(
                    closed=False,
                    close=AsyncMock(
                        side_effect=close_resource,
                    ),
                )
            )

            await application.initialize()

            stop_task = None

            try:
                await application.start()

                update = fixture.callback_update(
                    application.bot
                )

                context = CallbackContext.from_update(
                    update,
                    application,
                )

                context.user_data[
                    IMPORT_SESSION_KEY
                ] = PendingImport(
                    token="synthetic-token",
                    user_id=1,
                    user_name="Synthetic Tester",
                    chat_id=1,
                    state="ready",
                    preview=approved,
                )

                await application.update_queue.put(
                    update
                )

                self.assertTrue(
                    await asyncio.to_thread(
                        entered.wait,
                        3,
                    ),
                    "Real transaction never reached mutation.",
                )

                self.assertEqual(
                    manager.active_count,
                    1,
                )

                self.assertEqual(
                    manager.writer_count,
                    1,
                )

                self.assertEqual(
                    context.user_data[
                        IMPORT_SESSION_KEY
                    ].state,
                    "applying",
                )

                # Repeated approval must not start
                # another transaction.
                await application.update_queue.put(
                    fixture.callback_update(
                        application.bot
                    )
                )

                group_update = fixture.help_update(
                    application.bot
                )

                group_chat_id = (
                    group_update.effective_chat.id
                )

                await application.update_queue.put(
                    group_update
                )

                try:
                    await asyncio.wait_for(
                        help_completed.wait(),
                        timeout=2,
                    )

                except TimeoutError:
                    self.fail(
                        "Group command blocked by real "
                        "chat activity bookkeeping during "
                        "an active import transaction."
                    )

                self.assertTrue(
                    any(
                        "already in progress"
                        in str(call.args)
                        for call in answer.await_args_list
                    ),
                    "Duplicate approval was not rejected.",
                )

                mutate.assert_called_once()

                if real_bookkeeping:
                    help_reply.assert_awaited_once()

                    self.assertEqual(
                        help_reply.await_args.args[0],
                        HELP_TEXT,
                    )

                # Invoke the real framework shutdown
                # while the SQLite transaction is held.
                stop_task = asyncio.create_task(
                    application.stop()
                )

                async def wait_until_stopping():
                    while application.running:
                        await asyncio.sleep(0)

                await asyncio.wait_for(
                    wait_until_stopping(),
                    timeout=2,
                )

                self.assertFalse(
                    stop_task.done(),
                    "Framework stopped before the writer finished.",
                )

                self.assertEqual(
                    manager.active_count,
                    1,
                )

                self.assertFalse(
                    resource_closed.is_set()
                )

                release.set()

                await asyncio.wait_for(
                    asyncio.shield(stop_task),
                    timeout=5,
                )

                # Application.stop() does not invoke
                # post_stop or post_shutdown itself.
                # Reproduce the run_polling hook order.
                await application.post_stop(
                    application
                )

                self.assertFalse(
                    manager.accepting
                )

                self.assertEqual(
                    manager.active_count,
                    0,
                )

                await application.shutdown()

                await application.post_shutdown(
                    application
                )

                self.assertTrue(
                    resource_closed.is_set()
                )

                self.assertNotIn(
                    IMPORT_SESSION_KEY,
                    context.user_data,
                )

                mutate.assert_called_once()
                edit.assert_awaited()
                network.assert_not_called()

            finally:
                release.set()

                if stop_task is not None:
                    await stop_task

                if application.running:
                    await application.stop()

                await application.shutdown()

        backups = list(
            (root / "imports" / "backups")
            .rglob("*.db")
        )

        self.assertEqual(
            len(backups),
            1,
        )

        backup = backups[0]

        self.assertEqual(
            load_existing_user_snapshot(
                backup,
                user_id=1,
            ),
            before,
        )

        after = load_existing_user_snapshot(
            database,
            user_id=1,
        )

        if fail:
            self.assertEqual(
                after,
                before,
            )
        else:
            self.assertNotEqual(
                after,
                before,
            )

            self.assertIn(
                ("movie", 348),
                after.states,
            )

            self.assertIn(
                ("movie", 348),
                after.ratings,
            )

            replay = make_preview(
                database,
                resolution,
            )

            self.assertFalse(
                any(
                    item.has_changes
                    for item in replay.items
                )
            )

        if real_bookkeeping:
            # Accepted activity must eventually reach
            # SQLite before application shutdown returns.
            with closing(
                sqlite3.connect(database)
            ) as connection:

                chat = connection.execute(
                    "SELECT is_active FROM known_chats "
                    "WHERE chat_id = ?",
                    (group_chat_id,),
                ).fetchone()

                member = connection.execute(
                    "SELECT user_id FROM user_chats "
                    "WHERE chat_id = ? AND user_id = ?",
                    (group_chat_id, 2),
                ).fetchone()

                self.assertEqual(chat, (1,))
                self.assertEqual(member, (2,))

        for path in (database, backup):
            with closing(
                sqlite3.connect(path)
            ) as connection:

                self.assertEqual(
                    connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0],
                    "ok",
                )

                self.assertEqual(
                    connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall(),
                    [],
                )

    async def test_shutdown_waits_for_commit(self):
        await self.exercise_transaction(
            fail=False
        )

    async def test_shutdown_waits_for_rollback(self):
        await self.exercise_transaction(
            fail=True
        )


    async def test_group_activity_during_real_transaction(self):
        await self.exercise_transaction(
            fail=False,
            real_bookkeeping=True,
        )


if __name__ == "__main__":
    unittest.main()
