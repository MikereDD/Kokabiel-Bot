from __future__ import annotations

import json
import tempfile
import unittest

from pathlib import Path

from imports.db_preview import (
    ExistingUserSnapshot,
    build_database_preview,
)
from imports.merge import analyze_import
from imports.resolve import resolve_import_plan
from models import SearchResult


SIMKL_SERIES = {
    "shows": [
        {
            "added_to_watchlist_at": "2026-02-14T16:28:58Z",
            "last_watched_at": "2024-01-02T03:04:05Z",
            "user_rated_at": "2026-02-14T16:28:58Z",
            "user_rating": 9,
            "status": "completed",
            # Deliberately claims later progress than the explicit history.
            # Kokabiel must NOT invent episodes 3-9 from this marker.
            "last_watched": "S01E09",
            "next_to_watch": None,
            "watched_episodes_count": 9,
            "total_episodes_count": 9,
            "not_aired_episodes_count": 0,
            "show": {
                "title": "Example Show",
                "year": 2020,
                "runtime": 45,
                "ids": {
                    "simkl": 101,
                    "imdb": "tt1000001",
                    "tmdb": "200",
                    "tvdb": "300",
                },
            },
            "seasons": [
                {
                    "number": 1,
                    "episodes": [
                        {
                            "number": 1,
                            "watched_at": "2024-01-01T03:04:05Z",
                        },
                        {
                            "number": 2,
                            "watched_at": "2024-01-02T03:04:05Z",
                        },
                    ],
                }
            ],
        }
    ],
    "anime": [],
    "movies": [],
}


class CountingTmdb:
    def __init__(self) -> None:
        self.lookup_calls = 0

    async def lookup_result(
        self,
        media_type,
        tmdb_id,
    ):
        self.lookup_calls += 1

        return SearchResult(
            media_type="tv",
            tmdb_id=200,
            title="Example Show",
            original_title="Example Show",
            year="2020",
            overview="",
            poster_path=None,
        )

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


class SimklJsonPipelineTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_completed_series_plans_one_state_and_exact_episode_union(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "deadbeef.json"

            path.write_text(
                json.dumps(SIMKL_SERIES),
                encoding="utf-8",
            )

            plan = analyze_import(
                str(path),
                source_name="watchlists.json",
            )

        # Parent series + exactly two explicit episode records.
        self.assertEqual(
            plan.raw_record_count,
            3,
        )

        tmdb = CountingTmdb()

        resolution = await resolve_import_plan(
            plan,
            tmdb,
        )

        self.assertEqual(
            resolution.resolved_count,
            3,
        )

        # Parent and episodes share one canonical series identity.
        self.assertEqual(
            tmdb.lookup_calls,
            1,
        )

        preview = build_database_preview(
            resolution,
            ExistingUserSnapshot(
                user_id=1,
                states={},
                ratings={},
                episodes={},
                media=frozenset(),
            ),
        )

        self.assertEqual(
            preview.review_count,
            0,
        )

        self.assertEqual(
            preview.media_add_count,
            1,
        )

        # A brand-new completed show should require one state insert,
        # not an intermediate Watching insert followed by an update.
        self.assertEqual(
            preview.state_add_count,
            1,
        )
        self.assertEqual(
            preview.state_update_count,
            0,
        )

        self.assertEqual(
            preview.rating_add_count,
            1,
        )

        self.assertEqual(
            preview.episode_add_count,
            2,
        )

        additions = {
            episode
            for item in preview.items
            for episode in item.episode_additions
        }

        self.assertEqual(
            additions,
            {
                (1, 1),
                (1, 2),
            },
        )

        # The parent record, not an episode row, should establish the
        # completed series state.
        state_adds = [
            item
            for item in preview.items
            if item.state_action == "add"
        ]

        self.assertEqual(
            len(state_adds),
            1,
        )

        self.assertEqual(
            state_adds[0].after_status,
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
