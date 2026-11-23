from __future__ import annotations

import unittest

from imports.merge import (
    ImportConflict,
    ImportPlan,
    ImportPlanItem,
)
from imports.models import NormalizedImportRecord
from imports.resolve import resolve_import_plan
from models import SearchResult
from tmdb import TmdbClient


def result(
    media_type,
    tmdb_id,
    title,
    year,
    *,
    original_title=None,
):
    return SearchResult(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        original_title=(
            original_title or title
        ),
        year=str(year),
        overview="",
        poster_path=None,
    )


class FakeTmdb:
    def __init__(self):
        self.lookup = {}
        self.imdb = {}
        self.movies = {}
        self.tv = {}
        self.multi = {}

    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        return self.lookup[
            (media_type, tmdb_id)
        ]

    async def find_by_imdb(
        self,
        imdb_id,
        limit=8,
    ):
        return self.imdb.get(
            imdb_id,
            [],
        )[:limit]

    async def search_movie(
        self,
        query,
        limit=8,
    ):
        return self.movies.get(
            query,
            [],
        )[:limit]

    async def search_tv(
        self,
        query,
        limit=8,
    ):
        return self.tv.get(
            query,
            [],
        )[:limit]

    async def search_multi(
        self,
        query,
        limit=8,
    ):
        return self.multi.get(
            query,
            [],
        )[:limit]


def item(
    record,
    *,
    conflicts=(),
):
    return ImportPlanItem(
        key="test",
        identity_basis="test",
        proposed=record,
        source_record_count=1,
        source_files=(),
        conflicts=tuple(conflicts),
    )


def plan(one):
    return ImportPlan(
        source=one.proposed.source,
        raw_record_count=1,
        items=(one,),
    )


class ImportResolutionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_existing_tmdb_id_is_verified(self):
        tmdb = FakeTmdb()

        tmdb.lookup[
            ("movie", 348)
        ] = result(
            "movie",
            348,
            "Alien",
            1979,
        )

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="trakt",
                        media_type="movie",
                        title="Alien",
                        year=1979,
                        tmdb_id=348,
                    )
                )
            ),
            tmdb,
        )

        resolved = preview.items[0]

        self.assertEqual(
            resolved.status,
            "resolved",
        )
        self.assertEqual(
            resolved.method,
            "tmdb_id",
        )
        self.assertEqual(
            resolved.resolved.tmdb_id,
            348,
        )

    async def test_imdb_id_resolves_exactly(self):
        tmdb = FakeTmdb()

        tmdb.imdb[
            "tt0078748"
        ] = [
            result(
                "movie",
                348,
                "Alien",
                1979,
            )
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="imdb",
                        media_type="movie",
                        title="Alien",
                        year=1979,
                        imdb_id="tt0078748",
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.resolved_count,
            1,
        )
        self.assertEqual(
            preview.items[0].method,
            "imdb_id",
        )

    async def test_unique_exact_title_year_resolves(self):
        tmdb = FakeTmdb()

        tmdb.movies["Alien"] = [
            result(
                "movie",
                348,
                "Alien",
                1979,
            ),
            result(
                "movie",
                999,
                "Alien Something",
                2020,
            ),
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="letterboxd",
                        media_type="movie",
                        title="Alien",
                        year=1979,
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "resolved",
        )
        self.assertEqual(
            preview.items[0].resolved.tmdb_id,
            348,
        )

    async def test_title_without_year_requires_review(self):
        tmdb = FakeTmdb()

        tmdb.movies["Example"] = [
            result(
                "movie",
                1,
                "Example",
                2020,
            )
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="letterboxd",
                        media_type="movie",
                        title="Example",
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "needs_review",
        )

    async def test_tmdb_id_identity_mismatch_requires_review(self):
        tmdb = FakeTmdb()

        tmdb.lookup[
            ("movie", 348)
        ] = result(
            "movie",
            348,
            "Alien",
            1979,
        )

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="trakt",
                        media_type="movie",
                        title="The Wrong Movie",
                        year=2020,
                        tmdb_id=348,
                    )
                )
            ),
            tmdb,
        )

        resolved = preview.items[0]

        self.assertEqual(
            resolved.status,
            "needs_review",
        )
        self.assertEqual(
            resolved.method,
            "tmdb_id",
        )
        self.assertEqual(
            resolved.candidates[0].tmdb_id,
            348,
        )

    async def test_ambiguous_exact_matches_require_review(self):
        tmdb = FakeTmdb()

        tmdb.movies["Example"] = [
            result(
                "movie",
                1,
                "Example",
                2020,
            ),
            result(
                "movie",
                2,
                "Example",
                2020,
            ),
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="letterboxd",
                        media_type="movie",
                        title="Example",
                        year=2020,
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "needs_review",
        )
        self.assertEqual(
            len(
                preview.items[0].candidates
            ),
            2,
        )

    async def test_non_exact_search_never_autoresolves(self):
        tmdb = FakeTmdb()

        tmdb.movies["Alien"] = [
            result(
                "movie",
                100,
                "Alien Remake",
                1979,
            )
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="letterboxd",
                        media_type="movie",
                        title="Alien",
                        year=1979,
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "needs_review",
        )

    async def test_no_candidate_is_unresolved(self):
        tmdb = FakeTmdb()

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="letterboxd",
                        media_type="movie",
                        title="Missing",
                        year=1999,
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "unresolved",
        )

    async def test_failed_imdb_find_does_not_fuzzy_autoresolve(self):
        tmdb = FakeTmdb()

        tmdb.movies["Alien"] = [
            result(
                "movie",
                348,
                "Alien",
                1979,
            )
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="imdb",
                        media_type="movie",
                        title="Alien",
                        year=1979,
                        imdb_id="tt0000000",
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "needs_review",
        )
        self.assertEqual(
            preview.items[0].method,
            "title_year",
        )

    async def test_imdb_episode_title_is_not_used_as_series_guess(self):
        tmdb = FakeTmdb()

        tmdb.tv["Episode Name"] = [
            result(
                "tv",
                123,
                "Episode Name",
                2020,
            )
        ]

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="imdb",
                        media_type="episode",
                        title="Episode Name",
                        year=2020,
                        imdb_id="tt1234567",
                    )
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "unresolved",
        )

    async def test_identity_conflict_blocks_autoresolution(self):
        tmdb = FakeTmdb()

        preview = await resolve_import_plan(
            plan(
                item(
                    NormalizedImportRecord(
                        source="trakt",
                        media_type="movie",
                        tmdb_id=348,
                    ),
                    conflicts=(
                        ImportConflict(
                            field="tmdb_id",
                            values=("348", "999"),
                            message="conflict",
                        ),
                    ),
                )
            ),
            tmdb,
        )

        self.assertEqual(
            preview.items[0].status,
            "needs_review",
        )


class TmdbResolutionMethodTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_find_by_imdb_parses_movie_and_tv(self):
        class StubTmdb(TmdbClient):
            async def _get(
                self,
                path,
                **params,
            ):
                self.last_path = path
                self.last_params = params

                return {
                    "movie_results": [
                        {
                            "id": 348,
                            "title": "Alien",
                            "original_title": "Alien",
                            "release_date": "1979-05-25",
                        }
                    ],
                    "tv_results": [
                        {
                            "id": 1396,
                            "name": "Breaking Bad",
                            "original_name": "Breaking Bad",
                            "first_air_date": "2008-01-20",
                        }
                    ],
                }

        client = StubTmdb(
            None,
            "test-token",
        )

        found = await client.find_by_imdb(
            "tt0078748"
        )

        self.assertEqual(
            client.last_path,
            "/find/tt0078748",
        )
        self.assertEqual(
            client.last_params,
            {
                "external_source": "imdb_id"
            },
        )
        self.assertEqual(
            {
                candidate.media_type
                for candidate in found
            },
            {"movie", "tv"},
        )

    async def test_lookup_result_uses_canonical_endpoint(self):
        class StubTmdb(TmdbClient):
            async def _get(
                self,
                path,
                **params,
            ):
                self.last_path = path

                return {
                    "id": 348,
                    "title": "Alien",
                    "original_title": "Alien",
                    "release_date": "1979-05-25",
                }

        client = StubTmdb(
            None,
            "test-token",
        )

        found = await client.lookup_result(
            "movie",
            348,
        )

        self.assertEqual(
            client.last_path,
            "/movie/348",
        )
        self.assertEqual(
            found.tmdb_id,
            348,
        )


if __name__ == "__main__":
    unittest.main()
