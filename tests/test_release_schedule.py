import unittest

from datetime import date

from handlers.library import (
    schedule_keyboard,
    schedule_text,
)
from release_intelligence import (
    CACHE_MAX_AGE,
    ReleaseCandidate,
    ReleaseSnapshot,
    ScheduledRelease,
    personal_schedule_page,
)


class ReleaseScheduleTests(unittest.TestCase):

    def setUp(self):
        self.today = date(2027, 2, 1)
        self.now = 1800000000

    def candidate(
        self,
        i,
        dates=(),
        *,
        kind="movie",
        stale=False,
        unchecked=False,
    ):
        return ReleaseCandidate(
            media_type=kind,
            tmdb_id=i,
            title=f"Title <{i}>",
            media_class=(
                "film" if kind == "movie" else "tv_series"
            ),
            checked_at=(
                None if unchecked
                else self.now - CACHE_MAX_AGE
                if stale else self.now
            ),
            snapshot=(
                None if unchecked
                else ReleaseSnapshot(
                    "Continuing" if kind == "tv" else "Released",
                    tuple(
                        ScheduledRelease(
                            date=value,
                            label=(
                                "Episode"
                                if kind == "tv"
                                else "US Digital"
                            ),
                            season=2 if kind == "tv" else None,
                            episode=3 if kind == "tv" else None,
                        )
                        for value in dates
                    ),
                )
            ),
        )

    def schedule(self, candidates=(), **kwargs):
        return personal_schedule_page(
            tuple(candidates),
            today=self.today,
            now=self.now,
            **kwargs,
        )

    def test_chronological_order(self):
        result = self.schedule([
            self.candidate(1, ["2027-02-20"]),
            self.candidate(2, ["2027-02-03"], kind="tv"),
            self.candidate(3, ["2027-02-10"]),
        ])

        self.assertEqual(
            [item.candidate.tmdb_id for item in result.entries],
            [2, 3, 1],
        )

    def test_date_window_boundaries(self):
        candidate = self.candidate(1, [
            "2027-01-31",
            "2027-02-01",
            "2027-02-07",
            "2027-02-08",
            "2027-03-02",
            "2027-03-03",
        ])

        week = self.schedule([candidate], days=7)
        month = self.schedule([candidate], days=30)

        self.assertEqual(
            [item.event.date for item in week.entries],
            ["2027-02-01", "2027-02-07"],
        )

        self.assertEqual(month.total, 4)
        self.assertEqual(month.end, "2027-03-02")

    def test_coverage_counts(self):
        result = self.schedule([
            self.candidate(1),
            self.candidate(2, ["2027-02-03"], stale=True),
            self.candidate(3, unchecked=True),
        ])

        self.assertEqual(result.tracked, 3)
        self.assertEqual(result.fresh, 1)
        self.assertEqual(result.stale, 1)
        self.assertEqual(result.unchecked, 1)
        self.assertTrue(result.entries[0].stale)

    def test_complete_pagination(self):
        candidates = [
            self.candidate(i, ["2027-02-10"])
            for i in range(1, 26)
        ]

        seen = []

        for page in range(4):
            result = self.schedule(candidates, page=page)

            self.assertEqual(result.total, 25)
            self.assertEqual(result.page_count, 4)

            seen.extend(
                item.candidate.tmdb_id
                for item in result.entries
            )

        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25)

        last = self.schedule(candidates, page=999)

        self.assertEqual(last.page, 3)
        self.assertEqual(len(last.entries), 1)

    def test_empty_and_incomplete_schedule(self):
        empty = self.schedule()

        self.assertEqual(empty.page_count, 1)
        self.assertEqual(empty.total, 0)

        partial = self.schedule([
            self.candidate(1, unchecked=True),
        ])

        self.assertIn(
            "Coverage is incomplete",
            schedule_text("Tester", "US", partial),
        )

    def test_html_and_episode_information(self):
        result = self.schedule([
            self.candidate(
                1, ["2027-02-03"],
                kind="tv", stale=True,
            ),
        ])

        text = schedule_text("A & B", "US", result)

        self.assertIn("A &amp; B", text)
        self.assertIn("Title &lt;1&gt;", text)
        self.assertIn("S02E03", text)
        self.assertIn("needs refresh", text)
        self.assertLessEqual(len(text), 4096)

    def test_keyboard_callback_budget(self):
        result = self.schedule([
            self.candidate(i, ["2027-02-03"])
            for i in range(20)
        ])

        markup = schedule_keyboard(
            "a" * 24, 30, result,
        )

        data = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

        self.assertTrue(all(
            len(value.encode("utf-8")) <= 64
            for value in data
        ))

        self.assertIn(
            f"lib|{'a' * 24}|schedule_back|-",
            data,
        )

    def test_invalid_window_and_page(self):
        for kwargs in (
            {"days": 0},
            {"days": 365},
            {"page": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.schedule(**kwargs)


if __name__ == "__main__":
    unittest.main()
