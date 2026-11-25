from __future__ import annotations

import unittest

from imports.db_preview import (
    ExistingUserSnapshot,
    build_database_preview,
)
from imports.merge import ImportPlan
from imports.resolve import resolve_import_plan
from tests.test_import_resolution_cache import (
    CountingTmdb,
    plan_item,
    result,
)
from tmdb import TmdbError


def series_plan():
    return ImportPlan(
        source="simkl",
        raw_record_count=3,
        items=(
            plan_item(
                "tv:tmdb:200",
                "tv",
            ),
            plan_item(
                "episode:1:1:tmdb:200",
                "episode",
                season=1,
                episode=1,
            ),
            plan_item(
                "episode:1:2:tmdb:200",
                "episode",
                season=1,
                episode=2,
            ),
        ),
    )


class FailingTmdb(CountingTmdb):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        self.lookup_calls += 1
        raise self.error


class RecoveringTmdb(FailingTmdb):
    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        self.lookup_calls += 1

        if self.lookup_calls == 1:
            raise self.error

        return result()


class ImportResolutionErrorTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_missing_identity_is_unresolved(self):
        tmdb = FailingTmdb(
            TmdbError(
                "Synthetic missing identity",
                status_code=404,
            )
        )

        preview = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        self.assertEqual(preview.unresolved_count, 3)
        self.assertEqual(preview.resolved_count, 0)

        for item in preview.items:
            self.assertEqual(item.method, "tmdb_id")
            self.assertIsNone(item.resolved)
            self.assertEqual(item.candidates, ())

    async def test_missing_identity_is_cached(self):
        tmdb = FailingTmdb(
            TmdbError(
                "Synthetic missing identity",
                status_code=404,
            )
        )

        preview = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        self.assertEqual(preview.unresolved_count, 3)
        self.assertEqual(tmdb.lookup_calls, 1)

    async def test_negative_cache_does_not_survive_import(self):
        tmdb = RecoveringTmdb(
            TmdbError(
                "Synthetic missing identity",
                status_code=404,
            )
        )

        first = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        second = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        self.assertEqual(first.unresolved_count, 3)
        self.assertEqual(second.resolved_count, 3)
        self.assertEqual(tmdb.lookup_calls, 2)

    async def test_missing_identity_produces_no_database_changes(self):
        tmdb = FailingTmdb(
            TmdbError(
                "Synthetic missing identity",
                status_code=404,
            )
        )

        resolution = await resolve_import_plan(
            series_plan(),
            tmdb,
        )

        preview = build_database_preview(
            resolution,
            ExistingUserSnapshot(
                user_id=1,
                states={},
                ratings={},
                episodes={},
            ),
        )

        self.assertEqual(preview.review_count, 3)
        self.assertEqual(preview.media_add_count, 0)
        self.assertEqual(preview.state_add_count, 0)
        self.assertEqual(preview.state_update_count, 0)
        self.assertEqual(preview.episode_add_count, 0)
        self.assertEqual(preview.rating_add_count, 0)

    async def test_service_errors_abort_resolution(self):
        for status in (400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                tmdb = FailingTmdb(
                    TmdbError(
                        "Synthetic service failure",
                        status_code=status,
                    )
                )

                with self.assertRaises(TmdbError) as caught:
                    await resolve_import_plan(
                        series_plan(),
                        tmdb,
                    )

                self.assertEqual(
                    caught.exception.status_code,
                    status,
                )
                self.assertEqual(tmdb.lookup_calls, 1)

    async def test_network_timeout_and_malformed_response_abort(self):
        for message in (
            "TMDB request timed out.",
            "TMDB network request failed.",
            "TMDB returned malformed JSON.",
        ):
            with self.subTest(message=message):
                tmdb = FailingTmdb(
                    TmdbError(message)
                )

                with self.assertRaises(TmdbError):
                    await resolve_import_plan(
                        series_plan(),
                        tmdb,
                    )

                self.assertEqual(tmdb.lookup_calls, 1)


if __name__ == "__main__":
    unittest.main()
