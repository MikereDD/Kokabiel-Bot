import asyncio
import time
import unittest

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from release_intelligence import (
    CACHE_MAX_AGE,
    ReleaseCandidate,
    ReleaseSnapshot,
)
from release_refresh import (
    ReleaseRefreshWorker,
    RETRY_DELAY,
)
from tmdb import TmdbClient, TmdbError


class ReleaseRefreshTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.now = 1800000000
        self.tick = 1000

        self.parser = TmdbClient(
            None, "synthetic-token", region="US",
        )

        self.db = SimpleNamespace(
            release_candidates=AsyncMock(return_value=()),
            store_release_snapshot=AsyncMock(return_value=True),
        )

        self.client = SimpleNamespace(
            details=AsyncMock(side_effect=self.details),
        )

        self.worker = ReleaseRefreshWorker(
            self.db,
            self.client,
            clock=lambda: self.now,
            monotonic=lambda: self.tick,
        )

    def candidate(self, i, checked_at=None):
        return ReleaseCandidate(
            media_type="movie",
            tmdb_id=i,
            title=f"Film {i}",
            media_class="film",
            checked_at=checked_at,
            snapshot=(
                ReleaseSnapshot("Released", ())
                if checked_at is not None
                else None
            ),
        )

    async def details(self, media_type, tmdb_id, **kwargs):
        return self.parser._parse_details(
            media_type,
            {
                "id": tmdb_id,
                "title": f"Film {tmdb_id}",
                "release_date": "2027-02-10",
            },
        )

    async def run_refresh(self, candidates):
        self.db.release_candidates.return_value = tuple(candidates)

        return await self.worker.refresh(
            1, region="US",
        )

    async def test_batch_limit_and_priority(self):
        candidates = [
            self.candidate(i)
            for i in range(1, 26)
        ]

        candidates.append(
            self.candidate(
                100,
                self.now - CACHE_MAX_AGE,
            )
        )

        result = await self.run_refresh(
            reversed(candidates)
        )

        self.assertEqual(result.eligible, 26)
        self.assertEqual(result.attempted, 20)
        self.assertEqual(result.updated, 20)
        self.assertEqual(result.stop_reason, "batch_limit")

        self.assertEqual(
            [
                call.args[1]
                for call in self.client.details.await_args_list
            ],
            list(range(1, 21)),
        )

        self.assertTrue(all(
            call.kwargs["force_refresh"]
            for call in self.client.details.await_args_list
        ))

    async def test_fresh_metadata_skipped(self):
        result = await self.run_refresh([
            self.candidate(1, self.now),
            self.candidate(2, self.now - CACHE_MAX_AGE),
        ])

        self.assertEqual(result.stale, 1)
        self.assertEqual(result.updated, 1)

        self.client.details.assert_awaited_once_with(
            "movie", 2, force_refresh=True,
        )

    async def test_failure_and_retry_cooldown(self):
        async def fetch(media_type, tmdb_id, **kwargs):
            if tmdb_id == 1:
                raise TmdbError("Synthetic failure")

            return await self.details(
                media_type, tmdb_id, **kwargs,
            )

        self.client.details.side_effect = fetch

        result = await self.run_refresh([
            self.candidate(1),
            self.candidate(2),
        ])

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.updated, 1)

        stored = self.db.store_release_snapshot.await_args_list

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].args[0].tmdb_id, 2)

        result = await self.run_refresh([
            self.candidate(1)
        ])

        self.assertEqual(result.attempted, 0)
        self.assertEqual(result.skipped, 1)

        self.tick += RETRY_DELAY

        result = await self.run_refresh([
            self.candidate(1)
        ])

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.failed, 1)

    async def test_rate_limit_stops_batch(self):
        self.client.details.side_effect = TmdbError(
            "Synthetic rate limit",
            status_code=429,
        )

        result = await self.run_refresh([
            self.candidate(1),
            self.candidate(2),
        ])

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.stop_reason, "rate_limited")

        result = await self.run_refresh([
            self.candidate(2),
        ])

        self.assertEqual(result.stop_reason, "paused")
        self.assertEqual(result.attempted, 0)

        self.db.store_release_snapshot.assert_not_awaited()

    async def test_authorization_failure_stops_batch(self):
        self.client.details.side_effect = TmdbError(
            "Synthetic authorization error",
            status_code=401,
        )

        result = await self.run_refresh([
            self.candidate(1),
            self.candidate(2),
        ])

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.stop_reason, "authorization")

        self.db.store_release_snapshot.assert_not_awaited()

    async def test_identity_mismatch_rejected(self):
        details = await self.details("movie", 999)

        self.client.details.side_effect = None
        self.client.details.return_value = details

        result = await self.run_refresh([
            self.candidate(1)
        ])

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.updated, 0)

        self.db.store_release_snapshot.assert_not_awaited()

    async def test_concurrent_refresh_rejected(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fetch(media_type, tmdb_id, **kwargs):
            entered.set()
            await release.wait()

            return await self.details(
                media_type, tmdb_id, **kwargs,
            )

        self.client.details.side_effect = fetch
        self.db.release_candidates.return_value = (
            self.candidate(1),
        )

        task = asyncio.create_task(
            self.worker.refresh(1, region="US")
        )

        try:
            await asyncio.wait_for(
                entered.wait(), timeout=2,
            )

            result = await self.worker.refresh(
                2, region="US",
            )

            self.assertEqual(result.stop_reason, "busy")
            self.assertEqual(result.attempted, 0)

        finally:
            release.set()
            await task

    async def test_refresh_time_budget(self):
        async def fetch(media_type, tmdb_id, **kwargs):
            self.tick += 31

            return await self.details(
                media_type, tmdb_id, **kwargs,
            )

        self.client.details.side_effect = fetch

        result = await self.run_refresh([
            self.candidate(1),
            self.candidate(2),
        ])

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.stop_reason, "time_budget")

    async def test_force_refresh_bypasses_memory_cache(self):
        previous = await self.details("movie", 1)

        self.parser._details_cache[("movie", 1)] = (
            time.monotonic(),
            previous,
        )

        self.parser._get = AsyncMock(return_value={
            "id": 1,
            "title": "Updated Film",
            "release_date": "2027-03-01",
        })

        cached = await self.parser.details("movie", 1)

        self.assertEqual(cached, previous)
        self.parser._get.assert_not_awaited()

        refreshed = await self.parser.details(
            "movie", 1, force_refresh=True,
        )

        self.assertEqual(
            refreshed.release_date,
            "2027-03-01",
        )

        self.parser._get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
