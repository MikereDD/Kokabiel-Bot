from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import imports.db_apply as db_apply

from database import SCHEMA
from imports.db_apply import (
    ImportPreviewRejectedError,
    ImportPreviewStaleError,
    apply_database_import,
)
from imports.db_preview import (
    build_database_preview,
    load_existing_user_snapshot,
)
from imports.merge import ImportPlanItem
from imports.models import NormalizedImportRecord
from imports.resolve import (
    ResolutionCandidate,
    ResolutionPreview,
    ResolvedImportItem,
)


def resolved_item(
    record: NormalizedImportRecord,
    *,
    media_type: str,
    tmdb_id: int,
) -> ResolvedImportItem:
    plan_item = ImportPlanItem(
        key="test",
        identity_basis="test",
        proposed=record,
        source_record_count=1,
        source_files=(),
        conflicts=(),
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


def unresolved_item(
    record: NormalizedImportRecord,
) -> ResolvedImportItem:
    plan_item = ImportPlanItem(
        key="test",
        identity_basis="title_year",
        proposed=record,
        source_record_count=1,
        source_files=(),
        conflicts=(),
    )

    return ResolvedImportItem(
        plan_item=plan_item,
        status="unresolved",
        method="title_year",
        resolved=None,
        candidates=(),
        reason="No match",
    )


def make_resolution(
    *items: ResolvedImportItem,
) -> ResolutionPreview:
    assert items

    return ResolutionPreview(
        source=(
            items[0]
            .plan_item
            .proposed
            .source
        ),
        raw_record_count=len(items),
        items=tuple(items),
    )


def create_database(
    path: Path,
) -> None:
    connection = sqlite3.connect(
        path
    )

    try:
        connection.executescript(
            SCHEMA
        )
        connection.commit()

    finally:
        connection.close()


def make_preview(
    path: Path,
    resolution: ResolutionPreview,
    *,
    user_id: int = 1,
):
    snapshot = (
        load_existing_user_snapshot(
            path,
            user_id,
        )
    )

    return build_database_preview(
        resolution,
        snapshot,
    )


class ImportDatabaseApplyTests(
    unittest.TestCase
):
    def setUp(self):
        self.temp = (
            tempfile.TemporaryDirectory()
        )

        root = Path(
            self.temp.name
        )

        self.database = (
            root / "kokabiel.db"
        )

        self.backup = (
            root / "backup.db"
        )

        create_database(
            self.database
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_applies_new_movie_state_and_rating_with_preimport_backup(self):
        resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        approved = make_preview(
            self.database,
            resolution,
        )

        result = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Mike",
            chat_id=123,
        )

        self.assertFalse(
            result.already_applied
        )
        self.assertEqual(
            result.media_added,
            1,
        )
        self.assertEqual(
            result.states_added,
            1,
        )
        self.assertEqual(
            result.states_updated,
            0,
        )
        self.assertEqual(
            result.episodes_added,
            0,
        )
        self.assertEqual(
            result.ratings_added,
            1,
        )
        self.assertTrue(
            self.backup.is_file()
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            media = connection.execute(
                """
                SELECT
                    title,
                    year,
                    media_class
                FROM media
                WHERE media_type = 'movie'
                  AND tmdb_id = 348
                """
            ).fetchone()

            state = connection.execute(
                """
                SELECT
                    user_name,
                    watchlist,
                    viewing_status,
                    last_chat_id
                FROM user_media_state
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 348
                """
            ).fetchone()

            rating = connection.execute(
                """
                SELECT rating
                FROM ratings
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 348
                """
            ).fetchone()

        finally:
            connection.close()

        self.assertEqual(
            media,
            (
                "Alien",
                "1979",
                "film",
            ),
        )
        self.assertEqual(
            state,
            (
                "Mike",
                0,
                "watched",
                123,
            ),
        )
        self.assertEqual(
            rating,
            (9,),
        )

        backup_connection = (
            sqlite3.connect(
                self.backup
            )
        )

        try:
            backup_count = (
                backup_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    """
                ).fetchone()[0]
            )

        finally:
            backup_connection.close()

        self.assertEqual(
            backup_count,
            0,
        )

    def test_updates_existing_personal_state(self):
        connection = sqlite3.connect(
            self.database
        )

        try:
            connection.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year
                ) VALUES (
                    'movie',
                    348,
                    'Alien',
                    '1979'
                )
                """
            )

            connection.execute(
                """
                INSERT INTO user_media_state (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    watchlist,
                    viewing_status,
                    last_chat_id
                ) VALUES (
                    1,
                    'movie',
                    348,
                    'Old Name',
                    1,
                    'none',
                    10
                )
                """
            )

            connection.commit()

        finally:
            connection.close()

        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        self.assertEqual(
            approved.items[
                0
            ].state_action,
            "update",
        )

        result = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Mike",
            chat_id=123,
        )

        self.assertEqual(
            result.states_updated,
            1,
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            row = connection.execute(
                """
                SELECT
                    user_name,
                    watchlist,
                    viewing_status,
                    last_chat_id
                FROM user_media_state
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 348
                """
            ).fetchone()

        finally:
            connection.close()

        self.assertEqual(
            row,
            (
                "Mike",
                0,
                "watched",
                123,
            ),
        )

    def test_update_without_chat_id_preserves_existing_last_chat_id(self):
        connection = sqlite3.connect(
            self.database
        )

        try:
            connection.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year
                ) VALUES (
                    'movie',
                    348,
                    'Alien',
                    '1979'
                )
                """
            )

            connection.execute(
                """
                INSERT INTO user_media_state (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    watchlist,
                    viewing_status,
                    last_chat_id
                ) VALUES (
                    1,
                    'movie',
                    348,
                    'Old Name',
                    1,
                    'none',
                    777
                )
                """
            )

            connection.commit()

        finally:
            connection.close()

        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Mike",
            chat_id=None,
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            row = connection.execute(
                """
                SELECT
                    user_name,
                    last_chat_id,
                    viewing_status
                FROM user_media_state
                WHERE user_id = 1
                  AND media_type = 'movie'
                  AND tmdb_id = 348
                """
            ).fetchone()

        finally:
            connection.close()

        self.assertEqual(
            row,
            (
                "Mike",
                777,
                "watched",
            ),
        )

    def test_multiple_new_tv_episodes_apply_one_catalog_and_state_row(self):
        resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="trakt",
                    media_type="episode",
                    title="Example",
                    season=1,
                    episode=1,
                    watched=True,
                    watched_at=(
                        "2026-01-01T01:00:00Z"
                    ),
                ),
                media_type="tv",
                tmdb_id=200,
            ),
            resolved_item(
                NormalizedImportRecord(
                    source="trakt",
                    media_type="episode",
                    title="Example",
                    season=1,
                    episode=2,
                    watched=True,
                    watched_at=(
                        "2026-01-02T01:00:00Z"
                    ),
                ),
                media_type="tv",
                tmdb_id=200,
            ),
        )

        approved = make_preview(
            self.database,
            resolution,
        )

        result = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Mike",
            chat_id=123,
        )

        self.assertEqual(
            result.media_added,
            1,
        )
        self.assertEqual(
            result.states_added,
            1,
        )
        self.assertEqual(
            result.episodes_added,
            2,
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            media = connection.execute(
                """
                SELECT
                    media_class
                FROM media
                WHERE media_type = 'tv'
                  AND tmdb_id = 200
                """
            ).fetchone()

            state = connection.execute(
                """
                SELECT viewing_status
                FROM user_media_state
                WHERE user_id = 1
                  AND media_type = 'tv'
                  AND tmdb_id = 200
                """
            ).fetchone()

            episodes = (
                connection.execute(
                    """
                    SELECT
                        season_number,
                        episode_number,
                        watched_at
                    FROM episode_progress
                    WHERE user_id = 1
                      AND tmdb_id = 200
                    ORDER BY
                        season_number,
                        episode_number
                    """
                ).fetchall()
            )

        finally:
            connection.close()

        self.assertEqual(
            media,
            ("tv_series",),
        )
        self.assertEqual(
            state,
            ("watching",),
        )
        self.assertEqual(
            episodes,
            [
                (
                    1,
                    1,
                    "2026-01-01T01:00:00Z",
                ),
                (
                    1,
                    2,
                    "2026-01-02T01:00:00Z",
                ),
            ],
        )

    def test_database_change_during_backup_is_rejected(self):
        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        original_backup = (
            db_apply._create_verified_backup
        )

        def backup_then_change(
            database,
            backup,
        ):
            original_backup(
                database,
                backup,
            )

            writer = sqlite3.connect(
                self.database
            )

            try:
                writer.execute(
                    """
                    INSERT INTO media (
                        media_type,
                        tmdb_id,
                        title,
                        year
                    ) VALUES (
                        'movie',
                        999,
                        'Concurrent sentinel',
                        '2026'
                    )
                    """
                )

                writer.commit()

            finally:
                writer.close()

        with patch.object(
            db_apply,
            "_create_verified_backup",
            side_effect=backup_then_change,
        ):
            with self.assertRaises(
                ImportPreviewStaleError
            ):
                apply_database_import(
                    approved,
                    database_path=self.database,
                    backup_path=self.backup,
                    user_name="Mike",
                    chat_id=123,
                )

        self.assertFalse(
            self.backup.exists()
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            sentinel_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    WHERE media_type = 'movie'
                      AND tmdb_id = 999
                    """
                ).fetchone()[0]
            )

            import_state_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM user_media_state
                    WHERE user_id = 1
                      AND media_type = 'movie'
                      AND tmdb_id = 348
                    """
                ).fetchone()[0]
            )

        finally:
            connection.close()

        self.assertEqual(
            sentinel_count,
            1,
        )
        self.assertEqual(
            import_state_count,
            0,
        )

    def test_stale_preview_is_rejected_before_any_import_write(self):
        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            connection.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year
                ) VALUES (
                    'movie',
                    348,
                    'Alien',
                    '1979'
                )
                """
            )
            connection.commit()

        finally:
            connection.close()

        with self.assertRaises(
            ImportPreviewStaleError
        ):
            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=self.backup,
                user_name="Mike",
                chat_id=123,
            )

        connection = sqlite3.connect(
            self.database
        )

        try:
            state_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM user_media_state
                    """
                ).fetchone()[0]
            )

        finally:
            connection.close()

        self.assertEqual(
            state_count,
            0,
        )
        self.assertTrue(
            self.backup.is_file()
        )

    def test_review_required_preview_is_rejected_without_backup(self):
        resolution = make_resolution(
            unresolved_item(
                NormalizedImportRecord(
                    source="letterboxd",
                    media_type="movie",
                    title="Unknown",
                    year=2000,
                )
            )
        )

        approved = make_preview(
            self.database,
            resolution,
        )

        self.assertGreater(
            approved.review_count,
            0,
        )

        with self.assertRaises(
            ImportPreviewRejectedError
        ):
            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=self.backup,
                user_name="Mike",
            )

        self.assertFalse(
            self.backup.exists()
        )

    def test_failure_after_writes_rolls_back_entire_transaction(self):
        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        original = (
            db_apply._apply_item
        )

        def fail_after_write(
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

            raise RuntimeError(
                "synthetic failure"
            )

        with patch.object(
            db_apply,
            "_apply_item",
            side_effect=fail_after_write,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic failure",
            ):
                apply_database_import(
                    approved,
                    database_path=self.database,
                    backup_path=self.backup,
                    user_name="Mike",
                    chat_id=123,
                )

        connection = sqlite3.connect(
            self.database
        )

        try:
            media_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    """
                ).fetchone()[0]
            )

            state_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM user_media_state
                    """
                ).fetchone()[0]
            )

        finally:
            connection.close()

        self.assertEqual(
            media_count,
            0,
        )
        self.assertEqual(
            state_count,
            0,
        )
        self.assertTrue(
            self.backup.is_file()
        )

    def test_replay_of_approved_preview_is_safe_noop(self):
        resolution = make_resolution(
            resolved_item(
                NormalizedImportRecord(
                    source="imdb",
                    media_type="movie",
                    title="Alien",
                    year=1979,
                    watched=True,
                    rating=9.0,
                ),
                media_type="movie",
                tmdb_id=348,
            )
        )

        approved = make_preview(
            self.database,
            resolution,
        )

        first_backup = (
            self.backup
        )

        second_backup = (
            self.database.parent
            / "backup-second.db"
        )

        first = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=first_backup,
            user_name="Mike",
            chat_id=123,
        )

        second = apply_database_import(
            approved,
            database_path=self.database,
            backup_path=second_backup,
            user_name="Mike",
            chat_id=123,
        )

        self.assertFalse(
            first.already_applied
        )
        self.assertTrue(
            second.already_applied
        )

        self.assertEqual(
            second.media_added,
            0,
        )
        self.assertEqual(
            second.states_added,
            0,
        )
        self.assertEqual(
            second.states_updated,
            0,
        )
        self.assertEqual(
            second.episodes_added,
            0,
        )
        self.assertEqual(
            second.ratings_added,
            0,
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            media_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    """
                ).fetchone()[0]
            )

            state_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM user_media_state
                    """
                ).fetchone()[0]
            )

            rating_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM ratings
                    """
                ).fetchone()[0]
            )

        finally:
            connection.close()

        self.assertEqual(
            media_count,
            1,
        )
        self.assertEqual(
            state_count,
            1,
        )
        self.assertEqual(
            rating_count,
            1,
        )

    def test_backup_permissions_are_owner_only(self):
        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        apply_database_import(
            approved,
            database_path=self.database,
            backup_path=self.backup,
            user_name="Mike",
        )

        mode = stat.S_IMODE(
            self.backup.stat().st_mode
        )

        self.assertEqual(
            mode,
            0o600,
        )

    def test_existing_backup_path_is_never_overwritten(self):
        self.backup.write_text(
            "sentinel"
        )

        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        with self.assertRaises(
            FileExistsError
        ):
            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=self.backup,
                user_name="Mike",
            )

        self.assertEqual(
            self.backup.read_text(),
            "sentinel",
        )

        connection = sqlite3.connect(
            self.database
        )

        try:
            media_count = (
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    """
                ).fetchone()[0]
            )

        finally:
            connection.close()

        self.assertEqual(
            media_count,
            0,
        )

    def test_backup_captures_committed_wal_content_before_import(self):
        writer = sqlite3.connect(
            self.database
        )

        try:
            writer.execute(
                "PRAGMA journal_mode = WAL"
            )

            writer.execute(
                """
                INSERT INTO media (
                    media_type,
                    tmdb_id,
                    title,
                    year
                ) VALUES (
                    'movie',
                    999,
                    'Sentinel',
                    '2026'
                )
                """
            )

            writer.commit()

            resolution = make_resolution(
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

            approved = make_preview(
                self.database,
                resolution,
            )

            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=self.backup,
                user_name="Mike",
            )

        finally:
            writer.close()

        backup_connection = (
            sqlite3.connect(
                self.backup
            )
        )

        try:
            sentinel = (
                backup_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    WHERE media_type = 'movie'
                      AND tmdb_id = 999
                    """
                ).fetchone()[0]
            )

            imported = (
                backup_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media
                    WHERE media_type = 'movie'
                      AND tmdb_id = 348
                    """
                ).fetchone()[0]
            )

        finally:
            backup_connection.close()

        self.assertEqual(
            sentinel,
            1,
        )
        self.assertEqual(
            imported,
            0,
        )

    def test_blank_user_name_is_rejected_before_backup(self):
        resolution = make_resolution(
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

        approved = make_preview(
            self.database,
            resolution,
        )

        with self.assertRaises(
            ValueError
        ):
            apply_database_import(
                approved,
                database_path=self.database,
                backup_path=self.backup,
                user_name="   ",
            )

        self.assertFalse(
            self.backup.exists()
        )


if __name__ == "__main__":
    unittest.main()
