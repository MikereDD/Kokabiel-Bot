from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
import zipfile

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import imports.db_apply as db_apply

from imports.db_apply import (
    ImportVerificationError,
    apply_database_import,
)
from imports.db_preview import (
    load_existing_user_snapshot,
)
from imports.merge import analyze_import
from imports.models import NormalizedImportRecord
from imports.resolve import resolve_import_plan
from imports.selective import build_selective_import_preview
from models import SearchResult

from tests.test_import_db_apply import (
    create_database,
    make_preview,
    make_resolution,
    resolved_item,
    unresolved_item,
)


class FakeTmdb:
    async def lookup_result(self, media_type, tmdb_id):
        return SearchResult(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title="Example Show",
            original_title="Example Show",
            year="2020",
            overview="",
            poster_path=None,
        )

    async def find_by_imdb(self, *args, **kwargs):
        raise AssertionError(
            "Unexpected fallback identity lookup"
        )

    find_by_tvdb = find_by_imdb
    search_movie = find_by_imdb
    search_tv = find_by_imdb
    search_multi = find_by_imdb


class TransactionFinalStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.database = self.root / "kokabiel.db"
        self.backup = self.root / "backup.db"

        create_database(self.database)

    def snapshot(self, path=None):
        return load_existing_user_snapshot(
            path or self.database,
            1,
        )

    def parent(self, status):
        return resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="tv",
                title="Example Show",
                year=2020,
                tmdb_id=200,
                status=status,
                watchlist=(status == "plan_to_watch"),
                watched=(status == "completed"),
            ),
            media_type="tv",
            tmdb_id=200,
        )

    def episode(self):
        return resolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="episode",
                title="Example Show",
                year=2020,
                tmdb_id=200,
                season=1,
                episode=1,
                watched=True,
            ),
            media_type="tv",
            tmdb_id=200,
        )

    def preview(self, *items):
        return make_preview(
            self.database,
            make_resolution(*items),
        )

    def native_preview(self, path):
        plan = analyze_import(
            str(path),
            source_name=path.name,
        )

        resolution = asyncio.run(
            resolve_import_plan(
                plan,
                FakeTmdb(),
            )
        )

        self.assertEqual(
            resolution.resolved_count,
            2,
        )

        return build_selective_import_preview(
            resolution,
            self.snapshot(),
        ).full

    def assert_applies_and_replays(
        self,
        approved,
        expected_status,
    ):
        self.assertEqual(
            approved.review_count,
            0,
        )

        self.assertTrue(
            any(
                item.has_changes
                for item in approved.items
            )
        )

        before = self.snapshot()

        result = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Synthetic Tester",
            chat_id=123,
        )

        self.assertFalse(result.already_applied)

        self.assertEqual(
            result.media_added,
            approved.media_add_count,
        )

        self.assertEqual(
            result.states_added,
            approved.state_add_count,
        )

        self.assertEqual(
            result.states_updated,
            approved.state_update_count,
        )

        self.assertEqual(
            result.episodes_added,
            approved.episode_add_count,
        )

        self.assertEqual(
            result.ratings_added,
            approved.rating_add_count,
        )

        self.assertTrue(self.backup.is_file())

        self.assertEqual(
            self.snapshot(self.backup),
            before,
        )

        after = self.snapshot()

        self.assertEqual(
            after.media,
            frozenset({("tv", 200)}),
        )

        self.assertEqual(
            set(after.states),
            {("tv", 200)},
        )

        state = after.states[("tv", 200)]

        self.assertFalse(state.watchlist)

        self.assertEqual(
            state.viewing_status,
            expected_status,
        )

        self.assertEqual(
            after.episodes,
            {200: frozenset({(1, 1)})},
        )

        self.assertEqual(after.ratings, {})

        with closing(
            sqlite3.connect(self.database)
        ) as connection:
            self.assertEqual(
                connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone(),
                ("ok",),
            )

            self.assertEqual(
                connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(),
                [],
            )

        replay = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.root / "replay.db",
            user_name="Synthetic Tester",
            chat_id=123,
        )

        self.assertTrue(replay.already_applied)

        self.assertEqual(
            (
                replay.media_added,
                replay.states_added,
                replay.states_updated,
                replay.episodes_added,
                replay.ratings_added,
            ),
            (0, 0, 0, 0, 0),
        )

        self.assertEqual(
            self.snapshot(),
            after,
        )

    def test_native_trakt_watchlist_and_watched_episode(self):
        show = {
            "title": "Example Show",
            "year": 2020,
            "ids": {
                "trakt": 5,
                "tmdb": 200,
            },
        }

        upload = self.root / "trakt-export.zip"

        with zipfile.ZipFile(
            upload,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "lists-watchlist.json",
                json.dumps([
                    {
                        "type": "show",
                        "show": show,
                    }
                ]),
            )

            archive.writestr(
                "watched-history.json",
                json.dumps([
                    {
                        "type": "episode",
                        "watched_at": (
                            "2026-01-01T00:00:00.000Z"
                        ),
                        "episode": {
                            "season": 1,
                            "number": 1,
                            "title": "Pilot",
                            "ids": {"trakt": 77},
                        },
                        "show": show,
                    }
                ]),
            )

        approved = self.native_preview(upload)

        self.assertEqual(approved.source, "trakt")

        self.assert_applies_and_replays(
            approved,
            "watching",
        )

    def test_native_simkl_watchlist_and_watched_episode(self):
        export = {
            "shows": [
                {
                    "status": "plantowatch",
                    "show": {
                        "title": "Example Show",
                        "year": 2020,
                        "ids": {
                            "simkl": 9,
                            "tmdb": 200,
                        },
                    },
                    "seasons": [
                        {
                            "number": 1,
                            "episodes": [
                                {
                                    "number": 1,
                                    "watched_at": (
                                        "2026-01-01T00:00:00Z"
                                    ),
                                }
                            ],
                        }
                    ],
                }
            ],
            "anime": [],
            "movies": [],
        }

        upload = self.root / "SimklBackup.json"

        upload.write_text(
            json.dumps(export),
            encoding="utf-8",
        )

        approved = self.native_preview(upload)

        self.assertEqual(approved.source, "simkl")

        self.assert_applies_and_replays(
            approved,
            "watching",
        )

    def test_episode_before_completed_parent(self):
        approved = self.preview(
            self.episode(),
            self.parent("completed"),
        )

        self.assertEqual(
            approved.state_add_count,
            1,
        )

        self.assertEqual(
            approved.state_update_count,
            1,
        )

        self.assert_applies_and_replays(
            approved,
            "completed",
        )

    def test_completed_parent_before_episode(self):
        approved = self.preview(
            self.parent("completed"),
            self.episode(),
        )

        self.assertEqual(
            approved.state_update_count,
            0,
        )

        self.assert_applies_and_replays(
            approved,
            "completed",
        )

    def test_episode_before_watchlisted_parent(self):
        approved = self.preview(
            self.episode(),
            self.parent("plan_to_watch"),
        )

        self.assert_applies_and_replays(
            approved,
            "watching",
        )

    def test_selective_subset_with_parent_state_transition(self):
        blocked = unresolved_item(
            NormalizedImportRecord(
                source="trakt",
                media_type="movie",
                title="Unresolved Synthetic Film",
                year=2021,
                watched=True,
            )
        )

        resolution = make_resolution(
            self.parent("plan_to_watch"),
            self.episode(),
            blocked,
        )

        selective = build_selective_import_preview(
            resolution,
            self.snapshot(),
        )

        self.assertEqual(
            selective.full.review_count,
            1,
        )

        self.assertEqual(
            selective.eligible.review_count,
            0,
        )

        self.assertEqual(
            selective.eligible.raw_record_count,
            2,
        )

        self.assertEqual(
            selective.excluded_raw_record_count,
            1,
        )

        self.assert_applies_and_replays(
            selective.eligible,
            "watching",
        )

    def assert_corruption_rolls_back(
        self,
        mutation,
    ):
        approved = self.preview(
            self.parent("completed"),
            self.episode(),
        )

        before = self.snapshot()
        original = db_apply._apply_item

        def corrupted_apply(
            connection,
            user_id,
            user_name,
            chat_id,
            item,
            counts,
        ):
            original(
                connection,
                user_id,
                user_name,
                chat_id,
                item,
                counts,
            )

            if item.episode_additions:
                mutation(connection)

        with patch.object(
            db_apply,
            "_apply_item",
            side_effect=corrupted_apply,
        ):
            with self.assertRaises(
                ImportVerificationError
            ):
                apply_database_import(
                    approved,
                    database_path=self.database,
                    backup_path=self.backup,
                    user_name="Synthetic Tester",
                )

        self.assertEqual(
            self.snapshot(),
            before,
        )

        self.assertTrue(self.backup.is_file())

        self.assertEqual(
            self.snapshot(self.backup),
            before,
        )

    def test_verification_rejects_missing_approved_episode(self):
        def remove_episode(connection):
            connection.execute(
                """
                DELETE FROM episode_progress
                WHERE user_id = 1
                  AND tmdb_id = 200
                  AND season_number = 1
                  AND episode_number = 1
                """
            )

        self.assert_corruption_rolls_back(
            remove_episode
        )

    def test_verification_rejects_unapproved_extra_episode(self):
        def add_extra_episode(connection):
            connection.execute(
                """
                INSERT INTO episode_progress (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number
                ) VALUES (1, 200, 1, 99)
                """
            )

        self.assert_corruption_rolls_back(
            add_extra_episode
        )


    def seed_existing_history_and_rating(self):
        approved = self.preview(
            self.parent("completed")
        )

        apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.root / "seed-backup.db",
            user_name="Synthetic Tester",
        )

        with closing(
            sqlite3.connect(self.database)
        ) as connection:
            connection.execute(
                """
                INSERT INTO episode_progress (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number
                ) VALUES (1, 200, 1, 2)
                """
            )

            connection.execute(
                """
                INSERT INTO ratings (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    rating
                ) VALUES (
                    1, 'tv', 200,
                    'Synthetic Tester', 8
                )
                """
            )

            connection.commit()

    def test_verification_rejects_unexpected_state_change(self):
        def change_state(connection):
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = 'watching'
                WHERE user_id = 1
                  AND media_type = 'tv'
                  AND tmdb_id = 200
                """
            )

        self.assert_corruption_rolls_back(
            change_state
        )

    def test_verification_rejects_unapproved_rating(self):
        def add_rating(connection):
            connection.execute(
                """
                INSERT INTO ratings (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    rating
                ) VALUES (
                    1, 'tv', 200,
                    'Synthetic Tester', 10
                )
                """
            )

        self.assert_corruption_rolls_back(
            add_rating
        )

    def test_verification_rejects_unapproved_catalog_row(self):
        def add_media(connection):
            connection.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year,
                    poster_url,
                    media_class,
                    series_status,
                    season_count,
                    episode_count
                ) VALUES (
                    'movie', 999999,
                    'Unapproved Synthetic Film',
                    2020, NULL, 'film',
                    'unknown', 0, 0
                )
                """
            )

        self.assert_corruption_rolls_back(
            add_media
        )

    def test_verification_preserves_existing_rating(self):
        self.seed_existing_history_and_rating()

        def overwrite_rating(connection):
            connection.execute(
                """
                UPDATE ratings
                SET rating = 10
                WHERE user_id = 1
                  AND media_type = 'tv'
                  AND tmdb_id = 200
                """
            )

        self.assert_corruption_rolls_back(
            overwrite_rating
        )

    def test_verification_preserves_existing_episode_history(self):
        self.seed_existing_history_and_rating()

        def delete_existing_episode(connection):
            connection.execute(
                """
                DELETE FROM episode_progress
                WHERE user_id = 1
                  AND tmdb_id = 200
                  AND season_number = 1
                  AND episode_number = 2
                """
            )

        self.assert_corruption_rolls_back(
            delete_existing_episode
        )

    def test_replay_rejects_subsequent_status_change(self):
        approved = self.preview(
            self.parent("plan_to_watch"),
            self.episode(),
        )

        self.assert_applies_and_replays(
            approved,
            "watching",
        )

        with closing(
            sqlite3.connect(self.database)
        ) as connection:
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = 'completed'
                WHERE user_id = 1
                  AND media_type = 'tv'
                  AND tmdb_id = 200
                """
            )

            connection.commit()

        before = self.snapshot()

        with self.assertRaises(
            db_apply.ImportPreviewStaleError
        ):
            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=(
                    self.root / "stale-replay.db"
                ),
                user_name="Synthetic Tester",
            )

        self.assertEqual(
            self.snapshot(),
            before,
        )

        self.assertEqual(
            self.snapshot()
            .states[("tv", 200)]
            .viewing_status,
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
