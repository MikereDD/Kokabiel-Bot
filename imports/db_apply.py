from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from pathlib import Path

from imports.db_preview import (
    ImportDatabasePreview,
    ImportDbPreviewItem,
    build_database_preview,
    snapshot_from_connection,
)
from imports.resolve import ResolutionPreview


class ImportApplyError(RuntimeError):
    """Base exception for transactional import application."""


class ImportPreviewRejectedError(ImportApplyError):
    """The approved preview still contains review-required items."""


class ImportPreviewStaleError(ImportApplyError):
    """The database changed after the preview was approved."""


class ImportBackupError(ImportApplyError):
    """A verified pre-import database backup could not be created."""


class ImportVerificationError(ImportApplyError):
    """The applied transaction did not produce the expected state."""


@dataclass(frozen=True, slots=True)
class ImportApplyResult:
    backup_path: Path
    media_added: int
    states_added: int
    states_updated: int
    episodes_added: int
    ratings_added: int
    already_applied: bool = False


def apply_database_import(
    approved: ImportDatabasePreview,
    *,
    database_path: str | Path,
    backup_path: str | Path,
    user_name: str,
    chat_id: int | None = None,
) -> ImportApplyResult:
    """
    Apply one previously approved import preview transactionally.

    Safety properties:
    - review/unresolved items are rejected;
    - a verified SQLite backup is created before mutation;
    - BEGIN IMMEDIATE prevents a write race during stale-preview validation;
    - only actions explicitly present in the approved preview are applied;
    - ratings are inserted only, never overwritten;
    - episode progress is inserted only, never deleted;
    - the entire transaction rolls back on failure;
    - replaying an already-applied preview is a safe no-op.
    """

    database = Path(
        database_path
    ).resolve()

    backup = Path(
        backup_path
    ).resolve()

    if not database.is_file():
        raise FileNotFoundError(
            f"Kokabiel database does not exist: {database}"
        )

    if not user_name.strip():
        raise ValueError(
            "user_name must not be empty"
        )

    if approved.review_count:
        raise ImportPreviewRejectedError(
            "Import preview contains review-required items."
        )

    if backup == database:
        raise ImportBackupError(
            "Backup path must differ from the live database."
        )

    resolution = _resolution_from_preview(
        approved
    )

    connection = sqlite3.connect(
        database,
        timeout=10,
        isolation_level=None,
    )

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    committed = False

    try:
        version_before_backup = int(
            connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        )

        _create_verified_backup(
            database,
            backup,
        )

        connection.execute(
            "BEGIN IMMEDIATE"
        )

        version_after_lock = int(
            connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        )

        if (
            version_after_lock
            != version_before_backup
        ):
            connection.rollback()

            try:
                backup.unlink(
                    missing_ok=True
                )
            except OSError as exc:
                raise ImportBackupError(
                    "Database changed during backup and "
                    "the stale backup could not be removed."
                ) from exc

            raise ImportPreviewStaleError(
                "Database changed while the pre-import "
                "backup was being created. Retry with a "
                "fresh preview."
            )

        current_snapshot = (
            snapshot_from_connection(
                connection,
                approved.user_id,
            )
        )

        current = build_database_preview(
            resolution,
            current_snapshot,
        )

        if current == approved:
            pass

        elif _preview_already_applied(
            approved,
            current,
        ):
            connection.rollback()

            return ImportApplyResult(
                backup_path=backup,
                media_added=0,
                states_added=0,
                states_updated=0,
                episodes_added=0,
                ratings_added=0,
                already_applied=True,
            )

        else:
            raise ImportPreviewStaleError(
                "Database state no longer matches the approved import preview."
            )

        counts = {
            "media_added": 0,
            "states_added": 0,
            "states_updated": 0,
            "episodes_added": 0,
            "ratings_added": 0,
        }

        for item in approved.items:
            _apply_item(
                connection,
                approved.user_id,
                user_name,
                chat_id,
                item,
                counts,
            )

        foreign_key_errors = (
            connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        )

        if foreign_key_errors:
            raise ImportVerificationError(
                "Foreign-key verification failed before commit."
            )

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()

        if (
            quick_check is None
            or str(
                quick_check[0]
            ).casefold()
            != "ok"
        ):
            raise ImportVerificationError(
                "SQLite quick_check failed before commit."
            )

        applied_snapshot = (
            snapshot_from_connection(
                connection,
                approved.user_id,
            )
        )

        applied_preview = (
            build_database_preview(
                resolution,
                applied_snapshot,
            )
        )

        if not _preview_already_applied(
            approved,
            applied_preview,
        ):
            raise ImportVerificationError(
                "Transaction result does not match the approved preview."
            )

        connection.commit()
        committed = True

    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    finally:
        connection.close()

    if not committed:
        raise ImportVerificationError(
            "Import transaction did not commit."
        )

    return ImportApplyResult(
        backup_path=backup,
        media_added=counts[
            "media_added"
        ],
        states_added=counts[
            "states_added"
        ],
        states_updated=counts[
            "states_updated"
        ],
        episodes_added=counts[
            "episodes_added"
        ],
        ratings_added=counts[
            "ratings_added"
        ],
        already_applied=False,
    )


def _create_verified_backup(
    database: Path,
    backup: Path,
) -> None:
    if backup.exists():
        raise FileExistsError(
            f"Backup already exists: {backup}"
        )

    backup.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_uri = (
        f"{database.as_uri()}?mode=ro"
    )

    source = None
    destination = None

    try:
        source = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=10,
        )

        destination = sqlite3.connect(
            backup,
            timeout=10,
        )

        source.backup(
            destination
        )

        row = destination.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if (
            row is None
            or str(row[0]).casefold()
            != "ok"
        ):
            raise ImportBackupError(
                "Backup integrity_check failed."
            )

        destination.commit()

        # Backups contain the same personal data as the live database.
        # Do not leave them readable by other local users.
        backup.chmod(0o600)

    except Exception as exc:
        if destination is not None:
            destination.close()
            destination = None

        if source is not None:
            source.close()
            source = None

        try:
            backup.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        if isinstance(
            exc,
            ImportBackupError,
        ):
            raise

        raise ImportBackupError(
            f"Could not create verified database backup: {exc}"
        ) from exc

    finally:
        if destination is not None:
            destination.close()

        if source is not None:
            source.close()


def _resolution_from_preview(
    preview: ImportDatabasePreview,
) -> ResolutionPreview:
    return ResolutionPreview(
        source=preview.source,
        raw_record_count=(
            preview.raw_record_count
        ),
        items=tuple(
            item.resolution
            for item in preview.items
        ),
    )


def _apply_item(
    connection: sqlite3.Connection,
    user_id: int,
    user_name: str,
    chat_id: int | None,
    item: ImportDbPreviewItem,
    counts: dict[str, int],
) -> None:
    if item.needs_review:
        raise ImportPreviewRejectedError(
            "Review-required item reached transactional apply."
        )

    if (
        item.media_type is None
        or item.tmdb_id is None
    ):
        if item.has_changes:
            raise ImportVerificationError(
                "Preview contains database actions without a canonical identity."
            )

        return

    media_type = item.media_type
    tmdb_id = item.tmdb_id
    candidate = (
        item.resolution.resolved
    )

    if candidate is None:
        if item.has_changes:
            raise ImportVerificationError(
                "Preview contains actions for an unresolved identity."
            )

        return

    if item.media_action == "add":
        media_class = (
            "film"
            if media_type == "movie"
            else "tv_series"
        )

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
            ) VALUES (?, ?, ?, ?, NULL, ?, 'unknown', 0, 0)
            """,
            (
                media_type,
                tmdb_id,
                candidate.title,
                candidate.year,
                media_class,
            ),
        )

        counts[
            "media_added"
        ] += 1

    if item.state_action == "add":
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                media_type,
                tmdb_id,
                user_name,
                int(
                    item.after_watchlist
                ),
                item.after_status,
                chat_id,
            ),
        )

        counts[
            "states_added"
        ] += 1

    elif (
        item.state_action
        == "update"
    ):
        cursor = connection.execute(
            """
            UPDATE user_media_state
            SET watchlist = ?,
                viewing_status = ?,
                user_name = ?,
                last_chat_id = COALESCE(?, last_chat_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
              AND media_type = ?
              AND tmdb_id = ?
            """,
            (
                int(
                    item.after_watchlist
                ),
                item.after_status,
                user_name,
                chat_id,
                user_id,
                media_type,
                tmdb_id,
            ),
        )

        if cursor.rowcount != 1:
            raise ImportPreviewStaleError(
                "Expected personal-state row no longer exists."
            )

        counts[
            "states_updated"
        ] += 1

    record = (
        item.resolution
        .plan_item
        .proposed
    )

    for (
        season_number,
        episode_number,
    ) in item.episode_additions:
        if record.watched_at:
            connection.execute(
                """
                INSERT INTO episode_progress (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    watched_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    record.watched_at,
                ),
            )

        else:
            connection.execute(
                """
                INSERT INTO episode_progress (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                ),
            )

        counts[
            "episodes_added"
        ] += 1

    if item.rating_action == "add":
        if item.proposed_rating is None:
            raise ImportVerificationError(
                "Rating add action lacks a proposed rating."
            )

        if record.rated_at:
            connection.execute(
                """
                INSERT INTO ratings (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    rating,
                    rated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    item.proposed_rating,
                    record.rated_at,
                ),
            )

        else:
            connection.execute(
                """
                INSERT INTO ratings (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    rating
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    item.proposed_rating,
                ),
            )

        counts[
            "ratings_added"
        ] += 1

    elif (
        item.rating_action
        == "review"
    ):
        raise ImportPreviewRejectedError(
            "Rating conflict reached transactional apply."
        )


def _preview_already_applied(
    approved: ImportDatabasePreview,
    current: ImportDatabasePreview,
) -> bool:
    if (
        approved.source
        != current.source
        or approved.user_id
        != current.user_id
        or approved.raw_record_count
        != current.raw_record_count
        or len(approved.items)
        != len(current.items)
    ):
        return False

    for (
        wanted,
        now,
    ) in zip(
        approved.items,
        current.items,
        strict=True,
    ):
        if (
            wanted.resolution
            != now.resolution
            or now.needs_review
            or wanted.notices
            != now.notices
        ):
            return False

        if now.media_action != "none":
            return False

        if wanted.state_action != "none":
            if (
                now.state_action
                != "none"
                or now.before_watchlist
                != wanted.after_watchlist
                or now.before_status
                != wanted.after_status
            ):
                return False

        else:
            if (
                now.state_action
                != "none"
                or now.before_watchlist
                != wanted.before_watchlist
                or now.before_status
                != wanted.before_status
            ):
                return False

        if now.episode_additions:
            return False

        if wanted.rating_action == "add":
            if (
                now.rating_action
                != "none"
                or now.existing_rating
                != wanted.proposed_rating
            ):
                return False

        elif wanted.rating_action == "none":
            if (
                now.rating_action
                != "none"
                or now.existing_rating
                != wanted.existing_rating
            ):
                return False

        else:
            return False

    return True
