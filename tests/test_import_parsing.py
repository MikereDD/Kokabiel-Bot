from __future__ import annotations

import tempfile
import unittest
import zipfile

from pathlib import Path

from imports.parse import parse_import


class ImportParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(
        self,
        name: str,
        text: str,
    ) -> Path:
        path = self.root / name
        path.write_text(
            text,
            encoding="utf-8",
        )
        return path

    def test_imdb_rating_normalizes(self) -> None:
        path = self.write(
            "ratings.csv",
            "Const,Your Rating,Date Rated,Title,URL,Title Type,Year\n"
            "tt0078748,10,2026-01-01,Alien,"
            "https://www.imdb.com/title/tt0078748/,movie,1979\n",
        )

        result = parse_import(path)
        record = result.records[0]

        self.assertEqual(result.source, "imdb")
        self.assertEqual(record.imdb_id, "tt0078748")
        self.assertEqual(record.media_type, "movie")
        self.assertTrue(record.watched)
        self.assertEqual(record.rating, 10.0)

    def test_imdb_watchlist_is_not_watched(self) -> None:
        path = self.write(
            "watchlist.csv",
            "Position,Const,Created,Title,URL,Title Type,Year\n"
            "1,tt1234567,2026-01-01,Example,"
            "https://www.imdb.com/title/tt1234567/,tvSeries,2020\n",
        )

        record = parse_import(path).records[0]

        self.assertEqual(record.media_type, "tv")
        self.assertTrue(record.watchlist)
        self.assertFalse(record.watched)
        self.assertEqual(
            record.status,
            "plan_to_watch",
        )

    def test_letterboxd_rating_converts_to_ten_point(self) -> None:
        path = self.write(
            "ratings.csv",
            "Date,Name,Year,Letterboxd URI,Rating\n"
            "2026-01-01,Alien,1979,https://boxd.it/2awY,4.5\n",
        )

        record = parse_import(path).records[0]

        self.assertTrue(record.watched)
        self.assertEqual(record.rating, 9.0)

    def test_letterboxd_diary_uses_watched_date(self) -> None:
        path = self.write(
            "diary.csv",
            "Date,Name,Year,Letterboxd URI,Rating,Watched Date\n"
            "2026-02-01,Alien,1979,https://boxd.it/2awY,5,2026-01-31\n",
        )

        record = parse_import(path).records[0]

        self.assertEqual(
            record.watched_at,
            "2026-01-31",
        )
        self.assertEqual(record.rating, 10.0)

    def test_letterboxd_zip_skips_deleted_content(self) -> None:
        path = self.root / "letterboxd.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "watched.csv",
                "Date,Name,Year,Letterboxd URI\n"
                "2026-01-01,Alien,1979,https://boxd.it/2awY\n",
            )
            archive.writestr(
                "ratings.csv",
                "Date,Name,Year,Letterboxd URI,Rating\n"
                "2026-01-01,Alien,1979,https://boxd.it/2awY,5\n",
            )
            archive.writestr(
                "deleted/watched.csv",
                "Date,Name,Year,Letterboxd URI\n"
                "2026-01-01,Deleted Film,2000,https://boxd.it/x\n",
            )

        result = parse_import(path)

        titles = {
            record.title
            for record in result.records
        }

        self.assertIn("Alien", titles)
        self.assertNotIn("Deleted Film", titles)

    def test_simkl_series_progress_normalizes(self) -> None:
        path = self.write(
            "simkl.csv",
            "Type,SIMKL_ID,TMDB_ID,IMDB_ID,TVDB_ID,Title,Year,"
            "LastEpWatched,Watchlist,WatchedDate,Rating\n"
            "tv,8650,1402,tt0944947,121361,Example Show,2011,"
            "S04E08,watching,21-09-2026,8\n",
        )

        record = parse_import(path).records[0]

        self.assertEqual(record.media_type, "tv")
        self.assertEqual(record.tmdb_id, 1402)
        self.assertEqual(record.season, 4)
        self.assertEqual(record.episode, 8)
        self.assertEqual(record.status, "watching")
        self.assertEqual(record.rating, 8.0)

    def test_trakt_movie_history_normalizes(self) -> None:
        path = self.write(
            "watched-history-1.json",
            """[
              {
                "watched_at": "2026-01-02T03:04:05.000Z",
                "movie": {
                  "title": "Alien",
                  "year": 1979,
                  "ids": {
                    "trakt": 123,
                    "imdb": "tt0078748",
                    "tmdb": 348
                  }
                }
              }
            ]""",
        )

        record = parse_import(path).records[0]

        self.assertEqual(record.media_type, "movie")
        self.assertEqual(record.tmdb_id, 348)
        self.assertTrue(record.watched)
        self.assertEqual(
            record.watched_at,
            "2026-01-02T03:04:05.000Z",
        )

    def test_trakt_episode_prefers_show_identity(self) -> None:
        path = self.write(
            "watched-history-episodes.json",
            """[
              {
                "watched_at": "2026-01-02T03:04:05.000Z",
                "show": {
                  "title": "Example Show",
                  "year": 2020,
                  "ids": {
                    "trakt": 100,
                    "tmdb": 200,
                    "tvdb": 300
                  }
                },
                "episode": {
                  "season": 3,
                  "number": 7,
                  "title": "Episode Seven",
                  "ids": {
                    "trakt": 999
                  }
                }
              }
            ]""",
        )

        record = parse_import(path).records[0]

        self.assertEqual(record.media_type, "episode")
        self.assertEqual(record.title, "Example Show")
        self.assertEqual(record.tmdb_id, 200)
        self.assertEqual(record.season, 3)
        self.assertEqual(record.episode, 7)

    def test_trakt_zip_preserves_ratings_and_watchlist(self) -> None:
        path = self.root / "trakt-export.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "ratings-movies.json",
                """[
                  {
                    "rated_at": "2026-01-01T00:00:00Z",
                    "rating": 9,
                    "movie": {
                      "title": "Alien",
                      "year": 1979,
                      "ids": {
                        "trakt": 123,
                        "tmdb": 348,
                        "imdb": "tt0078748"
                      }
                    }
                  }
                ]""",
            )
            archive.writestr(
                "lists-watchlist.json",
                """[
                  {
                    "show": {
                      "title": "Example Show",
                      "year": 2020,
                      "ids": {
                        "trakt": 555,
                        "tmdb": 666
                      }
                    }
                  }
                ]""",
            )

        result = parse_import(path)

        rating = next(
            record
            for record in result.records
            if record.rating is not None
        )

        watchlist = next(
            record
            for record in result.records
            if record.watchlist
        )

        self.assertEqual(rating.rating, 9.0)
        self.assertEqual(
            rating.rated_at,
            "2026-01-01T00:00:00Z",
        )

        self.assertEqual(
            watchlist.status,
            "plan_to_watch",
        )


if __name__ == "__main__":
    unittest.main()
