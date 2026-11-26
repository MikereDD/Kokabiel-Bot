import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import date
from pathlib import Path

from database import Database
from release_intelligence import (
    CACHE_MAX_AGE,
    build_release_snapshot,
    upcoming_events,
)
from tmdb import TmdbClient


class ReleaseCacheTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.path = Path(temp.name) / "test.db"
        self.db = Database(self.path)
        await self.db.initialize()

        self.now = 1800000000
        self.client = TmdbClient(None, "synthetic", region="US")

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")

            conn.executemany("""
                INSERT INTO media (
                    media_type, tmdb_id, title, year, media_class
                ) VALUES (?, ?, ?, ?, ?)
            """, [
                ("movie", 1, "Film", "2027", "film"),
                ("movie", 2, "Watched", "2020", "film"),
                ("movie", 3, "Documentary", "2027", "documentary_film"),
                ("movie", 4, "Other", "2027", "film"),
                ("tv", 1, "Series", "2024", "tv_series"),
                ("tv", 2, "Caught Up", "2025", "tv_series"),
                ("tv", 3, "Completed", "2023", "tv_series"),
            ])

            conn.executemany("""
                INSERT INTO user_media_state (
                    user_id, media_type, tmdb_id,
                    user_name, watchlist, viewing_status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (1, "movie", 1, "Tester", 1, "none"),
                (1, "movie", 2, "Tester", 0, "watched"),
                (1, "movie", 3, "Tester", 1, "none"),
                (2, "movie", 4, "Other", 1, "none"),
                (1, "tv", 1, "Tester", 0, "watching"),
                (1, "tv", 2, "Tester", 0, "caught_up"),
                (1, "tv", 3, "Tester", 0, "completed"),
            ])

            conn.execute("""
                INSERT INTO ratings (
                    user_id, media_type, tmdb_id,
                    user_name, rating
                ) VALUES (1, 'tv', 1, 'Tester', 9)
            """)

            conn.execute("""
                INSERT INTO episode_progress (
                    user_id, tmdb_id, season_number, episode_number
                ) VALUES (1, 1, 1, 1)
            """)

            conn.commit()

    def film(self, tmdb_id=1, released="2027-02-10"):
        return self.client._parse_details("movie", {
            "id": tmdb_id,
            "title": "Film",
            "release_date": released,
        })

    def series(self, status="Returning Series"):
        return self.client._parse_details("tv", {
            "id": 1,
            "name": "Series",
            "status": status,
            "first_air_date": "2024-01-01",
            "next_episode_to_air": {
                "season_number": 3,
                "episode_number": 2,
                "air_date": "2027-02-12",
            },
        })

    async def store(self, details, checked_at=None, region="US"):
        return await self.db.store_release_snapshot(
            details,
            region=region,
            checked_at=self.now if checked_at is None else checked_at,
        )

    async def candidates(self, user=1, region="US"):
        return await self.db.release_candidates(user, region=region)

    async def candidate(self, kind="movie", region="US"):
        return next(
            item for item in await self.candidates(region=region)
            if item.media_type == kind and item.tmdb_id == 1
        )

    def personal_records(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return {
                table: conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in (
                    "media", "user_media_state",
                    "episode_progress", "ratings",
                )
            }

    async def test_candidate_scope(self):
        self.assertEqual(
            {(x.media_type, x.tmdb_id) for x in await self.candidates()},
            {("movie", 1), ("movie", 3), ("tv", 1), ("tv", 2)},
        )

        self.assertEqual(
            [(x.media_type, x.tmdb_id) for x in await self.candidates(2)],
            [("movie", 4)],
        )

    async def test_persistence_and_isolation(self):
        self.assertTrue(await self.store(self.film()))
        self.assertTrue(await self.store(self.series()))

        self.db = Database(self.path)
        await self.db.initialize()

        film = await self.candidate()
        series = await self.candidate("tv")

        self.assertEqual(film.checked_at, self.now)
        self.assertEqual(film.snapshot.events[0].date, "2027-02-10")
        self.assertEqual(series.snapshot.lifecycle, "Continuing")
        self.assertNotEqual(film.snapshot, series.snapshot)
        self.assertIsNone((await self.candidate(region="GB")).snapshot)

    async def test_freshness(self):
        self.assertTrue((await self.candidate()).is_stale(self.now))

        await self.store(self.film())
        item = await self.candidate()

        self.assertFalse(item.is_stale(self.now))
        self.assertFalse(item.is_stale(self.now + CACHE_MAX_AGE - 1))
        self.assertTrue(item.is_stale(self.now + CACHE_MAX_AGE))
        self.assertTrue(item.is_stale(self.now - 1))

    async def test_older_refresh_rejected(self):
        await self.store(self.film(released="2027-03-01"))

        self.assertFalse(await self.store(
            self.film(released="2027-01-01"),
            checked_at=self.now - 100,
        ))

        item = await self.candidate()
        self.assertEqual(item.snapshot.events[0].date, "2027-03-01")


    async def test_future_timestamp_recovery(self):
        before = self.personal_records()
        future = self.now + 365 * 86400

        await self.store(
            self.film(released="2027-02-10"),
            checked_at=future,
        )

        observed = await self.candidate()

        self.assertTrue(observed.is_stale(self.now))

        self.assertTrue(
            await self.db.store_release_snapshot(
                self.film(released="2027-06-30"),
                region="US",
                checked_at=self.now,
                recover_future_checked_at=observed.checked_at,
            )
        )

        recovered = await self.candidate()

        self.assertEqual(recovered.checked_at, self.now)
        self.assertFalse(recovered.is_stale(self.now))
        self.assertEqual(
            recovered.snapshot.events[0].date,
            "2027-06-30",
        )

        self.assertEqual(self.personal_records(), before)

    async def test_future_recovery_rejects_changed_timestamp(self):
        future = self.now + 365 * 86400

        await self.store(self.film(), checked_at=future)

        observed = await self.candidate()

        await self.store(
            self.film(released="2027-04-01"),
            checked_at=future + 1,
        )

        self.assertFalse(
            await self.db.store_release_snapshot(
                self.film(released="2027-06-30"),
                region="US",
                checked_at=self.now,
                recover_future_checked_at=observed.checked_at,
            )
        )

        current = await self.candidate()

        self.assertEqual(current.checked_at, future + 1)
        self.assertEqual(
            current.snapshot.events[0].date,
            "2027-04-01",
        )

    async def test_future_recovery_cannot_replay_after_success(self):
        future = self.now + 365 * 86400

        await self.store(self.film(), checked_at=future)

        self.assertTrue(
            await self.db.store_release_snapshot(
                self.film(released="2027-04-01"),
                region="US",
                checked_at=self.now,
                recover_future_checked_at=future,
            )
        )

        self.assertFalse(
            await self.db.store_release_snapshot(
                self.film(released="2027-06-30"),
                region="US",
                checked_at=self.now + 1,
                recover_future_checked_at=future,
            )
        )

        current = await self.candidate()

        self.assertEqual(current.checked_at, self.now)
        self.assertEqual(
            current.snapshot.events[0].date,
            "2027-04-01",
        )

    async def test_worker_recovers_after_real_database_clock_rollback(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from release_refresh import (
            ReleaseRefreshWorker,
            RETRY_DELAY,
        )

        before = self.personal_records()
        clock = SimpleNamespace(
            wall=self.now + 365 * 86400,
            mono=1000,
        )

        client = SimpleNamespace(
            details=AsyncMock(
                return_value=self.film(
                    4, released="2027-02-10",
                ),
            ),
        )

        worker = ReleaseRefreshWorker(
            self.db,
            client,
            clock=lambda: clock.wall,
            monotonic=lambda: clock.mono,
        )

        first = await worker.refresh(2, region="US")

        self.assertEqual(first.updated, 1)

        # Correct the synthetic clock; change the TMDB date.
        clock.wall = self.now

        client.details.return_value = self.film(
            4, released="2027-06-30",
        )

        results = []

        for _ in range(6):
            clock.mono += RETRY_DELAY + 1
            results.append(
                await worker.refresh(2, region="US")
            )

        self.assertEqual(
            sum(result.updated for result in results),
            1,
        )

        self.assertEqual(client.details.await_count, 2)

        (candidate,) = await self.candidates(2)

        self.assertEqual(candidate.checked_at, self.now)
        self.assertFalse(candidate.is_stale(self.now))
        self.assertEqual(
            candidate.snapshot.events[0].date,
            "2027-06-30",
        )

        self.assertEqual(self.personal_records(), before)


    def seed_refresh_films(self, count):
        ids = list(range(100, 100 + count))

        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany("""
                INSERT INTO media (
                    media_type, tmdb_id, title,
                    year, media_class
                )
                VALUES ('movie', ?, ?, '2027', 'film')
            """, [(i, f"Film {i}") for i in ids])

            conn.executemany("""
                INSERT INTO user_media_state (
                    user_id, media_type, tmdb_id,
                    user_name, watchlist, viewing_status
                )
                VALUES (3, 'movie', ?, 'Tester', 1, 'none')
            """, [(i,) for i in ids])

            conn.commit()

        return ids

    async def test_permanent_failures_do_not_starve_healthy_titles(self):
        from types import SimpleNamespace

        from release_refresh import (
            ReleaseRefreshWorker,
            RETRY_DELAY,
        )
        from tmdb import TmdbError

        ids = self.seed_refresh_films(364)
        failing = set(ids[:20])
        calls = []

        clock = SimpleNamespace(wall=self.now, mono=1000)

        async def details(media_type, tmdb_id, **kwargs):
            calls.append(tmdb_id)

            if tmdb_id in failing:
                raise TmdbError(
                    "Synthetic missing title",
                    status_code=404,
                )

            return self.film(tmdb_id)

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: clock.wall,
            monotonic=lambda: clock.mono,
        )

        before = self.personal_records()

        first = await worker.refresh(3, region="US")

        self.assertEqual(first.failed, 20)
        self.assertEqual(first.updated, 0)

        updated = 0

        for _ in range(18):
            clock.wall += RETRY_DELAY + 60
            clock.mono += RETRY_DELAY + 60

            result = await worker.refresh(3, region="US")
            updated += result.updated

        self.assertEqual(updated, 344)

        candidates = await self.candidates(3)

        checked = {
            item.tmdb_id
            for item in candidates
            if item.snapshot is not None
        }

        self.assertEqual(checked, set(ids) - failing)

        # Previously failed titles remain retryable.
        failing.clear()

        clock.wall += RETRY_DELAY + 60
        clock.mono += RETRY_DELAY + 60

        result = await worker.refresh(3, region="US")

        self.assertEqual(result.updated, 20)

        candidates = await self.candidates(3)

        self.assertEqual(len(candidates), 364)

        self.assertTrue(all(
            item.snapshot is not None
            for item in candidates
        ))

        self.assertEqual(self.personal_records(), before)

    async def test_failed_titles_rotate_without_starvation(self):
        from types import SimpleNamespace

        from release_refresh import (
            ReleaseRefreshWorker,
            RETRY_DELAY,
        )
        from tmdb import TmdbError

        ids = self.seed_refresh_films(45)
        calls = []

        clock = SimpleNamespace(wall=self.now, mono=1000)

        async def details(media_type, tmdb_id, **kwargs):
            calls.append(tmdb_id)

            raise TmdbError(
                "Synthetic persistent failure",
                status_code=404,
            )

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: clock.wall,
            monotonic=lambda: clock.mono,
        )

        for _ in range(3):
            result = await worker.refresh(3, region="US")

            self.assertEqual(result.attempted, 20)
            self.assertEqual(result.failed, 20)

            clock.wall += RETRY_DELAY + 60
            clock.mono += RETRY_DELAY + 60

        # Every title receives an attempt before any
        # previously failed title takes another slot.
        self.assertEqual(calls[:45], ids)
        self.assertEqual(calls[45:], ids[:15])


    async def test_refresh_all_populates_complete_collection(self):
        from types import SimpleNamespace

        from release_refresh import ReleaseRefreshWorker

        ids = self.seed_refresh_films(364)
        calls = []
        progress = []

        async def details(media_type, tmdb_id, **kwargs):
            calls.append(tmdb_id)
            return self.film(tmdb_id)

        async def on_progress(batch, result):
            progress.append((batch, result.updated))

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: self.now,
        )

        before = self.personal_records()

        result = await worker.refresh_all(
            3,
            region="US",
            on_progress=on_progress,
        )

        self.assertEqual(result.eligible, 364)
        self.assertEqual(result.attempted, 364)
        self.assertEqual(result.updated, 364)
        self.assertEqual(result.stale, 0)
        self.assertEqual(result.failed, 0)
        self.assertIsNone(result.stop_reason)
        self.assertEqual(calls, ids)
        self.assertEqual(progress[-1][1], 364)

        self.assertEqual(self.personal_records(), before)

    async def test_refresh_all_does_not_repeat_failed_titles(self):
        from types import SimpleNamespace

        from release_refresh import ReleaseRefreshWorker
        from tmdb import TmdbError

        ids = self.seed_refresh_films(364)
        failing = set(ids[:20])
        calls = []

        async def details(media_type, tmdb_id, **kwargs):
            calls.append(tmdb_id)

            if tmdb_id in failing:
                raise TmdbError(
                    "Synthetic missing title",
                    status_code=404,
                )

            return self.film(tmdb_id)

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: self.now,
        )

        result = await worker.refresh_all(3, region="US")

        self.assertEqual(result.attempted, 364)
        self.assertEqual(result.updated, 344)
        self.assertEqual(result.failed, 20)
        self.assertEqual(result.stale, 20)
        self.assertEqual(result.stop_reason, "remaining")

        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(set(calls), set(ids))

    async def test_refresh_all_stops_on_rate_limit(self):
        from types import SimpleNamespace

        from release_refresh import ReleaseRefreshWorker
        from tmdb import TmdbError

        ids = self.seed_refresh_films(45)
        calls = []

        async def details(media_type, tmdb_id, **kwargs):
            calls.append(tmdb_id)

            if len(calls) == 22:
                raise TmdbError(
                    "Synthetic rate limit",
                    status_code=429,
                )

            return self.film(tmdb_id)

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: self.now,
        )

        result = await worker.refresh_all(3, region="US")

        self.assertEqual(result.attempted, 22)
        self.assertEqual(result.updated, 21)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.stale, 24)
        self.assertEqual(result.stop_reason, "rate_limited")
        self.assertEqual(calls, ids[:22])

    async def test_refresh_all_stops_between_batches_on_shutdown(self):
        from types import SimpleNamespace

        from release_refresh import ReleaseRefreshWorker

        self.seed_refresh_films(45)

        running = True

        async def details(media_type, tmdb_id, **kwargs):
            return self.film(tmdb_id)

        async def on_progress(batch, result):
            nonlocal running
            running = False

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: self.now,
        )

        result = await worker.refresh_all(
            3,
            region="US",
            should_continue=lambda: running,
            on_progress=on_progress,
        )

        self.assertEqual(result.updated, 20)
        self.assertEqual(result.stale, 25)
        self.assertEqual(result.stop_reason, "shutdown")

        # The worker lock must be available afterward.
        result = await worker.refresh(3, region="US")

        self.assertEqual(result.updated, 20)

    async def test_refresh_all_respects_overall_time_budget(self):
        from types import SimpleNamespace

        from release_refresh import (
            ReleaseRefreshWorker,
            REFRESH_ALL_START_BUDGET,
        )

        self.seed_refresh_films(45)

        clock = SimpleNamespace(wall=self.now, mono=1000)

        async def details(media_type, tmdb_id, **kwargs):
            return self.film(tmdb_id)

        async def on_progress(batch, result):
            clock.mono += REFRESH_ALL_START_BUDGET

        worker = ReleaseRefreshWorker(
            self.db,
            SimpleNamespace(details=details),
            clock=lambda: clock.wall,
            monotonic=lambda: clock.mono,
        )

        result = await worker.refresh_all(
            3,
            region="US",
            on_progress=on_progress,
        )

        self.assertEqual(result.updated, 20)
        self.assertEqual(result.stale, 25)
        self.assertEqual(
            result.stop_reason,
            "overall_time_budget",
        )

    async def test_missing_media_and_region(self):
        self.assertFalse(await self.store(self.film(99999)))

        with self.assertRaises(ValueError):
            await self.store(self.film(), region="GB")

        self.assertIsNone((await self.candidate()).snapshot)

    async def test_schedule_range(self):
        snapshot = build_release_snapshot(self.series(), region="US")

        events = upcoming_events(
            snapshot,
            today=date(2027, 2, 1),
            through=date(2027, 2, 28),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].date, "2027-02-12")
        self.assertEqual((events[0].season, events[0].episode), (3, 2))

        self.assertEqual(upcoming_events(
            snapshot,
            today=date(2027, 3, 1),
            through=date(2027, 3, 31),
        ), ())

        with self.assertRaises(ValueError):
            upcoming_events(
                snapshot,
                today=date(2027, 3, 31),
                through=date(2027, 3, 1),
            )

    async def test_ended_and_canceled(self):
        for status in ("Ended", "Canceled"):
            with self.subTest(status=status):
                snapshot = build_release_snapshot(
                    self.series(status), region="US",
                )
                self.assertEqual(snapshot.lifecycle, status)
                self.assertEqual(snapshot.events, ())

    async def test_schema_upgrade_preserves_records(self):
        before = self.personal_records()

        # Only the disposable test database.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DROP TABLE release_cache")
            conn.commit()

        await self.db.initialize()
        await self.db.initialize()

        await self.store(self.film())
        await self.store(self.series())

        self.assertEqual(self.personal_records(), before)

        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone(),
                ("ok",),
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )


    async def test_progress_group_order_before_pagination(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""
                UPDATE user_media_state
                SET viewing_status = 'watched'
                WHERE user_id = 1
                  AND media_type = 'movie'
            """)

            conn.commit()

        entries = []

        for page in range(3):
            result = await self.db.personal_library_page(
                100,
                1,
                collection="progress",
                category="all",
                sort="title",
                page=page,
                page_size=2,
            )

            self.assertEqual(result.total, 6)
            entries.extend(result.entries)

        self.assertEqual(
            len({
                (entry.media_type, entry.tmdb_id)
                for entry in entries
            }),
            6,
        )

        self.assertEqual(
            [entry.media_class for entry in entries],
            [
                "film",
                "film",
                "tv_series",
                "tv_series",
                "tv_series",
                "documentary_film",
            ],
        )


if __name__ == "__main__":
    unittest.main()
