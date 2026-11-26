"""Conservative, user-scoped reversal of journaled imports."""

from __future__ import annotations

import json
import sqlite3

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from imports.db_apply import (
    ImportApplyError,
    _assert_backup_identity,
    _create_verified_backup,
    _remove_owned_backup,
)
from imports.journal import capture_personal_titles


class ImportUndoError(ImportApplyError):
    """An import cannot be safely reversed."""


class ImportUndoUnavailable(ImportUndoError):
    """The requested batch is unavailable to this user."""


class ImportUndoStaleError(ImportUndoError):
    """The approved undo preview is no longer current."""


@dataclass(frozen=True, slots=True)
class UndoTitle:
    media_type: str
    tmdb_id: int
    before_json: str
    after_json: str


@dataclass(frozen=True, slots=True)
class ImportUndoPreview:
    user_id: int
    batch_id: int
    source: str
    created_at: str
    eligible: tuple[UndoTitle, ...]
    skipped: tuple[UndoTitle, ...]

    @property
    def restore_count(self) -> int:
        return len(self.eligible)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


@dataclass(frozen=True, slots=True)
class ImportUndoResult:
    batch_id: int
    restored_count: int
    skipped_count: int
    backup_path: Path | None


def _validate_identity(user_id: int, batch_id: int) -> None:
    if (
        type(user_id) is not int
        or type(batch_id) is not int
        or user_id <= 0
        or batch_id <= 0
    ):
        raise ImportUndoUnavailable(
            "Import batch is unavailable."
        )


def _decode_title(
    raw: str,
    *,
    user_id: int,
    media_type: str,
    tmdb_id: int,
) -> dict | None:
    """Reject malformed or cross-user journal images."""

    def integer(value):
        return type(value) is int

    def require(condition):
        if not condition:
            raise ImportUndoError(
                "Import journal contains an invalid title image."
            )

    try:
        image = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ImportUndoError(
            "Import journal image could not be decoded."
        ) from exc

    if image is None:
        return None

    require(
        isinstance(image, dict)
        and set(image) == {
            "state", "rating", "episodes",
        }
    )

    state = image["state"]
    rating = image["rating"]
    episodes = image["episodes"]

    identity = [user_id, media_type, tmdb_id]

    if state is not None:
        require(
            isinstance(state, list)
            and len(state) == 9
            and state[:3] == identity
            and isinstance(state[3], str)
            and integer(state[4])
            and state[4] in {0, 1}
            and state[5] in {
                "none", "watching", "watched",
                "caught_up", "completed",
            }
            and (
                state[6] is None
                or integer(state[6])
            )
            and isinstance(state[7], str)
            and isinstance(state[8], str)
        )

    if rating is not None:
        require(
            isinstance(rating, list)
            and len(rating) == 6
            and rating[:3] == identity
            and isinstance(rating[3], str)
            and integer(rating[4])
            and 1 <= rating[4] <= 10
            and isinstance(rating[5], str)
        )

    require(isinstance(episodes, list))
    require(media_type == "tv" or not episodes)

    coordinates = set()

    for episode in episodes:
        require(
            isinstance(episode, list)
            and len(episode) == 5
            and episode[:2] == [user_id, tmdb_id]
            and integer(episode[2])
            and episode[2] >= 0
            and integer(episode[3])
            and episode[3] >= 0
            and isinstance(episode[4], str)
        )

        key = tuple(episode[2:4])

        require(key not in coordinates)
        coordinates.add(key)

    require(
        state is not None
        or rating is not None
        or bool(episodes)
    )

    return image


def _build_preview(
    connection: sqlite3.Connection,
    user_id: int,
    batch_id: int,
) -> ImportUndoPreview:
    _validate_identity(user_id, batch_id)

    batch = connection.execute(
        """
        SELECT source, created_at
        FROM import_batches
        WHERE batch_id = ?
          AND user_id = ?
          AND undone_at IS NULL
        """,
        (batch_id, user_id),
    ).fetchone()

    if batch is None:
        raise ImportUndoUnavailable(
            "Import batch is unavailable."
        )

    current = capture_personal_titles(
        connection,
        user_id,
    )

    eligible = []
    skipped = []

    for row in connection.execute(
        """
        SELECT media_type, tmdb_id,
               before_json, after_json, invalidated
        FROM import_title_changes
        WHERE batch_id = ?
        ORDER BY media_type, tmdb_id
        """,
        (batch_id,),
    ):
        media_type, tmdb_id, before, after, invalidated = row

        if (
            media_type not in {"movie", "tv"}
            or type(tmdb_id) is not int
            or tmdb_id <= 0
            or invalidated not in {0, 1}
        ):
            raise ImportUndoError(
                "Invalid import journal identity."
            )

        title = UndoTitle(
            media_type,
            tmdb_id,
            before,
            after,
        )

        identity = {
            "user_id": user_id,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
        }

        before_image = _decode_title(
            before, **identity
        )

        after_image = _decode_title(
            after, **identity
        )

        if before_image == after_image:
            raise ImportUndoError(
                "Journal entry contains no change."
            )

        if (
            invalidated
            or current.get(
                (media_type, tmdb_id)
            ) != after_image
        ):
            skipped.append(title)
        else:
            eligible.append(title)

    if not eligible and not skipped:
        raise ImportUndoError(
            "Import batch contains no journal entries."
        )

    return ImportUndoPreview(
        user_id=user_id,
        batch_id=batch_id,
        source=batch[0],
        created_at=batch[1],
        eligible=tuple(eligible),
        skipped=tuple(skipped),
    )


def preview_import_undo(
    database_path: str | Path,
    *,
    user_id: int,
    batch_id: int,
) -> ImportUndoPreview:
    """Generate a read-only, consistent undo preview."""

    database = Path(database_path).resolve()

    with closing(sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=10,
        isolation_level=None,
    )) as connection:
        connection.execute("BEGIN")

        try:
            return _build_preview(
                connection, user_id, batch_id
            )
        finally:
            connection.rollback()


def _restore_title(
    connection: sqlite3.Connection,
    user_id: int,
    title: UndoTitle,
) -> None:
    media_type = title.media_type
    tmdb_id = title.tmdb_id

    identity = {
        "user_id": user_id,
        "media_type": media_type,
        "tmdb_id": tmdb_id,
    }

    before = _decode_title(
        title.before_json, **identity
    )

    after = _decode_title(
        title.after_json, **identity
    )

    empty = {
        "state": None,
        "rating": None,
        "episodes": [],
    }

    before = before or empty
    after = after or empty

    key = (user_id, media_type, tmdb_id)

    # Modify only personal components changed by the import.

    if before["state"] != after["state"]:
        connection.execute(
            """
            DELETE FROM user_media_state
            WHERE user_id = ?
              AND media_type = ?
              AND tmdb_id = ?
            """,
            key,
        )

        if before["state"] is not None:
            connection.execute(
                """
                INSERT INTO user_media_state (
                    user_id, media_type, tmdb_id,
                    user_name, watchlist, viewing_status,
                    last_chat_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                before["state"],
            )

    if before["rating"] != after["rating"]:
        connection.execute(
            """
            DELETE FROM ratings
            WHERE user_id = ?
              AND media_type = ?
              AND tmdb_id = ?
            """,
            key,
        )

        if before["rating"] is not None:
            connection.execute(
                """
                INSERT INTO ratings (
                    user_id, media_type, tmdb_id,
                    user_name, rating, rated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                before["rating"],
            )

    if (
        media_type == "tv"
        and before["episodes"] != after["episodes"]
    ):
        original = {
            tuple(row[2:4]): row
            for row in before["episodes"]
        }

        imported = {
            tuple(row[2:4]): row
            for row in after["episodes"]
        }

        for coordinates in sorted(
            original.keys() | imported.keys()
        ):
            old = original.get(coordinates)
            new = imported.get(coordinates)

            if old == new:
                continue

            connection.execute(
                """
                DELETE FROM episode_progress
                WHERE user_id = ?
                  AND tmdb_id = ?
                  AND season_number = ?
                  AND episode_number = ?
                """,
                (user_id, tmdb_id, *coordinates),
            )

            if old is not None:
                connection.execute(
                    """
                    INSERT INTO episode_progress (
                        user_id, tmdb_id,
                        season_number, episode_number,
                        watched_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    old,
                )


def apply_import_undo(
    approved: ImportUndoPreview,
    *,
    database_path: str | Path,
    backup_path: str | Path,
) -> ImportUndoResult:
    """Apply only an unchanged, explicitly approved undo."""

    _validate_identity(
        approved.user_id,
        approved.batch_id,
    )

    database = Path(database_path).resolve()

    requested = Path(backup_path)
    backup = requested.parent.resolve() / requested.name

    if backup == database:
        raise ImportUndoError(
            "Undo backup must differ from the database."
        )

    backup_identity = None
    mutation_started = False

    def check_preview(connection):
        fresh = _build_preview(
            connection,
            approved.user_id,
            approved.batch_id,
        )

        if fresh != approved:
            raise ImportUndoStaleError(
                "Import undo preview has changed."
            )

    with closing(sqlite3.connect(
        f"{database.as_uri()}?mode=rw",
        uri=True,
        timeout=10,
        isolation_level=None,
    )) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        try:
            connection.execute("BEGIN IMMEDIATE")

            check_preview(connection)

            if not approved.eligible:
                connection.rollback()

                return ImportUndoResult(
                    approved.batch_id,
                    0,
                    approved.skipped_count,
                    None,
                )

            version = connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]

            connection.rollback()

            backup_identity = _create_verified_backup(
                database, backup
            )

            _assert_backup_identity(
                backup, backup_identity
            )

            connection.execute("BEGIN IMMEDIATE")

            if connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0] != version:
                raise ImportUndoStaleError(
                    "Database changed during undo backup."
                )

            check_preview(connection)

            expected = capture_personal_titles(
                connection,
                approved.user_id,
            )

            for title in approved.eligible:
                key = (
                    title.media_type,
                    title.tmdb_id,
                )

                before = _decode_title(
                    title.before_json,
                    user_id=approved.user_id,
                    media_type=title.media_type,
                    tmdb_id=title.tmdb_id,
                )

                if before is None:
                    expected.pop(key, None)
                else:
                    expected[key] = before

            _assert_backup_identity(
                backup, backup_identity
            )

            mutation_started = True

            for title in approved.eligible:
                _restore_title(
                    connection,
                    approved.user_id,
                    title,
                )

            actual = capture_personal_titles(
                connection,
                approved.user_id,
            )

            if actual != expected:
                raise ImportUndoError(
                    "Undo result does not match approval."
                )

            # Close the batch after successful restoration.
            # Previously skipped titles remain preserved.

            cursor = connection.execute(
                """
                UPDATE import_batches
                SET undone_at = CURRENT_TIMESTAMP
                WHERE batch_id = ?
                  AND user_id = ?
                  AND undone_at IS NULL
                """,
                (
                    approved.batch_id,
                    approved.user_id,
                ),
            )

            if cursor.rowcount != 1:
                raise ImportUndoStaleError(
                    "Import batch changed during undo."
                )

            # Undo itself fires the journal triggers.
            # Preserve the final distinction for this closed batch:
            # 0 = restored, 1 = retained/skipped.

            connection.execute(
                """
                UPDATE import_title_changes
                SET invalidated = 1
                WHERE batch_id = ?
                """,
                (approved.batch_id,),
            )

            connection.executemany(
                """
                UPDATE import_title_changes
                SET invalidated = 0
                WHERE batch_id = ?
                  AND media_type = ?
                  AND tmdb_id = ?
                """,
                (
                    (
                        approved.batch_id,
                        title.media_type,
                        title.tmdb_id,
                    )
                    for title in approved.eligible
                ),
            )

            if connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone() is not None:
                raise ImportUndoError(
                    "Undo foreign-key verification failed."
                )

            if connection.execute(
                "PRAGMA quick_check"
            ).fetchall() != [("ok",)]:
                raise ImportUndoError(
                    "Undo database verification failed."
                )

            connection.commit()

        except BaseException:
            if connection.in_transaction:
                connection.rollback()

            if (
                backup_identity is not None
                and not mutation_started
            ):
                _remove_owned_backup(
                    backup, backup_identity
                )

            raise

    return ImportUndoResult(
        approved.batch_id,
        approved.restore_count,
        approved.skipped_count,
        backup,
    )


def list_import_batches(
    database_path: str | Path,
    *,
    user_id: int,
    before_id: int | None = None,
) -> tuple:
    """Return at most nine open batches belonging to this user."""

    if type(user_id) is not int or user_id <= 0:
        raise ImportUndoUnavailable("Import history unavailable.")

    if before_id is not None:
        _validate_identity(user_id, before_id)

    database = Path(database_path).resolve()

    with closing(sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=10,
    )) as connection:
        return tuple(connection.execute(
            """
            SELECT b.batch_id, b.source, b.created_at,
                   COUNT(c.tmdb_id)
            FROM import_batches b
            JOIN import_title_changes c USING (batch_id)
            WHERE b.user_id = ?
              AND b.undone_at IS NULL
              AND (? IS NULL OR b.batch_id < ?)
            GROUP BY b.batch_id, b.source, b.created_at
            ORDER BY b.batch_id DESC
            LIMIT 9
            """,
            (user_id, before_id, before_id),
        ))
