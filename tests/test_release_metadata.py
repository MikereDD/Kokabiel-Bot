from __future__ import annotations

import unittest

from tmdb import TmdbClient


class ReleaseMetadataTests(unittest.TestCase):

    def setUp(self):
        self.client = TmdbClient(
            None,
            "synthetic-test-token",
            region="US",
        )

    def movie(self, **kwargs):
        return self.client._parse_details(
            "movie",
            {
                "id": 100,
                "title": "Example Film",
                **kwargs,
            },
        )

    def series(self, **kwargs):
        return self.client._parse_details(
            "tv",
            {
                "id": 200,
                "name": "Example Series",
                **kwargs,
            },
        )

    def test_movie_original_release_date(self):
        details = self.movie(
            release_date="2026-12-18",
            status="Released",
        )

        self.assertEqual(
            details.release_date,
            "2026-12-18",
        )

        self.assertEqual(details.release_region, "US")
        self.assertEqual(details.lifecycle_label, "Released")

    def test_regional_release_events(self):
        details = self.movie(
            release_dates={
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {
                                "type": 3,
                                "release_date": (
                                    "2026-12-18T00:00:00.000Z"
                                ),
                            },
                            {
                                "type": 4,
                                "release_date": (
                                    "2027-01-15T00:00:00.000Z"
                                ),
                            },
                            {
                                "type": 5,
                                "release_date": (
                                    "2027-02-16T00:00:00.000Z"
                                ),
                            },
                            {
                                "type": 3,
                                "release_date": (
                                    "2026-12-18T00:00:00.000Z"
                                ),
                            },
                        ],
                    },
                    {
                        "iso_3166_1": "GB",
                        "release_dates": [
                            {
                                "type": 3,
                                "release_date": "2026-12-01",
                            }
                        ],
                    },
                ]
            },
        )

        self.assertEqual(
            [
                (item.date, item.label, item.region)
                for item in details.release_events
            ],
            [
                ("2026-12-18", "Theatrical", "US"),
                ("2027-01-15", "Digital", "US"),
                ("2027-02-16", "Physical media", "US"),
            ],
        )

    def test_invalid_release_dates(self):
        for value in (
            None, "", "2026", "2026-02-30",
            "unknown", 2026,
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    self.movie(
                        release_date=value
                    ).release_date
                )

    def test_missing_release_information(self):
        details = self.movie()

        self.assertIsNone(details.release_date)
        self.assertEqual(details.release_events, ())

    def test_documentary_release_date(self):
        details = self.movie(
            release_date="2026-12-01",
            genres=[
                {"id": 99, "name": "Documentary"}
            ],
        )

        self.assertEqual(
            details.media_class,
            "documentary_film",
        )

        self.assertEqual(
            details.release_date,
            "2026-12-01",
        )

    def test_upcoming_episode(self):
        details = self.series(
            status="Returning Series",
            first_air_date="2024-01-01",
            next_episode_to_air={
                "season_number": 3,
                "episode_number": 2,
                "name": "The Next Chapter",
                "air_date": "2026-12-10",
                "runtime": 48,
            },
        )

        episode = details.next_episode

        self.assertIsNotNone(episode)
        self.assertEqual(episode.season_number, 3)
        self.assertEqual(episode.episode_number, 2)
        self.assertEqual(episode.air_date, "2026-12-10")
        self.assertEqual(episode.runtime, 48)
        self.assertEqual(
            details.lifecycle_label,
            "Continuing",
        )

    def test_distinct_lifecycle_statuses(self):
        cases = (
            ("Returning Series", "Continuing", "ongoing"),
            ("Ended", "Ended", "ended"),
            ("Canceled", "Canceled", "ended"),
            ("Cancelled", "Canceled", "ended"),
            ("Planned", "Planned", "ongoing"),
            ("In Production", "In production", "ongoing"),
            ("Pilot", "Pilot", "ongoing"),
            (None, "Unknown", "unknown"),
        )

        for raw, label, legacy in cases:
            with self.subTest(status=raw):
                details = self.series(status=raw)

                self.assertEqual(
                    details.lifecycle_label,
                    label,
                )

                self.assertEqual(
                    details.series_status,
                    legacy,
                )

    def test_missing_episode_does_not_imply_ended(self):
        details = self.series(
            status="Returning Series",
            next_episode_to_air=None,
        )

        self.assertIsNone(details.next_episode)
        self.assertEqual(
            details.lifecycle_label,
            "Continuing",
        )

    def test_unknown_episode_air_date(self):
        details = self.series(
            next_episode_to_air={
                "season_number": 2,
                "episode_number": 1,
                "name": "Season Premiere",
                "air_date": None,
            }
        )

        self.assertIsNotNone(details.next_episode)
        self.assertIsNone(details.next_episode.air_date)

    def test_invalid_episode_coordinates(self):
        details = self.series(
            next_episode_to_air={
                "season_number": -1,
                "episode_number": 0,
            }
        )

        self.assertIsNone(details.next_episode)


    def test_movie_release_information(self):
        from datetime import date
        from formatting import release_information_text

        details = self.movie(
            release_date="2026-12-18",
            release_dates={
                "results": [{
                    "iso_3166_1": "US",
                    "release_dates": [
                        {
                            "type": 3,
                            "release_date": "2026-12-18",
                        },
                        {
                            "type": 4,
                            "release_date": "2027-01-15",
                        },
                        {
                            "type": 5,
                            "release_date": "2027-02-16",
                        },
                    ],
                }]
            },
        )

        text = release_information_text(
            details,
            today=date(2026, 11, 25),
        )

        self.assertIn("US Theatrical:</b> Dec 18, 2026", text)
        self.assertIn("US Digital:</b> Jan 15, 2027", text)
        self.assertIn("US Physical media:</b> Feb 16, 2027", text)

    def test_series_release_information(self):
        from datetime import date
        from formatting import release_information_text

        details = self.series(
            status="Returning Series",
            first_air_date="2024-01-01",
            next_episode_to_air={
                "season_number": 3,
                "episode_number": 1,
                "name": "New & Returning",
                "air_date": "2026-12-10",
            },
            seasons=[{
                "season_number": 3,
                "episode_count": 8,
                "air_date": "2026-12-10",
            }],
        )

        text = release_information_text(
            details,
            today=date(2026, 11, 25),
        )

        self.assertIn("Continuing", text)
        self.assertIn("S03E01", text)
        self.assertIn("New &amp; Returning", text)
        self.assertIn("Season 3 premiere", text)
        self.assertIn("Dec 10, 2026", text)

    def test_ended_and_stale_episode_information(self):
        from datetime import date
        from formatting import release_information_text

        episode = {
            "season_number": 2,
            "episode_number": 1,
            "air_date": "2025-01-01",
        }

        for status in ("Ended", "Canceled", "Returning Series"):
            with self.subTest(status=status):
                details = self.series(
                    status=status,
                    next_episode_to_air=episode,
                )

                text = release_information_text(
                    details,
                    today=date(2026, 11, 25),
                )

                self.assertNotIn("S02E01", text)

                if status == "Returning Series":
                    self.assertIn("Continuing", text)
                    self.assertIn(
                        "No upcoming air date listed",
                        text,
                    )
                else:
                    self.assertIn(status, text)

    def test_media_caption_budget(self):
        from formatting import (
            media_caption,
            TELEGRAM_PHOTO_CAPTION_LIMIT,
        )

        details = self.movie(
            title="<&>" * 300,
            overview="<&>" * 2000,
            release_date="2026-12-18",
        )

        caption = media_caption(details)

        self.assertLessEqual(
            len(caption),
            TELEGRAM_PHOTO_CAPTION_LIMIT,
        )

        self.assertIn("Release date", caption)
        self.assertIn("Dec 18, 2026", caption)
        self.assertIn("Media data supplied by TMDB", caption)



if __name__ == "__main__":
    unittest.main()
