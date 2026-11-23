from __future__ import annotations

import tempfile
import unittest
import zipfile

from pathlib import Path

from imports.detect import (
    ImportDetectionError,
    detect_import_source,
)


class ImportDetectionTests(unittest.TestCase):
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

    def test_detects_imdb_ratings_csv(self) -> None:
        path = self.write(
            "ratings.csv",
            "Const,Your Rating,Date Rated,Title,URL,Title Type,Year\n"
            "tt0078748,10,2026-01-01,Alien,"
            "https://www.imdb.com/title/tt0078748/,movie,1979\n",
        )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "imdb",
        )

    def test_detects_imdb_list_csv(self) -> None:
        path = self.write(
            "watchlist.csv",
            "Position,Const,Created,Title,URL,Title Type,Year\n"
            "1,tt0078748,2026-01-01,Alien,"
            "https://www.imdb.com/title/tt0078748/,movie,1979\n",
        )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "imdb",
        )

    def test_detects_letterboxd_csv(self) -> None:
        path = self.write(
            "watched.csv",
            "Date,Name,Year,Letterboxd URI\n"
            "2026-01-01,Alien,1979,https://boxd.it/2awY\n",
        )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "letterboxd",
        )

    def test_detects_simkl_csv(self) -> None:
        path = self.write(
            "simkl.csv",
            "Type,SIMKL_ID,TMDB_ID,IMDB_ID,Title,Year,"
            "LastEpWatched,Watchlist,WatchedDate,Rating\n"
            "movie,123,348,tt0078748,Alien,1979,,completed,"
            "01/01/2026,10\n",
        )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "simkl",
        )

    def test_detects_trakt_zip(self) -> None:
        path = self.root / "trakt-export-user.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "watched-history-1.json",
                '[{"id":1}]',
            )
            archive.writestr(
                "ratings-movies.json",
                '[{"rating":10}]',
            )
            archive.writestr(
                "lists-watchlist.json",
                '[]',
            )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "trakt",
        )

    def test_detects_trakt_json_member(self) -> None:
        path = self.write(
            "watched-history-1.json",
            (
                '[{"movie":{"title":"Alien","year":1979,'
                '"ids":{"tmdb":348}}}]'
            ),
        )

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "trakt",
        )

    def test_detects_letterboxd_zip(self) -> None:
        path = self.root / "letterboxd-export.zip"

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

        result = detect_import_source(path)

        self.assertEqual(
            result.source,
            "letterboxd",
        )

    def test_unknown_csv_is_rejected(self) -> None:
        path = self.write(
            "unknown.csv",
            "foo,bar\n1,2\n",
        )

        with self.assertRaises(
            ImportDetectionError
        ):
            detect_import_source(path)

    def test_unknown_zip_is_rejected(self) -> None:
        path = self.root / "unknown.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "data.csv",
                "foo,bar\n1,2\n",
            )

        with self.assertRaises(
            ImportDetectionError
        ):
            detect_import_source(path)


if __name__ == "__main__":
    unittest.main()
