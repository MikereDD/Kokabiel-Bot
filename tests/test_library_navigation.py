from __future__ import annotations

import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

import handlers.library as library
import handlers.commands as commands
import handlers.callbacks as callbacks

from models import PersonalEntry, PersonalLibraryPage


class LibraryNavigationTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.message = SimpleNamespace(
            message_id=101,
            chat=SimpleNamespace(
                id=100,
                type=ChatType.PRIVATE,
            ),
            api_kwargs={},
        )

        self.update = SimpleNamespace(
            effective_chat=self.message.chat,
            effective_user=SimpleNamespace(
                id=1,
                full_name="Test & Member",
                username=None,
            ),
            effective_message=self.message,
            callback_query=None,
        )

        self.context = SimpleNamespace(
            user_data={},
        )

        self.entries = tuple(
            PersonalEntry(
                chat_id=100,
                user_id=1,
                media_type="movie" if i % 2 else "tv",
                tmdb_id=i,
                title=f"Title <{i}> & Friends",
                year="2024",
                media_class=(
                    "film" if i % 2 else "tv_series"
                ),
                poster_url=None,
                watchlist=True,
                viewing_status="watching",
                updated_at="2026-11-25",
                watched_episodes=0 if i % 2 else 5,
                rating=8,
            )
            for i in range(1, 10)
        )

        async def page(*args, **kwargs):
            number = kwargs.get("page", 0)

            return PersonalLibraryPage(
                entries=self.entries[
                    number * 8:(number + 1) * 8
                ],
                total=len(self.entries),
                page=number,
                page_size=8,
            )

        self.db = SimpleNamespace(
            personal_library_page=AsyncMock(
                side_effect=page,
            ),
        )

        self.authorize = AsyncMock(return_value=True)
        self.reply = AsyncMock(return_value=self.message)
        self.edit = AsyncMock()

        for patcher in (
            patch.object(
                library, "allowed", new=self.authorize,
            ),
            patch.object(
                library, "database", return_value=self.db,
            ),
            patch.object(
                library, "reply_personal_text",
                new=self.reply,
            ),
            patch.object(
                library, "edit_personal_text",
                new=self.edit,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sessions(self):
        return self.context.user_data.get(
            library.SESSION_KEY, {}
        )

    def current(self):
        token = next(reversed(self.sessions()))
        return token, self.sessions()[token]

    async def open(self, collection="all"):
        await library.open_library(
            self.update,
            self.context,
            collection=collection,
        )
        return self.current()

    async def click(
        self, token, action, value="-",
    ):
        query = SimpleNamespace(
            data=f"lib|{token}|{action}|{value}",
            answer=AsyncMock(),
            message=self.message,
        )

        self.update.callback_query = query

        await library.library_callback(
            self.update,
            self.context,
        )

        return query

    async def test_open_library(self):
        token, menu = await self.open()

        self.assertEqual(menu.owner_id, 1)
        self.assertEqual(menu.chat_id, 100)
        self.assertEqual(menu.result.total, 9)
        self.assertEqual(len(menu.result.entries), 8)
        self.assertEqual(len(token), 24)

        self.reply.assert_awaited_once()

        text = self.reply.await_args.args[2]

        self.assertIn("9 titles", text)
        self.assertIn("Page 1 of 2", text)
        self.assertIn("Test &amp; Member", text)
        self.assertIn("Title &lt;1&gt; &amp; Friends", text)

    async def test_next_and_previous_page(self):
        first, _ = await self.open()

        await self.click(first, "page", "1")

        second, menu = self.current()

        self.assertNotEqual(first, second)
        self.assertNotIn(first, self.sessions())
        self.assertEqual(menu.result.page, 1)
        self.assertEqual(len(menu.result.entries), 1)
        self.assertEqual(menu.result.entries[0].tmdb_id, 9)

        await self.click(second, "page", "0")

        _, menu = self.current()

        self.assertEqual(menu.result.page, 0)
        self.assertEqual(len(menu.result.entries), 8)

    async def test_filters_and_sorting(self):
        token, _ = await self.open()

        await self.click(token, "category", "series")
        token, menu = self.current()

        self.assertEqual(menu.view.category, "series")

        await self.click(token, "collection", "ratings")
        token, menu = self.current()

        self.assertEqual(menu.view.collection, "ratings")

        await self.click(token, "sort")
        token, menu = self.current()

        self.assertEqual(menu.view.sort, "updated")

        await self.click(token, "status")
        _, menu = self.current()

        self.assertEqual(menu.view.status, "none")

        self.assertEqual(
            self.db.personal_library_page.await_args.kwargs,
            {
                "collection": "ratings",
                "category": "series",
                "sort": "updated",
                "status": "none",
                "page": 0,
            },
        )

    async def test_title_selection_and_back(self):
        token, _ = await self.open()

        await self.click(token, "title", "1")

        detail_token, _ = self.current()

        self.assertNotEqual(token, detail_token)

        text = self.edit.await_args.args[3]

        self.assertIn("Title &lt;2&gt;", text)
        self.assertIn("Watched episodes:</b> 5", text)
        self.assertIn("Your rating:</b> ⭐ 8/10", text)

        markup = self.edit.await_args.kwargs["reply_markup"]

        self.assertEqual(
            markup.inline_keyboard[0][0].url,
            "https://www.themoviedb.org/tv/2",
        )

        await self.click(detail_token, "refresh")

        self.assertIn(
            "Page 1 of 2",
            self.edit.await_args.args[3],
        )

    async def test_expired_buttons(self):
        token, _ = await self.open()

        await self.click(token, "page", "1")

        self.edit.reset_mock()
        self.db.personal_library_page.reset_mock()

        query = await self.click(token, "title", "0")

        self.assertTrue(
            query.answer.await_args.kwargs["show_alert"]
        )

        self.edit.assert_not_awaited()
        self.db.personal_library_page.assert_not_awaited()

    async def test_other_user_rejected(self):
        token, _ = await self.open()

        self.update.effective_user.id = 2

        self.db.personal_library_page.reset_mock()

        query = await self.click(token, "title", "0")

        self.assertTrue(
            query.answer.await_args.kwargs["show_alert"]
        )

        self.edit.assert_not_awaited()
        self.db.personal_library_page.assert_not_awaited()

    async def test_other_chat_rejected(self):
        token, _ = await self.open()

        self.update.effective_chat = SimpleNamespace(
            id=999,
            type=ChatType.GROUP,
        )

        query = await self.click(token, "refresh")

        self.assertTrue(
            query.answer.await_args.kwargs["show_alert"]
        )

        self.edit.assert_not_awaited()

    async def test_invalid_actions(self):
        token, _ = await self.open()

        for action, value in (
            ("page", "-1"),
            ("page", "999"),
            ("title", "8"),
            ("title", "invalid"),
            ("category", "invalid"),
            ("collection", "invalid"),
            ("unknown", "-"),
        ):
            with self.subTest(action=action, value=value):
                query = await self.click(
                    token, action, value,
                )

                query.answer.assert_awaited_once()

        self.edit.assert_not_awaited()
        self.assertIn(token, self.sessions())

    async def test_close_and_reopen(self):
        token, _ = await self.open()

        await self.click(token, "close")

        self.assertNotIn(token, self.sessions())

        self.assertIsNone(
            self.edit.await_args.kwargs["reply_markup"]
        )

        new_token, _ = await self.open()

        self.assertNotEqual(token, new_token)

    async def test_callback_size_and_session_limit(self):
        first, _ = await self.open()

        markup = self.reply.await_args.kwargs["reply_markup"]

        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(
                    len(button.callback_data.encode("utf-8")),
                    64,
                )

        for _ in range(library.MAX_SESSIONS):
            await self.open()

        self.assertEqual(
            len(self.sessions()),
            library.MAX_SESSIONS,
        )

        self.assertNotIn(first, self.sessions())

    async def test_command_shortcuts(self):
        for command, expected in (
            (commands.mywatchlist, "watchlist"),
            (commands.watched, "watched"),
            (commands.myprogress, "progress"),
            (library.myratings, "ratings"),
            (library.mylibrary, "all"),
        ):
            with self.subTest(collection=expected):
                await command(self.update, self.context)

                _, menu = self.current()

                self.assertEqual(
                    menu.view.collection,
                    expected,
                )


    async def test_library_media_actions(self):
        for index, action, short_type, tmdb_id in (
            (0, "watched", "m", 1),
            (1, "progress", "t", 2),
            (1, "rate", "t", 2),
            (0, "watchlist", "m", 1),
        ):
            with self.subTest(action=action):
                token, _ = await self.open()

                await self.click(
                    token, "title", str(index)
                )

                detail_token, menu = self.current()

                self.assertEqual(
                    menu.selected_index, index
                )

                with patch.object(
                    callbacks,
                    "_action",
                    new=AsyncMock(),
                ) as media_action:

                    await self.click(
                        detail_token, "media", action
                    )

                    media_action.assert_awaited_once_with(
                        self.update,
                        self.context,
                        action,
                        short_type,
                        tmdb_id,
                    )

    async def test_media_action_requires_selected_title(self):
        token, _ = await self.open()

        with patch.object(
            callbacks, "_action", new=AsyncMock()
        ) as media_action:

            query = await self.click(
                token, "media", "watchlist"
            )

            media_action.assert_not_awaited()

            self.assertTrue(
                query.answer.await_args.kwargs["show_alert"]
            )

    async def test_invalid_media_action_for_type(self):
        token, _ = await self.open()

        await self.click(token, "title", "0")

        token, _ = self.current()

        with patch.object(
            callbacks, "_action", new=AsyncMock()
        ) as media_action:

            await self.click(
                token, "media", "progress"
            )

            media_action.assert_not_awaited()

    async def test_other_message_rejected(self):
        token, _ = await self.open()

        self.message = SimpleNamespace(
            message_id=999,
            chat=self.update.effective_chat,
            api_kwargs={},
        )

        self.edit.reset_mock()

        query = await self.click(token, "refresh")

        self.assertTrue(
            query.answer.await_args.kwargs["show_alert"]
        )

        self.edit.assert_not_awaited()

    async def test_private_callback_delivery(self):
        from handlers.common import send_personal_callback_text

        self.update.callback_query = SimpleNamespace()

        self.context.bot = SimpleNamespace(
            send_message=AsyncMock(),
        )

        await send_personal_callback_text(
            self.update,
            self.context,
            "Private test",
        )

        kwargs = (
            self.context.bot.send_message.await_args.kwargs
        )

        self.assertEqual(kwargs["chat_id"], 100)
        self.assertEqual(kwargs["text"], "Private test")
        self.assertNotIn("api_kwargs", kwargs)


    async def test_release_information_navigation(self):
        from tmdb import TmdbClient

        details = TmdbClient(
            None, "synthetic-token"
        )._parse_details(
            "tv",
            {
                "id": 2,
                "name": "Example Series",
                "status": "Returning Series",
                "first_air_date": "2024-01-01",
            },
        )

        token, _ = await self.open()
        await self.click(token, "title", "1")
        token, _ = self.current()

        client = SimpleNamespace(
            details=AsyncMock(return_value=details)
        )

        with patch.object(
            library, "tmdb", return_value=client
        ):
            await self.click(token, "release")

        client.details.assert_awaited_once_with("tv", 2)

        self.assertIn(
            "Continuing",
            self.edit.await_args.args[3],
        )

        token, menu = self.current()
        self.assertEqual(menu.selected_index, 1)

        await self.click(token, "detail")

        self.assertIn(
            "Watched episodes:</b> 5",
            self.edit.await_args.args[3],
        )

    async def test_release_information_failure(self):
        from tmdb import TmdbError

        token, _ = await self.open()
        await self.click(token, "title", "0")
        token, _ = self.current()

        client = SimpleNamespace(
            details=AsyncMock(
                side_effect=TmdbError("Synthetic failure"),
            )
        )

        with patch.object(
            library, "tmdb", return_value=client
        ):
            await self.click(token, "release")

        self.assertIn(
            "temporarily unavailable",
            self.edit.await_args.args[3],
        )

        token, _ = self.current()
        await self.click(token, "refresh")

        self.assertIn(
            "Page 1 of 2",
            self.edit.await_args.args[3],
        )


    def configure_schedule(self):
        import asyncio
        from handlers.import_tasks import ImportTaskManager
        from release_refresh import RefreshResult

        self.tasks = []
        self.manager = ImportTaskManager()

        def create_task(coroutine, **kwargs):
            task = asyncio.create_task(coroutine)
            self.tasks.append(task)
            return task

        self.context.application = SimpleNamespace(
            running=True,
            bot_data={},
            create_task=create_task,
        )

        self.db.release_candidates = AsyncMock(
            return_value=(),
        )

        self.worker = SimpleNamespace(
            refresh=AsyncMock(
                return_value=RefreshResult(
                    eligible=1,
                    stale=1,
                    attempted=1,
                    updated=1,
                ),
            ),
        )

        for patcher in (
            patch.object(
                library,
                "settings",
                return_value=SimpleNamespace(region="US"),
            ),
            patch.object(
                library,
                "import_task_manager",
                return_value=self.manager,
            ),
            patch.object(
                library,
                "_refresh_worker",
                return_value=self.worker,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def open_schedule(self):
        await library.myreleases(
            self.update,
            self.context,
        )

        return self.current()

    async def test_schedule_command_and_back(self):
        self.configure_schedule()

        token, menu = await self.open_schedule()

        self.assertEqual(menu.schedule_days, 30)

        self.assertIn(
            "My Release Schedule",
            self.reply.await_args.args[2],
        )

        self.db.release_candidates.assert_awaited_once_with(
            1, region="US",
        )

        await self.click(token, "schedule", "7")

        token, menu = self.current()
        self.assertEqual(menu.schedule_days, 7)

        await self.click(token, "schedule", "90")

        token, menu = self.current()
        self.assertEqual(menu.schedule_days, 90)

        await self.click(token, "schedule_back")

        _, menu = self.current()

        self.assertIsNone(menu.schedule_days)

        self.assertIn(
            "Page 1 of 2",
            self.edit.await_args.args[3],
        )

    async def test_schedule_pagination_and_reload(self):
        from datetime import date
        import time

        from release_intelligence import (
            ReleaseCandidate,
            ReleaseSnapshot,
            ScheduledRelease,
        )

        self.configure_schedule()

        self.db.release_candidates.return_value = tuple(
            ReleaseCandidate(
                media_type="movie",
                tmdb_id=i,
                title=f"Film {i}",
                media_class="film",
                checked_at=int(time.time()),
                snapshot=ReleaseSnapshot(
                    "Released",
                    (
                        ScheduledRelease(
                            date.today().isoformat(),
                            "US Digital",
                        ),
                    ),
                ),
            )
            for i in range(1, 11)
        )

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_page", "1")

        token, menu = self.current()

        self.assertEqual(menu.schedule_page, 1)
        self.assertIn(
            "Page 2 of 2",
            self.edit.await_args.args[3],
        )

        await self.click(token, "schedule_reload")

        _, menu = self.current()
        self.assertEqual(menu.schedule_page, 1)

        self.worker.refresh.assert_not_awaited()

    async def test_refresh_background_execution(self):
        import asyncio

        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
            RefreshResult,
        )

        self.configure_schedule()

        entered = asyncio.Event()
        finish = asyncio.Event()

        async def refresh(*args, **kwargs):
            entered.set()
            await finish.wait()

            return RefreshResult(
                eligible=1,
                stale=1,
                attempted=1,
                updated=1,
            )

        self.worker.refresh.side_effect = refresh

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_update")

        self.assertNotIn(token, self.sessions())

        self.assertIn(
            "Refreshing release information",
            self.edit.await_args.args[3],
        )

        self.assertEqual(len(self.tasks), 1)

        app = self.context.application

        try:
            await asyncio.wait_for(
                entered.wait(), timeout=2,
            )

            self.assertIn(
                RELEASE_REFRESH_ACTIVE_KEY,
                app.bot_data,
            )

            # A competing import writer must be rejected.
            error = self.manager.admission_error(
                app, 2, writer=True,
            )

            self.assertIn("Release information", error)

            # Ordinary library navigation is still available.
            await self.open()

            self.assertFalse(self.tasks[0].done())

        finally:
            finish.set()
            await self.tasks[0]

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            app.bot_data,
        )

        self.assertIn(
            "Updated: 1",
            self.edit.await_args.args[3],
        )

        self.assertIsNone(
            self.manager.admission_error(
                app, 2, writer=True,
            )
        )

    async def test_refresh_rejected_during_import_writer(self):
        self.configure_schedule()

        token, _ = await self.open_schedule()

        with patch.object(
            library,
            "import_task_manager",
            return_value=SimpleNamespace(
                accepting=True,
                writer_count=1,
            ),
        ):
            query = await self.click(
                token, "schedule_update",
            )

        self.assertIn(
            "import is being applied",
            query.answer.await_args.args[0],
        )

        self.assertEqual(self.tasks, [])
        self.worker.refresh.assert_not_awaited()

    async def test_duplicate_refresh_rejected(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
        )

        self.configure_schedule()

        token, _ = await self.open_schedule()

        reservation = object()

        self.context.application.bot_data[
            RELEASE_REFRESH_ACTIVE_KEY
        ] = reservation

        query = await self.click(
            token, "schedule_update",
        )

        self.assertIn(
            "already running",
            query.answer.await_args.args[0],
        )

        self.assertEqual(self.tasks, [])

        self.assertIs(
            self.context.application.bot_data[
                RELEASE_REFRESH_ACTIVE_KEY
            ],
            reservation,
        )

    async def test_refresh_ack_failure_releases_reservation(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
        )

        self.configure_schedule()

        token, _ = await self.open_schedule()

        self.edit.side_effect = RuntimeError(
            "Synthetic acknowledgement failure"
        )

        with self.assertRaises(RuntimeError):
            await self.click(token, "schedule_update")

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

        self.assertIn(token, self.sessions())
        self.assertEqual(self.tasks, [])
        self.worker.refresh.assert_not_awaited()

    async def test_refresh_final_edit_failure(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
        )

        self.configure_schedule()

        token, _ = await self.open_schedule()

        self.edit.side_effect = [
            None,
            RuntimeError("Synthetic final edit failure"),
        ]

        await self.click(token, "schedule_update")

        with self.assertRaises(RuntimeError):
            await self.tasks[0]

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

        self.worker.refresh.assert_awaited_once_with(
            1, region="US",
        )

        self.assertNotIn(token, self.sessions())

    async def test_refresh_shutdown_during_acknowledgement(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
        )

        self.configure_schedule()

        token, _ = await self.open_schedule()

        async def edit(*args, **kwargs):
            self.context.application.running = False

        self.edit.side_effect = edit

        await self.click(token, "schedule_update")

        self.assertEqual(self.tasks, [])

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

        self.worker.refresh.assert_not_awaited()

    async def test_refresh_cancelled_before_start(self):
        import asyncio

        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
        )

        self.configure_schedule()

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_update")

        self.tasks[0].cancel()

        with self.assertRaises(asyncio.CancelledError):
            await self.tasks[0]

        await asyncio.sleep(0)

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

        self.worker.refresh.assert_not_awaited()


    async def test_progress_released_episode_totals(self):
        token, menu = await self.open("progress")

        with patch.object(
            commands,
            "_progress_released_counts",
            new=AsyncMock(
                return_value={("tv", 2): (4, 10)},
            ),
        ) as resolver:
            await self.click(token, "episode_totals")

        entries = resolver.await_args.args[1]

        self.assertEqual(len(entries), 4)
        self.assertTrue(all(
            entry.media_type == "tv"
            for entry in entries
        ))

        text = self.edit.await_args.args[3]

        self.assertIn("4/10 released episodes watched", text)
        self.assertIn("Released total unavailable", text)
        self.assertNotIn(token, self.sessions())

    async def test_episode_totals_rejected_outside_progress(self):
        token, _ = await self.open("all")

        with patch.object(
            commands,
            "_progress_released_counts",
            new=AsyncMock(),
        ) as resolver:
            await self.click(token, "episode_totals")

        resolver.assert_not_awaited()
        self.edit.assert_not_awaited()

    async def test_real_library_watchlist_and_watched_handlers(self):
        from tmdb import TmdbClient

        details = TmdbClient(
            None, "synthetic"
        )._parse_details(
            "movie",
            {"id": 1, "title": "Example Film"},
        )

        client = SimpleNamespace(
            details=AsyncMock(return_value=details),
        )

        db = SimpleNamespace(
            toggle_watchlist=AsyncMock(return_value=True),
            toggle_movie_watched=AsyncMock(return_value=True),
        )

        with (
            patch.object(callbacks, "tmdb", return_value=client),
            patch.object(callbacks, "database", return_value=db),
        ):
            token, _ = await self.open()
            await self.click(token, "title", "0")
            token, _ = self.current()

            await self.click(token, "media", "watchlist")

            token, _ = self.current()
            await self.click(token, "title", "0")
            token, _ = self.current()

            await self.click(token, "media", "watched")

        db.toggle_watchlist.assert_awaited_once_with(
            100, details, 1, "Test & Member",
        )

        db.toggle_movie_watched.assert_awaited_once_with(
            100, details, 1, "Test & Member",
        )

    async def test_real_library_rating_and_progress_handlers(self):
        from tmdb import TmdbClient

        details = TmdbClient(
            None, "synthetic"
        )._parse_details(
            "tv",
            {"id": 2, "name": "Example Series"},
        )

        client = SimpleNamespace(
            details=AsyncMock(return_value=details),
        )

        state = SimpleNamespace(
            viewing_status="watching",
            watched_episodes=1,
        )

        with (
            patch.object(callbacks, "tmdb", return_value=client),
            patch.object(
                callbacks,
                "_series_state",
                new=AsyncMock(
                    return_value=(state, {(1, 1), (1, 2)}),
                ),
            ),
            patch.object(
                callbacks,
                "send_personal_callback_text",
                new=AsyncMock(),
            ) as send,
        ):
            token, _ = await self.open()
            await self.click(token, "title", "1")
            token, _ = self.current()

            await self.click(token, "media", "rate")
            await self.click(token, "media", "progress")

        self.assertEqual(send.await_count, 2)

        self.assertIn(
            "Choose a score",
            send.await_args_list[0].args[2],
        )

        for call in send.await_args_list:
            self.assertIsNotNone(
                call.kwargs["reply_markup"]
            )


    async def prepare_real_toggle_database(self):
        import sqlite3
        import tempfile

        from contextlib import closing
        from pathlib import Path

        from database import Database
        from tmdb import TmdbClient

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        path = Path(temporary.name) / "test.db"
        db = Database(path)

        await db.initialize()

        with closing(sqlite3.connect(path)) as conn:
            for i in (1, 2):
                conn.execute("""
                    INSERT INTO media (
                        media_type, tmdb_id, title,
                        year, media_class
                    )
                    VALUES ('movie', ?, ?, '2027', 'film')
                """, (i, f"Film {i}"))

                conn.execute("""
                    INSERT INTO user_media_state (
                        user_id, media_type, tmdb_id,
                        user_name, watchlist, viewing_status
                    )
                    VALUES (1, 'movie', ?, 'Tester', 1, 'none')
                """, (i,))

            conn.commit()

        parser = TmdbClient(None, "synthetic", region="US")

        async def details(media_type, tmdb_id, **kwargs):
            return parser._parse_details(
                media_type,
                {
                    "id": tmdb_id,
                    "title": f"Film {tmdb_id}",
                    "release_date": "2027-01-01",
                },
            )

        for patcher in (
            patch.object(library, "database", return_value=db),
            patch.object(callbacks, "database", return_value=db),
            patch.object(
                callbacks,
                "tmdb",
                return_value=SimpleNamespace(
                    details=AsyncMock(side_effect=details),
                ),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        return db, path

    async def test_watched_toggle_renders_current_database_state(self):
        db, path = await self.prepare_real_toggle_database()

        token, _ = await self.open("all")
        await self.click(token, "title", "0")
        detail_token, _ = self.current()

        await self.click(detail_token, "media", "watched")

        fresh_token, menu = self.current()

        self.assertNotEqual(fresh_token, detail_token)
        self.assertNotIn(detail_token, self.sessions())
        self.assertIsNone(menu.selected_index)

        entry = next(
            entry for entry in menu.result.entries
            if entry.tmdb_id == 1
        )

        self.assertEqual(entry.viewing_status, "watched")
        self.assertFalse(entry.watchlist)
        self.assertIn("Watched", self.edit.await_args.args[3])

        # A duplicate delivery or impatient second press
        # cannot reverse the completed action.
        await self.click(detail_token, "media", "watched")

        result = await db.personal_library_page(
            100, 1, collection="all",
        )

        entry = next(
            entry for entry in result.entries
            if entry.tmdb_id == 1
        )

        self.assertEqual(entry.viewing_status, "watched")

    async def test_toggle_preserves_filtered_collection_identity(self):
        db, path = await self.prepare_real_toggle_database()

        token, _ = await self.open("watchlist")
        await self.click(token, "title", "0")
        detail_token, _ = self.current()

        await self.click(detail_token, "media", "watched")

        fresh_token, menu = self.current()

        self.assertEqual(menu.view.collection, "watchlist")
        self.assertIsNone(menu.selected_index)
        self.assertEqual(menu.result.total, 1)

        self.assertEqual(
            [entry.tmdb_id for entry in menu.result.entries],
            [2],
        )

        self.assertNotIn(detail_token, self.sessions())
        self.assertNotEqual(fresh_token, detail_token)

        # The remaining title must not be selected
        # implicitly by the previous detail index.
        await self.click(fresh_token, "media", "watched")

        result = await db.personal_library_page(
            100, 1, collection="watchlist",
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(result.entries[0].tmdb_id, 2)
        self.assertEqual(
            result.entries[0].viewing_status, "none"
        )

    async def test_watchlist_toggle_refreshes_collection(self):
        db, path = await self.prepare_real_toggle_database()

        token, _ = await self.open("watchlist")
        await self.click(token, "title", "0")
        detail_token, _ = self.current()

        await self.click(detail_token, "media", "watchlist")

        _, menu = self.current()

        self.assertEqual(menu.result.total, 1)
        self.assertEqual(menu.result.entries[0].tmdb_id, 2)
        self.assertNotIn(detail_token, self.sessions())

        await self.click(detail_token, "media", "watchlist")

        result = await db.personal_library_page(
            100, 1, collection="watchlist",
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(result.entries[0].tmdb_id, 2)

    async def test_toggle_edit_failure_does_not_allow_replay(self):
        db, path = await self.prepare_real_toggle_database()

        token, _ = await self.open("all")
        await self.click(token, "title", "0")
        detail_token, _ = self.current()

        self.edit.side_effect = RuntimeError(
            "Synthetic Telegram edit failure"
        )

        with self.assertRaises(RuntimeError):
            await self.click(
                detail_token, "media", "watched",
            )

        self.assertNotIn(detail_token, self.sessions())

        await self.click(detail_token, "media", "watched")

        result = await db.personal_library_page(
            100, 1, collection="all",
        )

        entry = next(
            entry for entry in result.entries
            if entry.tmdb_id == 1
        )

        self.assertEqual(entry.viewing_status, "watched")
        self.assertFalse(entry.watchlist)


    async def test_refresh_all_schedule_controls(self):
        self.configure_schedule()
        token, _ = await self.open_schedule()

        markup = self.reply.await_args.kwargs["reply_markup"]

        buttons = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }

        for action in (
            "schedule_update",
            "schedule_update_all",
        ):
            self.assertIn(
                f"lib|{token}|{action}|-",
                buttons,
            )

    async def test_refresh_all_background_progress(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
            RefreshResult,
        )

        self.configure_schedule()

        async def refresh_all(
            user_id,
            *,
            region,
            should_continue,
            on_progress,
        ):
            self.assertEqual(user_id, 1)
            self.assertEqual(region, "US")
            self.assertTrue(should_continue())

            self.assertIsNotNone(
                self.manager.admission_error(
                    self.context.application,
                    2,
                    writer=True,
                )
            )

            await on_progress(
                1,
                RefreshResult(
                    eligible=364,
                    attempted=20,
                    updated=20,
                ),
            )

            text = self.edit.await_args.args[3]

            self.assertIn("Updated: 20", text)
            self.assertIn("\nBatch: 1\n", text)
            self.assertNotIn("\\n", text)

            return RefreshResult(
                eligible=364,
                stale=0,
                attempted=364,
                updated=364,
            )

        self.worker.refresh_all = AsyncMock(
            side_effect=refresh_all,
        )

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_update_all")

        self.assertNotIn(token, self.sessions())
        self.assertEqual(len(self.tasks), 1)

        await self.tasks[0]

        self.worker.refresh.assert_not_awaited()
        self.worker.refresh_all.assert_awaited_once()

        text = self.edit.await_args.args[3]

        self.assertIn("Updated: 364", text)
        self.assertIn(
            "Remaining stale or unchecked: 0",
            text,
        )

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

        _, menu = self.current()
        self.assertEqual(menu.schedule_days, 30)

    async def test_refresh_all_progress_failure_is_nonfatal(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
            RefreshResult,
        )

        self.configure_schedule()

        async def refresh_all(
            *args,
            should_continue,
            on_progress,
            **kwargs,
        ):
            self.edit.side_effect = RuntimeError(
                "Synthetic progress edit failure"
            )

            await on_progress(
                1,
                RefreshResult(
                    eligible=45,
                    attempted=20,
                    updated=20,
                ),
            )

            self.edit.side_effect = None

            return RefreshResult(
                eligible=45,
                attempted=45,
                updated=45,
            )

        self.worker.refresh_all = AsyncMock(
            side_effect=refresh_all,
        )

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_update_all")
        await self.tasks[0]

        self.assertIn(
            "Updated: 45",
            self.edit.await_args.args[3],
        )

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

    async def test_refresh_all_shutdown_guard(self):
        from release_refresh import (
            RELEASE_REFRESH_ACTIVE_KEY,
            RefreshResult,
        )

        self.configure_schedule()

        async def refresh_all(
            *args,
            should_continue,
            on_progress,
            **kwargs,
        ):
            self.assertTrue(should_continue())

            self.context.application.running = False

            self.assertFalse(should_continue())

            return RefreshResult(
                eligible=45,
                stale=25,
                attempted=20,
                updated=20,
                stop_reason="shutdown",
            )

        self.worker.refresh_all = AsyncMock(
            side_effect=refresh_all,
        )

        token, _ = await self.open_schedule()

        await self.click(token, "schedule_update_all")
        await self.tasks[0]

        self.assertIn(
            "Refresh stopped for shutdown.",
            self.edit.await_args.args[3],
        )

        self.assertNotIn(
            RELEASE_REFRESH_ACTIVE_KEY,
            self.context.application.bot_data,
        )

    async def test_refresh_all_rejects_invalid_value(self):
        self.configure_schedule()
        self.worker.refresh_all = AsyncMock()

        token, _ = await self.open_schedule()

        await self.click(
            token,
            "schedule_update_all",
            "invalid",
        )

        self.assertEqual(len(self.tasks), 0)
        self.worker.refresh_all.assert_not_awaited()

    async def test_callback_router(self):
        query = SimpleNamespace(
            data="lib|synthetic|refresh|-",
        )

        self.update.callback_query = query

        with patch.object(
            callbacks,
            "library_callback",
            new=AsyncMock(),
        ) as handler:

            await callbacks.callback_router(
                self.update,
                self.context,
            )

            handler.assert_awaited_once_with(
                self.update,
                self.context,
            )


if __name__ == "__main__":
    unittest.main()
