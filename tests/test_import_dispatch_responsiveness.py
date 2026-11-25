from __future__ import annotations

import asyncio
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update, User
from telegram.ext import CallbackContext, ExtBot

import app
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)


class ImportDispatchResponsivenessTests(
    unittest.IsolatedAsyncioTestCase
):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        self.settings = SimpleNamespace(
            bot_token="123456:synthetic-test-token",
            database_path=self.root / "synthetic.db",
            log_path=self.root / "synthetic.log",
            log_level="INFO",
            local_bot_api_url=None,
            local_bot_api_file_url=None,
            owner_user_ids={1},
            allowed_chat_ids=set(),
        )

    @staticmethod
    async def initialize_bot(bot):
        object.__setattr__(
            bot,
            "_bot_user",
            User(
                id=123456,
                first_name="Synthetic Bot",
                is_bot=True,
                username="synthetic_test_bot",
            ),
        )

    @staticmethod
    def document_update(bot):
        return Update.de_json(
            {
                "update_id": 100,
                "message": {
                    "message_id": 100,
                    "date": 1700000000,
                    "chat": {
                        "id": 1,
                        "type": "private",
                        "first_name": "Importer",
                    },
                    "from": {
                        "id": 1,
                        "is_bot": False,
                        "first_name": "Importer",
                    },
                    "document": {
                        "file_id": "synthetic-file",
                        "file_unique_id": "synthetic-unique",
                        "file_name": "ratings.csv",
                        "file_size": 100,
                    },
                },
            },
            bot,
        )

    @staticmethod
    def callback_update(bot):
        return Update.de_json(
            {
                "update_id": 101,
                "callback_query": {
                    "id": "synthetic-callback",
                    "chat_instance": "synthetic-chat",
                    "data": "imp|apply|synthetic-token",
                    "from": {
                        "id": 1,
                        "is_bot": False,
                        "first_name": "Importer",
                    },
                    "message": {
                        "message_id": 101,
                        "date": 1700000000,
                        "chat": {
                            "id": 1,
                            "type": "private",
                            "first_name": "Importer",
                        },
                        "text": "Synthetic import preview",
                    },
                },
            },
            bot,
        )

    @staticmethod
    def help_update(bot):
        return Update.de_json(
            {
                "update_id": 102,
                "message": {
                    "message_id": 102,
                    "date": 1700000000,
                    "chat": {
                        "id": -100123,
                        "type": "supergroup",
                        "title": "Synthetic Group",
                    },
                    "from": {
                        "id": 2,
                        "is_bot": False,
                        "first_name": "Other Member",
                    },
                    "text": "/help",
                    "entities": [
                        {
                            "type": "bot_command",
                            "offset": 0,
                            "length": 5,
                        },
                    ],
                },
            },
            bot,
        )

    async def exercise_dispatcher(self, operation):
        started = asyncio.Event()
        released = asyncio.Event()
        finished = asyncio.Event()
        help_completed = asyncio.Event()

        async def slow_import(update, context):
            started.set()

            try:
                await released.wait()
            finally:
                finished.set()

        async def normal_help(update, context):
            self.assertEqual(
                update.effective_user.id,
                2,
            )

            self.assertEqual(
                update.effective_chat.id,
                -100123,
            )

            help_completed.set()

        with (
            patch.object(
                app,
                "load_settings",
                return_value=self.settings,
            ),
            patch.object(
                app,
                "configure_logging",
            ),
            patch.object(
                ExtBot,
                "initialize",
                new=self.initialize_bot,
            ),
            patch.object(
                ExtBot,
                "shutdown",
                new_callable=AsyncMock,
            ),
            patch.object(
                app,
                "receive_import_document",
                side_effect=slow_import,
            ),
            patch.object(
                app,
                "callback_router",
                side_effect=slow_import,
            ),
            patch.object(
                app,
                "help_command",
                side_effect=normal_help,
            ),
        ):
            application = app.build_application()

            self.assertEqual(
                application.update_processor.max_concurrent_updates,
                1,
            )

            await application.initialize()

            try:
                await application.start()

                if operation == "document":
                    update = self.document_update(
                        application.bot
                    )

                    state = "awaiting_upload"
                    preview = None

                else:
                    update = self.callback_update(
                        application.bot
                    )

                    state = "ready"

                    preview = SimpleNamespace(
                        user_id=1,
                        review_count=0,
                        items=(
                            SimpleNamespace(
                                has_changes=True
                            ),
                        ),
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
                    user_name="Importer",
                    chat_id=1,
                    state=state,
                    preview=preview,
                )

                await application.update_queue.put(
                    update
                )

                await asyncio.wait_for(
                    started.wait(),
                    timeout=2,
                )

                await application.update_queue.put(
                    self.help_update(application.bot)
                )

                try:
                    await asyncio.wait_for(
                        help_completed.wait(),
                        timeout=1,
                    )

                except TimeoutError:
                    self.fail(
                        "An unrelated group command was "
                        "blocked by an active import."
                    )

                self.assertFalse(finished.is_set())

            finally:
                # Always release the synthetic operation
                # before stopping the real dispatcher.
                released.set()

                if application.running:
                    await application.stop()

                await application.shutdown()

        self.assertTrue(finished.is_set())

    async def test_document_does_not_block_other_group(self):
        await self.exercise_dispatcher("document")

    async def test_apply_does_not_block_other_group(self):
        await self.exercise_dispatcher("callback")


if __name__ == "__main__":
    unittest.main()
