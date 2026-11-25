from __future__ import annotations

import unittest

from imports.merge import ImportPlan
from imports.models import NormalizedImportRecord
from imports.resolve import resolve_import_plan
from tests.test_import_resolution import item, result
from tests.test_import_resolution_fallback_cache import (
    CountingFallbackTmdb,
    series_plan,
)
from tmdb import TmdbError


class BoundaryTmdb(CountingFallbackTmdb):
    def __init__(self):
        super().__init__()
        self.movie_calls = 0

    async def search_movie(self, query, limit=8):
        self.movie_calls += 1
        return await super().search_movie(query, limit)


def mixed_plan(**identity):
    return ImportPlan(
        source="simkl",
        raw_record_count=2,
        items=tuple(
            item(
                NormalizedImportRecord(
                    source="simkl",
                    media_type=media_type,
                    title="Example",
                    year=2020,
                    **identity,
                )
            )
            for media_type in ("movie", "tv")
        ),
    )


class ImportResolutionBoundaryTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_direct_lookup_namespaces_are_isolated(self):
        tmdb = BoundaryTmdb()

        tmdb.lookup[("movie", 200)] = result(
            "movie", 200, "Example", 2020
        )
        tmdb.lookup[("tv", 200)] = result(
            "tv", 200, "Example", 2020
        )

        preview = await resolve_import_plan(
            mixed_plan(tmdb_id=200),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 2)
        self.assertEqual(tmdb.lookup_calls, 2)
        self.assertEqual(
            tuple(i.resolved.media_type for i in preview.items),
            ("movie", "tv"),
        )

    async def test_title_search_namespaces_are_isolated(self):
        tmdb = BoundaryTmdb()

        tmdb.movies["Example"] = [
            result("movie", 200, "Example", 2020)
        ]
        tmdb.tv["Example"] = [
            result("tv", 300, "Example", 2020)
        ]

        preview = await resolve_import_plan(
            mixed_plan(),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 2)
        self.assertEqual(tmdb.movie_calls, 1)
        self.assertEqual(tmdb.title_calls, 1)
        self.assertEqual(
            tuple(i.resolved.tmdb_id for i in preview.items),
            (200, 300),
        )

    async def test_cached_imdb_results_are_filtered_per_record(self):
        tmdb = BoundaryTmdb()

        tmdb.imdb["tt1000001"] = [
            result("movie", 200, "Example", 2020),
            result("tv", 300, "Example", 2020),
        ]

        preview = await resolve_import_plan(
            mixed_plan(imdb_id="tt1000001"),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 2)
        self.assertEqual(tmdb.imdb_calls, 1)
        self.assertEqual(
            tuple(i.resolved.media_type for i in preview.items),
            ("movie", "tv"),
        )

    async def test_imdb_service_failures_propagate(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):

                class FailingImdb(BoundaryTmdb):
                    async def find_by_imdb(self, imdb_id, limit=8):
                        self.imdb_calls += 1
                        raise TmdbError(
                            "Synthetic IMDb lookup failure",
                            status_code=status,
                        )

                tmdb = FailingImdb()

                with self.assertRaises(TmdbError) as caught:
                    await resolve_import_plan(
                        series_plan(imdb_id="tt1000001"),
                        tmdb,
                    )

                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(tmdb.imdb_calls, 1)
                self.assertEqual(tmdb.title_calls, 0)

    async def test_title_search_service_failures_propagate(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):

                class FailingSearch(BoundaryTmdb):
                    async def search_tv(self, query, limit=8):
                        self.title_calls += 1
                        raise TmdbError(
                            "Synthetic title search failure",
                            status_code=status,
                        )

                tmdb = FailingSearch()

                with self.assertRaises(TmdbError) as caught:
                    await resolve_import_plan(
                        series_plan(),
                        tmdb,
                    )

                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(tmdb.title_calls, 1)


if __name__ == "__main__":
    unittest.main()
