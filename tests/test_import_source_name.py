from __future__ import annotations

import tempfile
import unittest

from pathlib import Path

from imports.detect import (
    ImportDetectionError,
    detect_import_source,
)
from imports.merge import analyze_import
from imports.parse import parse_import


TRAKT_HISTORY = """[
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
]"""


class ImportSourceNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_trakt_detection_survives_opaque_quarantine_name(self) -> None:
        path = self.root / "deadbeef.json"
        path.write_text(TRAKT_HISTORY, encoding="utf-8")

        detected = detect_import_source(
            path,
            source_name="watched-history-1.json",
        )

        self.assertEqual(detected.source, "trakt")

    def test_trakt_history_semantics_survive_opaque_quarantine_name(self) -> None:
        path = self.root / "deadbeef.json"
        path.write_text(TRAKT_HISTORY, encoding="utf-8")

        parsed = parse_import(
            path,
            source_name="watched-history-1.json",
        )

        self.assertEqual(parsed.source, "trakt")
        self.assertEqual(parsed.source_files, ("watched-history-1.json",))

        record = parsed.records[0]

        self.assertTrue(record.watched)
        self.assertEqual(
            record.watched_at,
            "2026-01-02T03:04:05.000Z",
        )
        self.assertEqual(
            record.source_file,
            "watched-history-1.json",
        )

    def test_analyze_import_preserves_source_name(self) -> None:
        path = self.root / "deadbeef.json"
        path.write_text(TRAKT_HISTORY, encoding="utf-8")

        plan = analyze_import(
            str(path),
            source_name="watched-history-1.json",
        )

        self.assertEqual(plan.source, "trakt")
        self.assertEqual(plan.raw_record_count, 1)
        self.assertTrue(plan.items[0].proposed.watched)

    def test_fake_trakt_filename_does_not_make_arbitrary_json_trakt(self) -> None:
        path = self.root / "deadbeef.json"
        path.write_text(
            '{"hello": "world"}',
            encoding="utf-8",
        )

        with self.assertRaises(ImportDetectionError):
            detect_import_source(
                path,
                source_name="watched-history-1.json",
            )

    def test_letterboxd_watchlist_semantics_survive_opaque_name(self) -> None:
        path = self.root / "deadbeef.csv"
        path.write_text(
            "Date,Name,Year,Letterboxd URI\n"
            "2026-01-01,Alien,1979,https://boxd.it/2awY\n",
            encoding="utf-8",
        )

        parsed = parse_import(
            path,
            source_name="watchlist.csv",
        )

        record = parsed.records[0]

        self.assertFalse(record.watched)
        self.assertTrue(record.watchlist)
        self.assertEqual(record.status, "plan_to_watch")
        self.assertEqual(record.source_file, "watchlist.csv")


if __name__ == "__main__":
    unittest.main()
