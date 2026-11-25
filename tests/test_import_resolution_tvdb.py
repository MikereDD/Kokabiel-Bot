from __future__ import annotations

import unittest

from imports.models import NormalizedImportRecord
from imports.resolve import resolve_import_plan
from tests.test_import_resolution import (
    FakeTmdb,
    item,
    plan,
    result,
)
from tmdb import TmdbError


class TvdbTmdb(FakeTmdb):
    def __init__(self):
        super().__init__()
        self.tvdb = {}
        self.tvdb_calls = 0
        self.imdb_calls = 0
        self.lookup_calls = 0

    async def find_by_tvdb(
        self,
        tvdb_id,
        limit=8,
    ):
        self.tvdb_calls += 1

        value = self.tvdb.get(tvdb_id, [])

        if isinstance(value, Exception):
            raise value

        return value[:limit]

    async def find_by_imdb(
        self,
        imdb_id,
        limit=8,
    ):
        self.imdb_calls += 1
        return await super().find_by_imdb(imdb_id, limit)

    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        self.lookup_calls += 1
        return await super().lookup_result(
            media_type,
            tmdb_id,
        )


def tvdb_plan(**overrides):
    values = {
        "source": "simkl",
        "media_type": "tv",
        "title": "Example Show",
        "year": 2020,
        "tvdb_id": 300,
    }

    values.update(overrides)

    return plan(
        item(
            NormalizedImportRecord(**values)
        )
    )


class ImportTvdbResolutionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_unique_tvdb_identity_resolves(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 1)
        self.assertEqual(preview.items[0].method, "tvdb_id")
        self.assertEqual(preview.items[0].resolved.tmdb_id, 200)
        self.assertEqual(tmdb.tvdb_calls, 1)

    async def test_tvdb_title_mismatch_requires_review(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = [
            result("tv", 200, "Different Show", 2020)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.review_count, 1)
        self.assertIsNone(preview.items[0].resolved)

    async def test_tvdb_year_mismatch_requires_review(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = [
            result("tv", 200, "Example Show", 2021)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.review_count, 1)

    async def test_ambiguous_tvdb_identity_requires_review(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = [
            result("tv", 200, "Example Show", 2020),
            result("tv", 201, "Example Show", 2020),
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.review_count, 1)
        self.assertIsNone(preview.items[0].resolved)

    async def test_missing_tvdb_does_not_autoresolve_title(self):
        tmdb = TvdbTmdb()
        tmdb.tv["Example Show"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 0)
        self.assertEqual(preview.review_count, 1)

    async def test_missing_tvdb_without_candidates_is_unresolved(self):
        tmdb = TvdbTmdb()

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.unresolved_count, 1)
        self.assertEqual(preview.items[0].method, "tvdb_id")
        self.assertEqual(tmdb.tvdb_calls, 1)

    async def test_incompatible_media_type_cannot_resolve(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = [
            result("movie", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 0)
        self.assertEqual(tmdb.tvdb_calls, 1)

    async def test_tmdb_retains_precedence(self):
        tmdb = TvdbTmdb()
        tmdb.lookup[("tv", 200)] = result(
            "tv", 200, "Example Show", 2020
        )

        preview = await resolve_import_plan(
            tvdb_plan(tmdb_id=200),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 1)
        self.assertEqual(preview.items[0].method, "tmdb_id")
        self.assertEqual(tmdb.tvdb_calls, 0)

    async def test_imdb_retains_precedence(self):
        tmdb = TvdbTmdb()
        tmdb.imdb["tt1000001"] = [
            result("tv", 200, "Example Show", 2020)
        ]

        preview = await resolve_import_plan(
            tvdb_plan(imdb_id="tt1000001"),
            tmdb,
        )

        self.assertEqual(preview.resolved_count, 1)
        self.assertEqual(preview.items[0].method, "imdb_id")
        self.assertEqual(tmdb.tvdb_calls, 0)

    async def test_tvdb_service_failure_aborts_resolution(self):
        tmdb = TvdbTmdb()
        tmdb.tvdb[300] = TmdbError(
            "Synthetic rate limit",
            status_code=429,
        )

        with self.assertRaises(TmdbError):
            await resolve_import_plan(
                tvdb_plan(),
                tmdb,
            )


class TvdbClientContractTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_tvdb_endpoint_returns_only_series_identities(self):
        from tmdb import TmdbClient

        class StubClient(TmdbClient):
            def __init__(self):
                self.calls = []

            async def _get(self, path, **params):
                self.calls.append((path, params))

                return {
                    "tv_results": [
                        {
                            "id": 200,
                            "name": "Example Show",
                            "original_name": "Example Show",
                            "first_air_date": "2020-01-01",
                            "overview": "",
                        }
                    ],
                    "movie_results": [
                        {
                            "id": 999,
                            "title": "Wrong Movie",
                        }
                    ],
                    "tv_season_results": [
                        {"id": 888}
                    ],
                    "tv_episode_results": [
                        {"id": 777}
                    ],
                }

        client = StubClient()

        results = await client.find_by_tvdb(300)

        self.assertEqual(
            client.calls,
            [
                (
                    "/find/300",
                    {"external_source": "tvdb_id"},
                )
            ],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].media_type, "tv")
        self.assertEqual(results[0].tmdb_id, 200)
        self.assertEqual(results[0].year, "2020")


if __name__ == "__main__":
    unittest.main()
