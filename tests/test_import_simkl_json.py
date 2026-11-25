from __future__ import annotations

import json
import tempfile
import unittest

from pathlib import Path

from imports.detect import (
    ImportDetectionError,
    detect_import_source,
)
from imports.parse import parse_import


SIMKL_JSON = {
    "shows": [
        {
            "added_to_watchlist_at": "2026-02-14T16:28:58Z",
            "last_watched_at": "2024-01-02T03:04:05Z",
            "user_rated_at": "2026-02-14T16:28:58Z",
            "user_rating": 9,
            "status": "completed",
            "last_watched": "S01E02",
            "next_to_watch": None,
            "watched_episodes_count": 2,
            "total_episodes_count": 2,
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
    "anime": [
        {
            "added_to_watchlist_at": "2026-02-14T16:28:58Z",
            "last_watched_at": "2020-01-01T00:00:00Z",
            "user_rated_at": None,
            "user_rating": None,
            "status": "completed",
            "last_watched": None,
            "next_to_watch": None,
            "watched_episodes_count": 1,
            "total_episodes_count": 1,
            "not_aired_episodes_count": 0,
            "show": {
                "title": "Example Anime Movie",
                "year": 1988,
                "runtime": 90,
                "ids": {
                    "simkl": 401,
                    "imdb": "tt1000401",
                    "tmdb": "400",
                },
            },
            "anime_type": "movie",
        },
        {
            "added_to_watchlist_at": "2026-02-14T16:28:58Z",
            "last_watched_at": "2022-01-08T18:12:56Z",
            "user_rated_at": None,
            "user_rating": None,
            "status": "watching",
            "last_watched": "E1",
            "next_to_watch": "E2",
            "watched_episodes_count": 1,
            "total_episodes_count": 13,
            "not_aired_episodes_count": 0,
            "show": {
                "title": "Example Anime TV",
                "year": 2021,
                "runtime": 25,
                "ids": {
                    "simkl": 501,
                    "imdb": "tt1000501",
                    "tmdb": "500",
                    "tvdb": "550",
                },
            },
            "anime_type": "tv",
            "seasons": [
                {
                    "number": 1,
                    "episodes": [
                        {
                            "number": 1,
                            "watched_at": "2022-01-08T18:12:56Z",
                        }
                    ],
                }
            ],
        },
        {
            "added_to_watchlist_at": "2026-02-14T16:28:58Z",
            "last_watched_at": "2021-05-16T23:06:00Z",
            "user_rated_at": None,
            "user_rating": None,
            "status": "completed",
            "last_watched": None,
            "next_to_watch": None,
            "watched_episodes_count": 1,
            "total_episodes_count": 1,
            "not_aired_episodes_count": 0,
            "show": {
                "title": "Example Anime Special",
                "year": 2006,
                "runtime": 105,
                "ids": {
                    "simkl": 601,
                    "imdb": "tt1000601",
                    "tmdbm": "600",
                },
            },
            "anime_type": "special",
        },
    ],
    "movies": [
        {
            "added_to_watchlist_at": "2026-02-14T16:28:57Z",
            "last_watched_at": "2025-01-09T01:48:03Z",
            "user_rated_at": "2026-02-14T16:28:57Z",
            "user_rating": 8,
            "status": "completed",
            "watched_episodes_count": 0,
            "total_episodes_count": 0,
            "not_aired_episodes_count": 0,
            "movie": {
                "title": "Example Movie",
                "year": 2008,
                "runtime": 152,
                "ids": {
                    "simkl": 701,
                    "imdb": "tt1000701",
                    "tmdb": "700",
                    "tvdb": "750",
                },
            },
        },
        {
            "added_to_watchlist_at": "2022-06-08T21:08:33Z",
            "last_watched_at": None,
            "user_rated_at": None,
            "user_rating": None,
            "status": "plantowatch",
            "watched_episodes_count": 0,
            "total_episodes_count": 0,
            "not_aired_episodes_count": 0,
            "movie": {
                "title": "Plan Movie",
                "year": 1920,
                "runtime": 76,
                "ids": {
                    "simkl": 702,
                    "imdb": "tt1000702",
                    "tmdb": "701",
                },
            },
        },
    ],
}


class SimklJsonImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_export(self) -> Path:
        path = self.root / "deadbeef.json"
        path.write_text(
            json.dumps(SIMKL_JSON),
            encoding="utf-8",
        )
        return path

    def test_detects_simkl_watchlists_json_by_content(self) -> None:
        path = self.write_export()

        detected = detect_import_source(
            path,
            source_name="watchlists.json",
        )

        self.assertEqual(detected.source, "simkl")

    def test_arbitrary_json_named_watchlists_is_rejected(self) -> None:
        path = self.root / "deadbeef.json"
        path.write_text(
            '{"hello":"world"}',
            encoding="utf-8",
        )

        with self.assertRaises(ImportDetectionError):
            detect_import_source(
                path,
                source_name="watchlists.json",
            )

    def test_parses_series_parent_and_exact_episode_history(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        parents = [
            record
            for record in parsed.records
            if (
                record.title == "Example Show"
                and record.media_type == "tv"
            )
        ]

        self.assertEqual(len(parents), 1)

        parent = parents[0]

        self.assertEqual(parent.tmdb_id, 200)
        self.assertEqual(parent.imdb_id, "tt1000001")
        self.assertEqual(parent.tvdb_id, 300)
        self.assertEqual(parent.simkl_id, 101)
        self.assertEqual(parent.status, "completed")
        self.assertEqual(parent.rating, 9.0)
        self.assertEqual(
            parent.rated_at,
            "2026-02-14T16:28:58Z",
        )

        episodes = sorted(
            (
                record.season,
                record.episode,
                record.watched_at,
            )
            for record in parsed.records
            if (
                record.title == "Example Show"
                and record.media_type == "episode"
            )
        )

        self.assertEqual(
            episodes,
            [
                (
                    1,
                    1,
                    "2024-01-01T03:04:05Z",
                ),
                (
                    1,
                    2,
                    "2024-01-02T03:04:05Z",
                ),
            ],
        )

    def test_completed_movie_maps_to_watched_without_movie_status_review(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        record = next(
            record
            for record in parsed.records
            if record.title == "Example Movie"
        )

        self.assertEqual(record.media_type, "movie")
        self.assertEqual(record.tmdb_id, 700)
        self.assertTrue(record.watched)
        self.assertEqual(
            record.watched_at,
            "2025-01-09T01:48:03Z",
        )
        self.assertIsNone(record.status)
        self.assertEqual(record.rating, 8.0)
        self.assertEqual(
            record.rated_at,
            "2026-02-14T16:28:57Z",
        )

    def test_plantowatch_movie_maps_to_watchlist(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        record = next(
            record
            for record in parsed.records
            if record.title == "Plan Movie"
        )

        self.assertEqual(record.media_type, "movie")
        self.assertFalse(record.watched)
        self.assertTrue(record.watchlist)
        self.assertEqual(
            record.status,
            "plan_to_watch",
        )

    def test_anime_movie_maps_to_movie(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        record = next(
            record
            for record in parsed.records
            if record.title == "Example Anime Movie"
        )

        self.assertEqual(record.media_type, "movie")
        self.assertEqual(record.tmdb_id, 400)
        self.assertTrue(record.watched)

    def test_anime_tv_maps_to_series_and_episode_history(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        parent = next(
            record
            for record in parsed.records
            if (
                record.title == "Example Anime TV"
                and record.media_type == "tv"
            )
        )

        episode = next(
            record
            for record in parsed.records
            if (
                record.title == "Example Anime TV"
                and record.media_type == "episode"
            )
        )

        self.assertEqual(parent.status, "watching")
        self.assertEqual(parent.tmdb_id, 500)

        self.assertEqual(episode.tmdb_id, 500)
        self.assertEqual(episode.season, 1)
        self.assertEqual(episode.episode, 1)
        self.assertTrue(episode.watched)

    def test_anime_special_tmdbm_maps_to_movie(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        record = next(
            record
            for record in parsed.records
            if record.title == "Example Anime Special"
        )

        self.assertEqual(record.media_type, "movie")
        self.assertEqual(record.tmdb_id, 600)
        self.assertTrue(record.watched)

    def test_source_file_preserves_original_export_name(self) -> None:
        parsed = parse_import(
            self.write_export(),
            source_name="watchlists.json",
        )

        self.assertTrue(parsed.records)

        self.assertTrue(
            all(
                record.source_file
                == "watchlists.json"
                for record in parsed.records
            )
        )


if __name__ == "__main__":
    unittest.main()
