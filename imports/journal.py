"""Transactional provenance for completed personal imports."""

from __future__ import annotations

import json
import sqlite3


TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS import_batches (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        origin_group_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        undone_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_title_changes (
        batch_id INTEGER NOT NULL,
        media_type TEXT NOT NULL
            CHECK (media_type IN ('movie', 'tv')),
        tmdb_id INTEGER NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        invalidated INTEGER NOT NULL DEFAULT 0
            CHECK (invalidated IN (0, 1)),
        PRIMARY KEY (batch_id, media_type, tmdb_id),
        FOREIGN KEY (batch_id)
            REFERENCES import_batches(batch_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_import_batches_user
    ON import_batches(user_id, batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_import_title_identity
    ON import_title_changes(media_type, tmdb_id, batch_id)
    """,
)


def _invalidate_statement(table: str, row: str) -> str:
    media_type = (
        "'tv'"
        if table == "episode_progress"
        else f"{row}.media_type"
    )

    return f"""
        UPDATE import_title_changes
        SET invalidated = 1
        WHERE media_type = {media_type}
          AND tmdb_id = {row}.tmdb_id
          AND invalidated = 0
          AND batch_id IN (
              SELECT batch_id
              FROM import_batches
              WHERE user_id = {row}.user_id
                AND undone_at IS NULL
          );
    """


def _trigger_statements() -> tuple[str, ...]:
    statements = []

    for table in (
        "user_media_state",
        "ratings",
        "episode_progress",
    ):
        for operation, rows in (
            ("INSERT", ("NEW",)),
            ("UPDATE", ("OLD", "NEW")),
            ("DELETE", ("OLD",)),
        ):
            body = "\n".join(
                _invalidate_statement(table, row)
                for row in rows
            )

            statements.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS
                    import_journal_{table}_{operation.lower()}
                AFTER {operation} ON {table}
                BEGIN
                    {body}
                END
                """
            )

    return tuple(statements)


TRIGGER_STATEMENTS = _trigger_statements()

JOURNAL_SCHEMA = (
    ";\n".join(
        TABLE_STATEMENTS + TRIGGER_STATEMENTS
    )
    + ";\n"
)


def install_journal_schema(
    connection: sqlite3.Connection,
) -> None:
    # Individual statements preserve the caller's transaction.
    for statement in TABLE_STATEMENTS + TRIGGER_STATEMENTS:
        connection.execute(statement)


def _empty_title() -> dict:
    return {
        "state": None,
        "rating": None,
        "episodes": [],
    }


def capture_personal_titles(
    connection: sqlite3.Connection,
    user_id: int,
) -> dict:
    """Capture exact persisted personal values, not shared catalog data."""

    result = {}

    for row in connection.execute(
        """
        SELECT user_id, media_type, tmdb_id, user_name,
               watchlist, viewing_status, last_chat_id,
               created_at, updated_at
        FROM user_media_state
        WHERE user_id = ?
        ORDER BY media_type, tmdb_id
        """,
        (user_id,),
    ):
        values = list(row)

        key = (values[1], values[2])

        result.setdefault(
            key, _empty_title()
        )["state"] = values

    for row in connection.execute(
        """
        SELECT user_id, media_type, tmdb_id,
               user_name, rating, rated_at
        FROM ratings
        WHERE user_id = ?
        ORDER BY media_type, tmdb_id
        """,
        (user_id,),
    ):
        values = list(row)

        key = (values[1], values[2])

        result.setdefault(
            key, _empty_title()
        )["rating"] = values

    for row in connection.execute(
        """
        SELECT user_id, tmdb_id,
               season_number, episode_number, watched_at
        FROM episode_progress
        WHERE user_id = ?
        ORDER BY tmdb_id, season_number, episode_number
        """,
        (user_id,),
    ):
        values = list(row)

        key = ("tv", values[1])

        result.setdefault(
            key, _empty_title()
        )["episodes"].append(values)

    return result


def _encode(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def record_completed_import(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    source: str,
    origin_group_id: int | None,
    before: dict,
    after: dict,
) -> int | None:
    """Record the delta inside the import's existing transaction."""

    if not connection.in_transaction:
        raise RuntimeError(
            "Import journal requires an active transaction."
        )

    changes = [
        (
            media_type,
            tmdb_id,
            _encode(before.get((media_type, tmdb_id))),
            _encode(after.get((media_type, tmdb_id))),
        )
        for media_type, tmdb_id in sorted(
            before.keys() | after.keys()
        )
        if before.get((media_type, tmdb_id))
        != after.get((media_type, tmdb_id))
    ]

    if not changes:
        return None

    cursor = connection.execute(
        """
        INSERT INTO import_batches (
            user_id, source, origin_group_id
        ) VALUES (?, ?, ?)
        """,
        (user_id, source, origin_group_id),
    )

    batch_id = cursor.lastrowid

    if batch_id is None:
        raise RuntimeError(
            "Import journal did not create a batch."
        )

    connection.executemany(
        """
        INSERT INTO import_title_changes (
            batch_id, media_type, tmdb_id,
            before_json, after_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (batch_id, *change)
            for change in changes
        ),
    )

    return int(batch_id)
