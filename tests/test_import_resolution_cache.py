from __future__ import annotations

import unittest

from imports.merge import (
    ImportPlan,
    ImportPlanItem,
)
from imports.models import NormalizedImportRecord
from imports.resolve import resolve_import_plan
from models import SearchResult


def result():
    return SearchResult(
        media_type="tv",
        tmdb_id=200,
        title="Example Show",
        original_title="Example Show",
        year="2020",
        overview="",
        poster_path=None,
    )


class CountingTmdb:
    def __init__(self):
        self.lookup_calls = 0

    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        self.lookup_calls += 1
        return result()

    async def find_by_imdb(
        self,
        imdb_id,
        limit=8,
    ):
        return []

    async def search_movie(
        self,
        query,
        limit=8,
    ):
        return []

    async def search_tv(
        self,
        query,
        limit=8,
    ):
        return []

    async def search_multi(
        self,
        query,
        limit=8,
    ):
        return []


def plan_item(
    key,
    media_type,
    *,
    season=None,
    episode=None,
):
    return ImportPlanItem(
        key=key,
        identity_basis="tmdb",
        proposed=NormalizedImportRecord(
            source="simkl",
            media_type=media_type,
            title="Example Show",
            year=2020,
            tmdb_id=200,
            simkl_id=101,
            season=season,
            episode=episode,
            watched=True,
        ),
        source_record_count=1,
        source_files=("watchlists.json",),
        conflicts=(),
    )


class ImportResolutionCacheTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_repeated_tmdb_identity_is_verified_once_per_import(self):
        tmdb = CountingTmdb()

        plan = ImportPlan(
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

        preview = await resolve_import_plan(
            plan,
            tmdb,
        )

        self.assertEqual(
            preview.resolved_count,
            3,
        )

        self.assertEqual(
            tmdb.lookup_calls,
            1,
        )


if __name__ == "__main__":
    unittest.main()
