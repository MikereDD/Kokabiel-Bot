from __future__ import annotations

import os
import sqlite3
import time

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from imports.db_preview import (
    ExistingUserSnapshot,
    ImportDatabasePreview,
    ImportDbPreviewItem,
    build_database_preview,
    snapshot_from_connection,
)
from imports.resolve import ResolutionPreview
from imports.journal import (
    capture_personal_titles,
    record_completed_import,
)


BACKUP_DEADLINE_SECONDS = 30.0
BACKUP_BUSY_TIMEOUT_SECONDS = 0.25
BACKUP_RETRY_SECONDS = 0.05
BACKUP_STEP_PAGES = 128


class ImportApplyError(RuntimeError):
    """Base exception for transactional import application."""


class ImportPreviewRejectedError(ImportApplyError):
    """The approved preview still contains review-required items."""


class ImportPreviewStaleError(ImportApplyError):
    """The database changed after the preview was approved."""


class ImportBackupError(ImportApplyError):
    """A verified pre-import database backup could not be created."""


class ImportPreWriteError(ImportApplyError):
    """Import failed before database mutations were attempted."""


class ImportVerificationError(ImportApplyError):
    """The applied transaction did not produce the expected state."""


@dataclass(frozen=True, slots=True)
class ImportApplyResult:
    backup_path: Path | None
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
    origin_group_id: int | None = None,
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

    if origin_group_id is not None:
        if type(origin_group_id) is not int or origin_group_id >= 0:
            raise ValueError("Invalid import originating group.")

        chat_id = origin_group_id

    database = Path(
        database_path
    ).resolve()

    requested_backup = Path(backup_path)

    backup = (
        requested_backup.parent.resolve()
        / requested_backup.name
    )

    if not database.is_file():
        raise ImportPreWriteError(
            "Kokabiel database does not exist."
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

    connection = None
    backup_identity = None
    mutation_started = False
    committed = False

    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=rw",
            uri=True,
            timeout=10,
            isolation_level=None,
        )

        connection.execute("PRAGMA foreign_keys = ON")

        # Preflight under a write reservation. A database
        # that is already locked fails before backup creation.
        connection.execute("BEGIN IMMEDIATE")

        current_snapshot = snapshot_from_connection(
            connection,
            approved.user_id,
        )

        current = build_database_preview(
            resolution,
            current_snapshot,
        )

        if current != approved:
            if not _preview_already_applied(
                approved,
                current,
                current_snapshot,
            ):
                raise ImportPreviewStaleError(
                    "Database state no longer matches "
                    "the approved import preview."
                )

            no_changes = True

        else:
            no_changes = not any(
                item.has_changes
                for item in approved.items
            )

        if no_changes:
            connection.rollback()

            return ImportApplyResult(
                backup_path=None,
                media_added=0,
                states_added=0,
                states_updated=0,
                episodes_added=0,
                ratings_added=0,
                already_applied=True,
            )

        version_before_backup = int(
            connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        )

        # The backup uses a separate read-only connection.
        # Release the reservation before copying the database.
        connection.rollback()

        backup_identity = _create_verified_backup(
            database,
            backup,
        )

        _assert_backup_identity(
            backup,
            backup_identity,
        )

        # Reacquire the reservation and ensure no other
        # connection changed the database during the gap.
        connection.execute("BEGIN IMMEDIATE")

        version_after_lock = int(
            connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        )

        if version_after_lock != version_before_backup:
            raise ImportPreviewStaleError(
                "Database changed while the pre-import "
                "backup was being created. Retry with a "
                "fresh preview."
            )

        current_snapshot = snapshot_from_connection(
            connection,
            approved.user_id,
        )

        current = build_database_preview(
            resolution,
            current_snapshot,
        )

        if current != approved:
            raise ImportPreviewStaleError(
                "Database state no longer matches "
                "the approved import preview."
            )

        expected_state = _expected_final_state(
            current_snapshot,
            approved,
        )

        counts = {
            "media_added": 0,
            "states_added": 0,
            "states_updated": 0,
            "episodes_added": 0,
            "ratings_added": 0,
        }

        _assert_backup_identity(
            backup,
            backup_identity,
        )

        journal_before = capture_personal_titles(
            connection,
            approved.user_id,
        )

        mutation_started = True

        for item in approved.items:
            _apply_item(
                connection,
                approved.user_id,
                user_name,
                chat_id,
                item,
                counts,
            )

        if origin_group_id is not None:
            connection.execute(
                '''
                INSERT INTO user_chats (
                    chat_id, user_id, user_name
                ) VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET
                    user_name = excluded.user_name,
                    last_seen_at = CURRENT_TIMESTAMP
                ''',
                (
                    origin_group_id,
                    approved.user_id,
                    user_name,
                ),
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

        if (
            _snapshot_state(applied_snapshot)
            != expected_state
            or not _preview_already_applied(
                approved,
                applied_preview,
                applied_snapshot,
            )
        ):
            raise ImportVerificationError(
                "Transaction result does not match the approved preview."
            )

        record_completed_import(
            connection,
            user_id=approved.user_id,
            source=approved.source,
            origin_group_id=origin_group_id,
            before=journal_before,
            after=capture_personal_titles(
                connection,
                approved.user_id,
            ),
        )

        connection.commit()
        committed = True

    except Exception as exc:
        if (
            connection is not None
            and connection.in_transaction
        ):
            connection.rollback()

        if not mutation_started:
            if backup_identity is not None:
                _remove_owned_backup(
                    backup,
                    backup_identity,
                )

            if not isinstance(exc, ImportApplyError):
                raise ImportPreWriteError(
                    "Import failed before database "
                    "mutations were attempted."
                ) from exc

        raise

    finally:
        if connection is not None:
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


def _assert_backup_identity(
    backup: Path,
    identity: tuple[int, int],
) -> None:
    """Require the originally reserved backup to remain present."""
    try:
        current = backup.lstat()

    except OSError as exc:
        raise ImportBackupError(
            "Verified backup is no longer available."
        ) from exc

    if (current.st_dev, current.st_ino) != identity:
        raise ImportBackupError(
            "Verified backup identity changed."
        )


def _remove_owned_backup(
    backup: Path,
    identity: tuple[int, int],
) -> None:
    """Remove only the backup reserved by this operation."""
    try:
        current = backup.lstat()

    except FileNotFoundError:
        return

    except OSError as exc:
        raise ImportBackupError(
            "Could not inspect unused backup."
        ) from exc

    if (current.st_dev, current.st_ino) != identity:
        raise ImportBackupError(
            "Backup identity changed; refusing removal."
        )

    try:
        backup.unlink()

    except OSError as exc:
        raise ImportBackupError(
            "Could not remove unused backup."
        ) from exc


def _create_verified_backup(
    database: Path,
    backup: Path,
) -> tuple[int, int]:
    identity = None

    deadline = (
        time.monotonic()
        + BACKUP_DEADLINE_SECONDS
    )

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise ImportBackupError(
                "Database backup execution deadline exceeded."
            )

    def backup_progress(
        status: int,
        remaining: int,
        total: int,
    ) -> None:
        # SQLite calls this after each backup step,
        # including busy and locked retry steps.
        check_deadline()

    def verification_progress() -> int:
        # Interrupt lengthy integrity verification
        # cooperatively through SQLite's progress handler.
        return int(time.monotonic() >= deadline)

    try:
        backup.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # O_EXCL rejects existing files and symlinks,
        # including dangling symlinks.
        fd = os.open(
            backup,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )

        try:
            reserved = os.fstat(fd)

            identity = (
                reserved.st_dev,
                reserved.st_ino,
            )

        finally:
            os.close(fd)

        # Ensure an unusually restrictive umask does
        # not prevent SQLite from opening the reservation.
        backup.chmod(0o600)

        source_uri = f"{database.as_uri()}?mode=ro"

        with closing(
            sqlite3.connect(
                source_uri,
                uri=True,
                timeout=BACKUP_BUSY_TIMEOUT_SECONDS,
            )
        ) as source:
            with closing(
                sqlite3.connect(
                    backup,
                    timeout=BACKUP_BUSY_TIMEOUT_SECONDS,
                )
            ) as destination:

                check_deadline()

                source.backup(
                    destination,
                    pages=BACKUP_STEP_PAGES,
                    progress=backup_progress,
                    sleep=BACKUP_RETRY_SECONDS,
                )

                check_deadline()

                destination.set_progress_handler(
                    verification_progress,
                    1000,
                )

                try:
                    row = destination.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()

                except sqlite3.OperationalError:
                    # Distinguish deadline interruption
                    # from unrelated SQLite failures.
                    check_deadline()
                    raise

                finally:
                    destination.set_progress_handler(
                        None,
                        0,
                    )

                check_deadline()

                if (
                    row is None
                    or str(row[0]).casefold() != "ok"
                ):
                    raise ImportBackupError(
                        "Backup integrity_check failed."
                    )

                destination.commit()

                check_deadline()

        _assert_backup_identity(
            backup,
            identity,
        )

        check_deadline()

        return identity

    except Exception as exc:
        # Never remove a destination that we failed
        # to reserve exclusively.
        if identity is not None:
            _remove_owned_backup(
                backup,
                identity,
            )

        if isinstance(exc, ImportBackupError):
            raise

        if isinstance(exc, FileExistsError):
            raise ImportBackupError(
                "Backup destination already exists."
            ) from exc

        raise ImportBackupError(
            "Could not create verified database backup."
        ) from exc


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


def _snapshot_state(
    snapshot: ExistingUserSnapshot,
) -> tuple:
    """Project import-relevant persisted values.

    Generated timestamps are intentionally excluded from this
    projection. Personal-state row existence, rating values,
    catalog identities and exact episode sets are retained.
    """
    return (
        snapshot.user_id,
        {
            key: (
                state.watchlist,
                state.viewing_status,
            )
            for key, state in snapshot.states.items()
        },
        {
            key: rating.rating
            for key, rating in snapshot.ratings.items()
        },
        snapshot.episodes,
        snapshot.media,
    )


def _expected_final_state(
    snapshot: ExistingUserSnapshot,
    approved: ImportDatabasePreview,
) -> tuple:
    """Fold approved actions over the locked starting snapshot.

    Later approved state transitions supersede earlier
    transitions for the same canonical identity.

    This calculation does not execute SQL and does not use
    the database writer's mutation implementation.
    """
    if snapshot.user_id != approved.user_id:
        raise ImportVerificationError(
            "Approved preview has an unexpected user identity."
        )

    _, states, ratings, episodes, media = _snapshot_state(
        snapshot
    )

    episodes = {
        key: set(values)
        for key, values in episodes.items()
    }

    media = set(media)

    for item in approved.items:
        if item.needs_review:
            raise ImportPreviewRejectedError(
                "Review-required item reached verification."
            )

        if (
            item.media_type is None
            or item.tmdb_id is None
        ):
            if item.has_changes:
                raise ImportVerificationError(
                    "Approved action lacks a canonical identity."
                )

            continue

        key = (
            item.media_type,
            item.tmdb_id,
        )

        if item.media_action == "add":
            media.add(key)

        if item.state_action in {"add", "update"}:
            states[key] = (
                item.after_watchlist,
                item.after_status,
            )

        if item.rating_action == "add":
            if item.proposed_rating is None:
                raise ImportVerificationError(
                    "Approved rating action lacks a rating."
                )

            ratings[key] = item.proposed_rating

        elif item.rating_action == "review":
            raise ImportPreviewRejectedError(
                "Rating conflict reached verification."
            )

        if item.episode_additions:
            episodes.setdefault(
                item.tmdb_id,
                set(),
            ).update(
                item.episode_additions
            )

    return (
        snapshot.user_id,
        states,
        ratings,
        {
            key: frozenset(values)
            for key, values in episodes.items()
        },
        frozenset(media),
    )


def _preview_already_applied(
    approved: ImportDatabasePreview,
    current: ImportDatabasePreview,
    snapshot: ExistingUserSnapshot,
) -> bool:
    """Recognize a safe replay using final canonical state.

    Intermediate personal-state transitions are not required
    to survive later approved transitions.

    Every approved resolution must still agree with the
    current preview, and re-planning must require no writes.
    """
    if (
        approved.source != current.source
        or approved.user_id != current.user_id
        or approved.user_id != snapshot.user_id
        or approved.raw_record_count != current.raw_record_count
        or len(approved.items) != len(current.items)
    ):
        return False

    final_states = {}

    for wanted, now in zip(
        approved.items,
        current.items,
        strict=True,
    ):
        if (
            wanted.resolution != now.resolution
            or wanted.needs_review
            or now.needs_review
            or wanted.notices != now.notices
            or wanted.media_type != now.media_type
            or wanted.tmdb_id != now.tmdb_id
        ):
            return False

        if (
            now.media_action != "none"
            or now.state_action != "none"
            or now.rating_action != "none"
            or now.episode_additions
        ):
            return False

        if (
            wanted.media_type is None
            or wanted.tmdb_id is None
        ):
            if wanted.has_changes:
                return False

            continue

        key = (
            wanted.media_type,
            wanted.tmdb_id,
        )

        if (
            wanted.media_action == "add"
            and key not in snapshot.media
        ):
            return False

        final_states[key] = (
            wanted.after_watchlist,
            wanted.after_status,
        )

        if wanted.state_action != "none":
            if key not in snapshot.states:
                return False

        if wanted.episode_additions:
            existing_episodes = snapshot.episodes.get(
                wanted.tmdb_id,
                frozenset(),
            )

            if not set(
                wanted.episode_additions
            ).issubset(existing_episodes):
                return False

        if wanted.rating_action == "add":
            if (
                wanted.proposed_rating is None
                or now.existing_rating != wanted.proposed_rating
            ):
                return False

        elif wanted.rating_action == "none":
            if now.existing_rating != wanted.existing_rating:
                return False

        else:
            return False

    for key, expected in final_states.items():
        state = snapshot.states.get(key)

        actual = (
            (state.watchlist, state.viewing_status)
            if state is not None
            else (False, "none")
        )

        if actual != expected:
            return False

    return True
