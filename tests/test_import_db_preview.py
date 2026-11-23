from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest

from pathlib import Path

from imports.db_preview import (
    ExistingPersonalState,
    ExistingRating,
    ExistingUserSnapshot,
    build_database_preview,
    load_existing_user_snapshot,
)
from imports.merge import (
    ImportPlanItem,
)
from imports.models import (
    NormalizedImportRecord,
)
from imports.resolve import (
    ResolutionCandidate,
    ResolutionPreview,
    ResolvedImportItem,
)


def resolved_item(
    record,
    *,
    media_type,
    tmdb_id,
    conflicts=(),
):
    plan_item = ImportPlanItem(
        key="test",
        identity_basis="test",
        proposed=record,
        source_record_count=1,
        source_files=(),
        conflicts=tuple(conflicts),
    )

    candidate = ResolutionCandidate(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=record.title or "Test",
        year=(
            str(record.year)
            if record.year is not None
            else "Unknown year"
        ),
    )

    return ResolvedImportItem(
        plan_item=plan_item,
        status="resolved",
        method="tmdb_id",
        resolved=candidate,
        candidates=(candidate,),
        reason="test",
    )


def preview(
    item,
    snapshot=None,
):
    if snapshot is None:
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={},
            episodes={},
        )

    return build_database_preview(
        ResolutionPreview(
            source=item.plan_item.proposed.source,
            raw_record_count=1,
            items=(item,),
        ),
        snapshot,
    )


class ImportDatabasePreviewTests(
    unittest.TestCase
):
    def test_new_state_reports_required_media_catalog_insert(self):
        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        item = result.items[0]

        self.assertEqual(
            item.media_action,
            "add",
        )
        self.assertEqual(
            result.media_add_count,
            1,
        )

    def test_existing_catalog_row_avoids_media_insert(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={},
            episodes={},
            media=frozenset(
                {
                    ("movie", 348),
                }
            ),
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                ),
                media_type="movie",
                tmdb_id=348,
            ),
            snapshot,
        )

        item = result.items[0]

        self.assertEqual(
            item.media_action,
            "none",
        )
        self.assertEqual(
            item.state_action,
            "add",
        )

    def test_new_watched_movie_adds_state(self):
        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        item = result.items[0]

        self.assertEqual(
            item.state_action,
            "add",
        )
        self.assertEqual(
            item.after_status,
            "watched",
        )
        self.assertFalse(
            item.after_watchlist
        )

    def test_existing_watched_movie_does_not_regress_to_watchlist(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={
                ("movie", 348):
                    ExistingPersonalState(
                        watchlist=False,
                        viewing_status="watched",
                        updated_at="2026-01-01",
                    )
            },
            ratings={},
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watchlist=True,
                    status="plan_to_watch",
                ),
                media_type="movie",
                tmdb_id=348,
            ),
            snapshot,
        )

        item = result.items[0]

        self.assertEqual(
            item.state_action,
            "none",
        )
        self.assertEqual(
            item.after_status,
            "watched",
        )
        self.assertFalse(
            item.after_watchlist
        )

    def test_completed_series_does_not_regress_to_watching(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={
                ("tv", 200):
                    ExistingPersonalState(
                        watchlist=False,
                        viewing_status="completed",
                        updated_at="2026-01-01",
                    )
            },
            ratings={},
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="simkl",
                    media_type="tv",
                    title="Example",
                    status="watching",
                ),
                media_type="tv",
                tmdb_id=200,
            ),
            snapshot,
        )

        item = result.items[0]

        self.assertEqual(
            item.state_action,
            "none",
        )
        self.assertEqual(
            item.after_status,
            "completed",
        )

    def test_caught_up_series_does_not_regress_to_watching(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={
                ("tv", 200):
                    ExistingPersonalState(
                        watchlist=False,
                        viewing_status="caught_up",
                        updated_at="2026-01-01",
                    )
            },
            ratings={},
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="simkl",
                    media_type="tv",
                    status="watching",
                ),
                media_type="tv",
                tmdb_id=200,
            ),
            snapshot,
        )

        self.assertEqual(
            result.items[0].after_status,
            "caught_up",
        )

    def test_completed_import_upgrades_watching_series(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={
                ("tv", 200):
                    ExistingPersonalState(
                        watchlist=True,
                        viewing_status="watching",
                        updated_at="2026-01-01",
                    )
            },
            ratings={},
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="simkl",
                    media_type="tv",
                    status="completed",
                ),
                media_type="tv",
                tmdb_id=200,
            ),
            snapshot,
        )

        item = result.items[0]

        self.assertEqual(
            item.state_action,
            "update",
        )
        self.assertEqual(
            item.after_status,
            "completed",
        )
        self.assertFalse(
            item.after_watchlist
        )

    def test_multiple_new_episodes_share_one_media_and_state_insert(self):
        first = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example",
                season=1,
                episode=1,
                watched=True,
            ),
            media_type="tv",
            tmdb_id=200,
        )

        second = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example",
                season=1,
                episode=2,
                watched=True,
            ),
            media_type="tv",
            tmdb_id=200,
        )

        result = build_database_preview(
            ResolutionPreview(
                source="trakt",
                raw_record_count=2,
                items=(
                    first,
                    second,
                ),
            ),
            ExistingUserSnapshot(
                user_id=1,
                states={},
                ratings={},
                episodes={},
                media=frozenset(),
            ),
        )

        first_plan = result.items[0]
        second_plan = result.items[1]

        self.assertEqual(
            first_plan.media_action,
            "add",
        )
        self.assertEqual(
            first_plan.state_action,
            "add",
        )
        self.assertEqual(
            first_plan.episode_additions,
            ((1, 1),),
        )

        self.assertEqual(
            second_plan.media_action,
            "none",
        )
        self.assertEqual(
            second_plan.state_action,
            "none",
        )
        self.assertEqual(
            second_plan.episode_additions,
            ((1, 2),),
        )

        self.assertEqual(
            result.media_add_count,
            1,
        )
        self.assertEqual(
            result.state_add_count,
            1,
        )
        self.assertEqual(
            result.episode_add_count,
            2,
        )

    def test_exact_episode_history_is_union_only(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={},
            episodes={
                200: frozenset(
                    {
                        (1, 1),
                    }
                )
            },
        )

        item = resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example",
                season=1,
                episode=2,
                watched=True,
            ),
            media_type="tv",
            tmdb_id=200,
        )

        result = preview(
            item,
            snapshot,
        )

        self.assertEqual(
            result.items[0].episode_additions,
            ((1, 2),),
        )

    def test_existing_episode_is_skipped(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={},
            episodes={
                200: frozenset(
                    {
                        (1, 2),
                    }
                )
            },
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="trakt",
                    media_type="episode",
                    season=1,
                    episode=2,
                    watched=True,
                ),
                media_type="tv",
                tmdb_id=200,
            ),
            snapshot,
        )

        self.assertEqual(
            result.items[0].episode_additions,
            (),
        )

    def test_simkl_last_episode_does_not_invent_history(self):
        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="simkl",
                    media_type="tv",
                    title="Example",
                    season=4,
                    episode=8,
                    status="watching",
                ),
                media_type="tv",
                tmdb_id=200,
            )
        )

        item = result.items[0]

        self.assertEqual(
            item.episode_additions,
            (),
        )

        codes = {
            notice.code
            for notice in item.notices
        }

        self.assertIn(
            "series_progress_hint_not_expanded",
            codes,
        )

    def test_new_rating_is_add(self):
        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        item = result.items[0]

        self.assertEqual(
            item.rating_action,
            "add",
        )
        self.assertEqual(
            item.proposed_rating,
            9,
        )

    def test_identical_rating_is_noop(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={
                ("movie", 348):
                    ExistingRating(
                        rating=9,
                        rated_at="2026-01-01",
                    )
            },
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            ),
            snapshot,
        )

        self.assertEqual(
            result.items[0].rating_action,
            "none",
        )

    def test_different_existing_rating_requires_review(self):
        snapshot = ExistingUserSnapshot(
            user_id=1,
            states={},
            ratings={
                ("movie", 348):
                    ExistingRating(
                        rating=8,
                        rated_at="2026-01-01",
                    )
            },
            episodes={},
        )

        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    rating=10.0,
                ),
                media_type="movie",
                tmdb_id=348,
            ),
            snapshot,
        )

        item = result.items[0]

        self.assertEqual(
            item.rating_action,
            "review",
        )
        self.assertTrue(
            item.needs_review
        )
        self.assertEqual(
            item.existing_rating,
            8,
        )

    def test_episode_rating_is_not_promoted_to_series_rating(self):
        result = preview(
            resolved_item(
                NormalizedImportRecord(
                    source="trakt",
                    media_type="episode",
                    season=1,
                    episode=2,
                    rating=10.0,
                ),
                media_type="tv",
                tmdb_id=200,
            )
        )

        item = result.items[0]

        self.assertEqual(
            item.rating_action,
            "none",
        )
        self.assertTrue(
            item.needs_review
        )

    def test_unresolved_identity_produces_no_db_actions(self):
        record = NormalizedImportRecord(
            source="letterboxd",
            media_type="movie",
            title="Unknown",
            year=2000,
        )

        plan_item = ImportPlanItem(
            key="test",
            identity_basis="title_year",
            proposed=record,
            source_record_count=1,
            source_files=(),
            conflicts=(),
        )

        unresolved = ResolvedImportItem(
            plan_item=plan_item,
            status="unresolved",
            method="title_year",
            resolved=None,
            candidates=(),
            reason="No match",
        )

        result = build_database_preview(
            ResolutionPreview(
                source="letterboxd",
                raw_record_count=1,
                items=(unresolved,),
            ),
            ExistingUserSnapshot(
                user_id=1,
                states={},
                ratings={},
                episodes={},
            ),
        )

        item = result.items[0]

        self.assertFalse(
            item.has_changes
        )
        self.assertTrue(
            item.needs_review
        )

    def test_canonical_duplicate_is_blocked(self):
        record = NormalizedImportRecord(
            source="trakt",
            media_type="movie",
            title="Alien",
            year=1979,
        )

        first = resolved_item(
            record,
            media_type="movie",
            tmdb_id=348,
        )

        second = resolved_item(
            record,
            media_type="movie",
            tmdb_id=348,
        )

        result = build_database_preview(
            ResolutionPreview(
                source="trakt",
                raw_record_count=2,
                items=(first, second),
            ),
            ExistingUserSnapshot(
                user_id=1,
                states={},
                ratings={},
                episodes={},
            ),
        )

        self.assertTrue(
            all(
                item.needs_review
                for item in result.items
            )
        )

        self.assertTrue(
            all(
                not item.has_changes
                for item in result.items
            )
        )


class SnapshotReadOnlyTests(
    unittest.TestCase
):
    def test_snapshot_reads_all_personal_state_without_modifying_db(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"

            connection = sqlite3.connect(
                path
            )

            connection.executescript(
                """
                CREATE TABLE media (
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    year TEXT NOT NULL,
                    PRIMARY KEY (
                        media_type,
                        tmdb_id
                    )
                );

                CREATE TABLE user_media_state (
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    watchlist INTEGER NOT NULL,
                    viewing_status TEXT NOT NULL,
                    last_chat_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        user_id,
                        media_type,
                        tmdb_id
                    )
                );

                CREATE TABLE ratings (
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    rated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        user_id,
                        media_type,
                        tmdb_id
                    )
                );

                CREATE TABLE episode_progress (
                    user_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    season_number INTEGER NOT NULL,
                    episode_number INTEGER NOT NULL,
                    watched_at TEXT NOT NULL,
                    PRIMARY KEY (
                        user_id,
                        tmdb_id,
                        season_number,
                        episode_number
                    )
                );

                INSERT INTO media
                VALUES (
                    'tv', 200,
                    'Example', '2020'
                );

                INSERT INTO user_media_state
                VALUES (
                    1, 'tv', 200, 'User',
                    0, 'caught_up', NULL,
                    '2026-01-01',
                    '2026-01-02'
                );

                INSERT INTO ratings
                VALUES (
                    1, 'tv', 200,
                    'User', 9,
                    '2026-01-03'
                );

                INSERT INTO episode_progress
                VALUES (
                    1, 200, 1, 1,
                    '2026-01-04'
                );
                """
            )

            connection.commit()
            connection.close()

            before = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()

            snapshot = (
                load_existing_user_snapshot(
                    path,
                    1,
                )
            )

            after = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()

            self.assertEqual(
                before,
                after,
            )

            self.assertIn(
                ("tv", 200),
                snapshot.media,
            )

            self.assertEqual(
                snapshot.states[
                    ("tv", 200)
                ].viewing_status,
                "caught_up",
            )

            self.assertEqual(
                snapshot.ratings[
                    ("tv", 200)
                ].rating,
                9,
            )

            self.assertEqual(
                snapshot.episodes[200],
                frozenset(
                    {
                        (1, 1),
                    }
                ),
            )


if __name__ == "__main__":
    unittest.main()
