from __future__ import annotations

import unittest
from dataclasses import replace

from imports.merge import ImportPlan
from imports.resolve import resolve_import_plan
from tests.test_import_resolution_cache import plan_item
from tests.test_import_resolution import result
from tests.test_import_resolution_tvdb import TvdbTmdb


class CountingFallbackTmdb(TvdbTmdb):
    def __init__(self):
        super().__init__()
        self.title_calls = 0

    async def search_tv(self, query, limit=8):
        self.title_calls += 1
        return await super().search_tv(query, limit)


def series_plan(**identity):
    items = (
        plan_item("parent", "tv"),
        plan_item("episode-one", "episode", season=1, episode=1),
        plan_item("episode-two", "episode", season=1, episode=2),
    )

    return ImportPlan(
        source="simkl",
        raw_record_count=3,
        items=tuple(
            replace(
                item,
                proposed=replace(
                    item.proposed,
                    tmdb_id=None,
                    **identity,
                ),
            )
            for item in items
        ),
    )


class ImportFallbackCacheTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_repeated_imdb_identity_is_queried_once(self):
        tmdb = CountingFallbackTmdb()

        tmdb.imdb["tt1000001"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            series_plan(imdb_id="tt1000001"),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 3)
        self.assertEqual(tmdb.imdb_calls, 1)
        self.assertEqual(tmdb.title_calls, 0)

    async def test_repeated_tvdb_identity_is_queried_once(self):
        tmdb = CountingFallbackTmdb()

        tmdb.tvdb[300] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            series_plan(tvdb_id=300),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 3)
        self.assertEqual(tmdb.tvdb_calls, 1)
        self.assertEqual(tmdb.title_calls, 0)

    async def test_repeated_title_search_is_queried_once(self):
        tmdb = CountingFallbackTmdb()

        tmdb.tv["Example Show"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 3)
        self.assertEqual(tmdb.title_calls, 1)

    async def test_empty_imdb_and_title_results_are_cached(self):
        tmdb = CountingFallbackTmdb()

        preview = await resolve_import_plan(
            series_plan(imdb_id="tt1000001"),
            tmdb,
        )

        self.assertEqual(preview.unresolved_count, 3)
        self.assertEqual(tmdb.imdb_calls, 1)
        self.assertEqual(tmdb.title_calls, 1)

    async def test_empty_tvdb_and_title_results_are_cached(self):
        tmdb = CountingFallbackTmdb()

        preview = await resolve_import_plan(
            series_plan(tvdb_id=300),
            tmdb,
        )

        self.assertEqual(preview.unresolved_count, 3)
        self.assertEqual(tmdb.tvdb_calls, 1)
        self.assertEqual(tmdb.title_calls, 1)

    async def test_title_cache_does_not_survive_import(self):
        tmdb = CountingFallbackTmdb()

        first = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        tmdb.tv["Example Show"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        second = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        self.assertEqual(first.unresolved_count, 3)
        self.assertEqual(second.resolved_count, 3)
        self.assertEqual(tmdb.title_calls, 2)

    async def test_cached_identity_still_checks_each_record(self):
        tmdb = CountingFallbackTmdb()

        tmdb.imdb["tt1000001"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        original = series_plan(imdb_id="tt1000001")

        mismatched = replace(
            original.items[2],
            proposed=replace(
                original.items[2].proposed,
                year=2021,
            ),
        )

        plan = replace(
            original,
            items=(
                original.items[0],
                original.items[1],
                mismatched,
            ),
        )

        preview = await resolve_import_plan(plan, tmdb)

        self.assertEqual(preview.resolved_count, 2)
        self.assertEqual(preview.review_count, 1)
        self.assertEqual(tmdb.imdb_calls, 1)


if __name__ == "__main__":
    unittest.main()
