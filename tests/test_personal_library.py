from __future__ import annotations

import sqlite3
import tempfile
import unittest

from contextlib import closing
from pathlib import Path

from database import Database


class PersonalLibraryTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.path = Path(temp.name) / "library.db"
        self.db = Database(self.path)

        await self.db.initialize()

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")

            media = []
            states = []

            for i in range(1, 601):
                media.extend([
                    (
                        "movie",
                        i,
                        f"Film {i:04d}",
                        str(2000 + i % 25),
                        "film",
                    ),
                    (
                        "tv",
                        i,
                        f"Series {i:04d}",
                        str(2000 + i % 25),
                        "tv_series",
                    ),
                ])

                states.extend([
                    (
                        1, "movie", i, "Tester",
                        int(i <= 50),
                        "none" if i <= 50 else "watched",
                        100,
                    ),
                    (
                        1, "tv", i, "Tester",
                        int(i <= 50),
                        "none" if i <= 50 else "watching",
                        100,
                    ),
                ])

            media.extend([
                (
                    "movie", 601,
                    "Documentary Film", "2024",
                    "documentary_film",
                ),
                (
                    "tv", 601,
                    "Documentary Series", "2024",
                    "documentary_series",
                ),
                (
                    "tv", 602,
                    "Documentary Limited Series", "2025",
                    "documentary_limited_series",
                ),
                (
                    "movie", 9000,
                    "Rated Only", "2026", "film",
                ),
                (
                    "movie", 9001,
                    "Other User Film", "2026", "film",
                ),
            ])

            states.extend([
                (1, "movie", 601, "Tester", 1, "none", 100),
                (1, "tv", 601, "Tester", 0, "completed", 100),
                (1, "tv", 602, "Tester", 0, "caught_up", 100),
                (2, "movie", 9001, "Other", 1, "watched", 100),
            ])

            conn.executemany("""
                INSERT INTO media (
                    media_type, tmdb_id, title,
                    year, media_class
                ) VALUES (?, ?, ?, ?, ?)
            """, media)

            conn.executemany("""
                INSERT INTO user_media_state (
                    user_id, media_type, tmdb_id,
                    user_name, watchlist, viewing_status,
                    last_chat_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, states)

            conn.executemany("""
                INSERT INTO ratings (
                    user_id, media_type, tmdb_id,
                    user_name, rating
                ) VALUES (?, ?, ?, ?, ?)
            """, [
                (1, "movie", 1, "Tester", 8),
                (1, "tv", 1, "Tester", 9),
                (1, "movie", 9000, "Tester", 10),
                (2, "movie", 1, "Other", 2),
            ])

            conn.executemany("""
                INSERT INTO episode_progress (
                    user_id, tmdb_id,
                    season_number, episode_number
                ) VALUES (?, ?, ?, ?)
            """, [
                (1, 1, 1, 1),
                (1, 1, 1, 2),
                (1, 1, 2, 1),
                (2, 1, 1, 1),
            ])

            conn.commit()

    async def query(self, **kwargs):
        return await self.db.personal_library_page(
            100, 1, **kwargs
        )

    async def test_complete_library_pagination(self):
        seen = set()
        page_number = 0

        while True:
            result = await self.query(
                page=page_number,
                page_size=20,
                sort="title",
            )

            self.assertEqual(result.total, 1204)
            self.assertEqual(result.page_count, 61)

            for entry in result.entries:
                key = (entry.media_type, entry.tmdb_id)
                self.assertNotIn(key, seen)
                seen.add(key)

            if not result.has_next:
                break

            page_number += 1

        self.assertEqual(len(seen), 1204)
        self.assertIn(("movie", 1), seen)
        self.assertIn(("tv", 1), seen)
        self.assertNotIn(("movie", 9001), seen)

    async def test_category_totals(self):
        expected = {
            "films": 601,
            "series": 600,
            "documentaries": 3,
        }

        for category, total in expected.items():
            with self.subTest(category=category):
                result = await self.query(
                    category=category
                )
                self.assertEqual(result.total, total)

    async def test_collection_totals(self):
        expected = {
            "all": 1204,
            "watchlist": 101,
            "watched": 552,
            "progress": 1102,
            "ratings": 3,
        }

        for collection, total in expected.items():
            with self.subTest(collection=collection):
                result = await self.query(
                    collection=collection
                )
                self.assertEqual(result.total, total)

    async def test_viewing_status_filters(self):
        expected = {
            "none": 102,
            "watching": 550,
            "watched": 550,
            "completed": 1,
            "caught_up": 1,
        }

        for status, total in expected.items():
            with self.subTest(status=status):
                result = await self.query(status=status)
                self.assertEqual(result.total, total)

    async def test_combined_filters(self):
        result = await self.query(
            category="documentaries",
            collection="watched",
            status="completed",
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(
            result.entries[0].title,
            "Documentary Series",
        )


    async def test_updated_sort_normalizes_timestamp_formats(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""
                UPDATE user_media_state
                SET updated_at = '2026-01-01 00:00:00'
                WHERE user_id = 1
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = '2026-01-01T00:00:00Z'
                WHERE user_id = 1
            """)

            conn.execute("""
                UPDATE user_media_state
                SET updated_at = '2027-01-01 23:59:00'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 1
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = '2027-01-01T00:01:00Z'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 1
            """)

            conn.execute("""
                UPDATE user_media_state
                SET updated_at = '2027-01-01 22:00:00'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 2
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = '2027-01-01T23:30:00+02:00'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 9000
            """)

            conn.commit()

        result = await self.query(
            sort="updated",
            page_size=3,
        )

        self.assertEqual(
            [(e.media_type, e.tmdb_id) for e in result.entries],
            [("movie", 1), ("movie", 2), ("movie", 9000)],
        )

        self.assertEqual(
            result.entries[0].updated_at,
            "2027-01-01 23:59:00",
        )

    async def test_added_sort_normalizes_timestamp_formats(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""
                UPDATE user_media_state
                SET created_at = '2026-01-01 00:00:00'
                WHERE user_id = 1
            """)

            conn.execute("""
                UPDATE user_media_state
                SET created_at = '2027-01-01 23:59:00'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 1
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = '2027-01-01T00:01:00Z'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 9000
            """)

            conn.commit()

        result = await self.query(
            sort="added",
            page_size=2,
        )

        self.assertEqual(
            [(e.media_type, e.tmdb_id) for e in result.entries],
            [("movie", 1), ("movie", 9000)],
        )

    async def test_updated_sort_handles_invalid_timestamps(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""
                UPDATE user_media_state
                SET updated_at = 'invalid'
                WHERE user_id = 1
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = 'invalid'
                WHERE user_id = 1
            """)

            conn.execute("""
                UPDATE ratings
                SET rated_at = '2027-01-01T12:00:00Z'
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 1
            """)

            conn.commit()

        result = await self.query(
            sort="updated",
            page_size=1,
        )

        self.assertEqual(
            result.entries[0].media_type,
            "movie",
        )

        self.assertEqual(result.entries[0].tmdb_id, 1)

        self.assertEqual(
            result.entries[0].updated_at,
            "2027-01-01T12:00:00Z",
        )

    async def test_rating_sort_and_ratings_only(self):
        result = await self.query(
            collection="ratings",
            sort="rating",
        )

        self.assertEqual(
            [entry.rating for entry in result.entries],
            [10, 9, 8],
        )

        self.assertEqual(
            result.entries[0].title,
            "Rated Only",
        )

        self.assertFalse(result.entries[0].watchlist)

        self.assertEqual(
            result.entries[0].viewing_status,
            "none",
        )

    async def test_episode_identity_and_user_isolation(self):
        result = await self.query(
            collection="ratings",
            sort="rating",
        )

        series = next(
            entry for entry in result.entries
            if entry.media_type == "tv"
        )

        film = next(
            entry for entry in result.entries
            if entry.media_type == "movie"
            and entry.tmdb_id == 1
        )

        self.assertEqual(series.watched_episodes, 3)
        self.assertEqual(film.watched_episodes, 0)
        self.assertEqual(film.rating, 8)

    async def test_other_user_library(self):
        result = await self.db.personal_library_page(
            100, 2
        )

        self.assertEqual(result.total, 2)

        self.assertEqual(
            {
                entry.tmdb_id
                for entry in result.entries
            },
            {1, 9001},
        )

    async def test_global_personal_library(self):
        original = await self.query(sort="title")

        other_chat = await self.db.personal_library_page(
            999, 1, sort="title"
        )

        self.assertEqual(original.total, other_chat.total)

        self.assertEqual(
            [entry.tmdb_id for entry in original.entries],
            [entry.tmdb_id for entry in other_chat.entries],
        )

    async def test_empty_library(self):
        result = await self.db.personal_library_page(
            100, 999
        )

        self.assertEqual(result.total, 0)
        self.assertEqual(result.entries, ())
        self.assertEqual(result.page_count, 1)
        self.assertFalse(result.has_next)
        self.assertFalse(result.has_previous)

    async def test_out_of_range_page(self):
        result = await self.query(page=99999)

        self.assertEqual(
            result.page,
            result.page_count - 1,
        )

        self.assertFalse(result.has_next)
        self.assertTrue(result.has_previous)

    async def test_invalid_parameters(self):
        invalid = [
            {"collection": "invalid"},
            {"category": "invalid"},
            {"sort": "invalid"},
            {"status": "invalid"},
            {"page": -1},
            {"page_size": 0},
            {"page_size": 21},
        ]

        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await self.query(**kwargs)

    async def test_deterministic_sorting(self):
        for sort in (
            "title", "updated", "added",
            "year", "rating",
        ):
            with self.subTest(sort=sort):
                first = await self.query(sort=sort)
                second = await self.query(sort=sort)

                self.assertEqual(first, second)

    async def test_default_page_size(self):
        result = await self.query()

        self.assertEqual(result.page_size, 8)
        self.assertEqual(len(result.entries), 8)
        self.assertTrue(result.has_next)


if __name__ == "__main__":
    unittest.main()
