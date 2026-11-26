from __future__ import annotations

import asyncio
import random
import sqlite3
import json
from dataclasses import asdict, replace
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

from imports.journal import (
    JOURNAL_SCHEMA,
    install_journal_schema,
)

from release_intelligence import (
    ReleaseCandidate,
    ReleaseSnapshot,
    ScheduledRelease,
    build_release_snapshot,
)

from models import (
    GroupEntry,
    KnownChat,
    MediaClass,
    MediaDetails,
    MediaType,
    PersonalEntry,
    PersonalLibraryPage,
    SuggestionEntry,
    UserMediaState,
    ViewingStatus,
)

T = TypeVar("T")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS media (
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year TEXT NOT NULL,
    poster_url TEXT,
    media_class TEXT NOT NULL DEFAULT 'film',
    series_status TEXT NOT NULL DEFAULT 'unknown',
    season_count INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (media_type, tmdb_id)
);

-- Retained only so existing installations can be migrated safely.
CREATE TABLE IF NOT EXISTS group_catalog (
    chat_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    watchlist INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
    watched INTEGER NOT NULL DEFAULT 0 CHECK (watched IN (0, 1)),
    added_by INTEGER NOT NULL,
    added_by_name TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, media_type, tmdb_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_media_state (
    user_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    watchlist INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
    viewing_status TEXT NOT NULL DEFAULT 'none'
        CHECK (viewing_status IN ('none', 'watching', 'watched', 'caught_up', 'completed')),
    last_chat_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, media_type, tmdb_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_progress (
    user_id INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    watched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, tmdb_id, season_number, episode_number)
);

CREATE TABLE IF NOT EXISTS ratings (
    user_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
    rated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, media_type, tmdb_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

-- Group membership/interaction bridge. Personal media state is never keyed by chat.
CREATE TABLE IF NOT EXISTS user_chats (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS suggestions (
    chat_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    suggested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, media_type, tmdb_id, user_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

-- Legacy table retained for one-way migration into suggestions.
CREATE TABLE IF NOT EXISTS recommendations (
    chat_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    recommended_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, media_type, tmdb_id, user_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS known_chats (
    chat_id INTEGER NOT NULL PRIMARY KEY,
    chat_type TEXT NOT NULL,
    title TEXT NOT NULL,
    username TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS release_cache (
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    region TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    lifecycle TEXT NOT NULL,
    events_json TEXT NOT NULL,
    PRIMARY KEY (media_type, tmdb_id, region),
    FOREIGN KEY (media_type, tmdb_id)
        REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_known_chats_type_active
ON known_chats(chat_type, is_active, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_user_watchlist
ON user_media_state(user_id, watchlist, updated_at);

CREATE INDEX IF NOT EXISTS idx_user_status
ON user_media_state(user_id, viewing_status, updated_at);

CREATE INDEX IF NOT EXISTS idx_episode_progress_user
ON episode_progress(user_id, tmdb_id, season_number);

CREATE INDEX IF NOT EXISTS idx_user_chats_chat
ON user_chats(chat_id, user_id, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_ratings_media
ON ratings(media_type, tmdb_id, user_id);

CREATE INDEX IF NOT EXISTS idx_suggestions_group
ON suggestions(chat_id, suggested_at);
"""


SCHEMA += JOURNAL_SCHEMA


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)
            self._ensure_media_columns(connection)
            self._migrate_legacy_group_state(connection)
            self._migrate_recommendations(connection)
            self._migrate_global_user_state(connection)
            self._ensure_indexes(connection)
            install_journal_schema(connection)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except BaseException:
            connection.close()
            raise

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:
            return await asyncio.to_thread(self._run_write_sync, operation)

    def _run_write_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with closing(self._connect()) as connection, connection:
            result = operation(connection)
            connection.commit()
            return result

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run_read_sync, operation)

    def _run_read_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with closing(self._connect()) as connection, connection:
            return operation(connection)

    # ------------------------------------------------------------------
    # Chat tracking / owner commands
    # ------------------------------------------------------------------

    async def record_chat(
        self,
        chat_id: int,
        chat_type: str,
        title: str,
        username: str | None = None,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO known_chats (
                    chat_id, chat_type, title, username, is_active
                ) VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(chat_id)
                DO UPDATE SET chat_type = excluded.chat_type,
                              title = excluded.title,
                              username = excluded.username,
                              is_active = 1,
                              last_seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id, chat_type, title, username),
            )

        await self._write(operation)

    async def record_user_chat(
        self,
        chat_id: int,
        user_id: int,
        user_name: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO user_chats (
                    chat_id, user_id, user_name
                ) VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET user_name = excluded.user_name,
                              last_seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id, user_id, user_name),
            )

        await self._write(operation)

    async def mark_chat_inactive(self, chat_id: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE known_chats
                SET is_active = 0, last_seen_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

        await self._write(operation)

    async def list_known_groups(
        self, *, include_inactive: bool = True
    ) -> list[KnownChat]:
        def operation(connection: sqlite3.Connection) -> list[KnownChat]:
            where = "chat_type IN ('group', 'supergroup')"
            if not include_inactive:
                where += " AND is_active = 1"
            rows = connection.execute(
                f"""
                SELECT chat_id, chat_type, title, username, is_active,
                       first_seen_at, last_seen_at
                FROM known_chats
                WHERE {where}
                ORDER BY is_active DESC, title COLLATE NOCASE, chat_id
                """
            ).fetchall()
            return [
                KnownChat(
                    chat_id=int(row["chat_id"]),
                    chat_type=str(row["chat_type"]),
                    title=str(row["title"]),
                    username=row["username"],
                    is_active=bool(row["is_active"]),
                    first_seen_at=str(row["first_seen_at"]),
                    last_seen_at=str(row["last_seen_at"]),
                )
                for row in rows
            ]

        return await self._read(operation)

    async def count_active_groups(self) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM known_chats
                WHERE chat_type IN ('group', 'supergroup') AND is_active = 1
                """
            ).fetchone()
            return int(row["count"] if row else 0)

        return await self._read(operation)

    # ------------------------------------------------------------------
    # Personal watchlists and viewing state
    # ------------------------------------------------------------------

    async def user_state(
        self,
        chat_id: int,
        user_id: int,
        media_type: MediaType,
        tmdb_id: int,
    ) -> UserMediaState:
        def operation(connection: sqlite3.Connection) -> UserMediaState:
            row = connection.execute(
                """
                SELECT ums.watchlist, ums.viewing_status,
                       CASE WHEN ums.media_type = 'tv' THEN (
                           SELECT COUNT(*) FROM episode_progress ep
                           WHERE ep.user_id = ums.user_id
                             AND ep.tmdb_id = ums.tmdb_id
                       ) ELSE 0 END AS watched_episodes
                FROM user_media_state ums
                WHERE ums.user_id = ?
                  AND ums.media_type = ? AND ums.tmdb_id = ?
                """,
                (user_id, media_type, tmdb_id),
            ).fetchone()
            if row is None:
                return UserMediaState()
            return UserMediaState(
                watchlist=bool(row["watchlist"]),
                viewing_status=str(row["viewing_status"]),
                watched_episodes=int(row["watched_episodes"]),
            )

        return await self._read(operation)

    async def toggle_watchlist(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            self._upsert_media(connection, details)
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            row = connection.execute(
                """
                SELECT watchlist FROM user_media_state
                WHERE user_id = ?
                  AND media_type = ? AND tmdb_id = ?
                """,
                (user_id, details.media_type, details.tmdb_id),
            ).fetchone()
            enabled = not bool(row["watchlist"])
            connection.execute(
                """
                UPDATE user_media_state
                SET watchlist = ?, user_name = ?, last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = ? AND tmdb_id = ?
                """,
                (
                    int(enabled),
                    user_name,
                    chat_id,
                    user_id,
                    details.media_type,
                    details.tmdb_id,
                ),
            )
            return enabled

        return await self._write(operation)

    async def toggle_movie_watched(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> bool:
        if details.media_type != "movie":
            raise ValueError("movie watched toggle requires a movie")

        def operation(connection: sqlite3.Connection) -> bool:
            self._upsert_media(connection, details)
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            row = connection.execute(
                """
                SELECT viewing_status FROM user_media_state
                WHERE user_id = ?
                  AND media_type = 'movie' AND tmdb_id = ?
                """,
                (user_id, details.tmdb_id),
            ).fetchone()
            enabled = str(row["viewing_status"]) != "watched"
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = ?,
                    watchlist = CASE WHEN ? = 1 THEN 0 ELSE watchlist END,
                    user_name = ?, last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = 'movie' AND tmdb_id = ?
                """,
                (
                    "watched" if enabled else "none",
                    int(enabled),
                    user_name,
                    chat_id,
                    user_id,
                    details.tmdb_id,
                ),
            )
            return enabled

        return await self._write(operation)

    async def set_series_status(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        status: ViewingStatus,
    ) -> None:
        if details.media_type != "tv":
            raise ValueError("series status requires TV media")
        if status not in {"none", "watching", "caught_up", "completed"}:
            raise ValueError("invalid series status")

        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = ?,
                    watchlist = CASE WHEN ? != 'none' THEN 0 ELSE watchlist END,
                    user_name = ?, last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (status, status, user_name, chat_id, user_id, details.tmdb_id),
            )

        await self._write(operation)

    async def set_episode_watched(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        season_number: int,
        episode_number: int,
        enabled: bool,
    ) -> None:
        await self.set_episode_keys(
            chat_id,
            details,
            user_id,
            user_name,
            [(season_number, episode_number)],
            enabled=enabled,
        )

    async def set_episode_keys(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        episode_keys: Iterable[tuple[int, int]],
        *,
        enabled: bool,
    ) -> None:
        if details.media_type != "tv":
            raise ValueError("episode progress requires TV media")
        keys = tuple({(int(season), int(episode)) for season, episode in episode_keys})

        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            if enabled:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO episode_progress (
                        user_id, tmdb_id, season_number, episode_number
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (user_id, details.tmdb_id, season, episode)
                        for season, episode in keys
                    ],
                )
            else:
                connection.executemany(
                    """
                    DELETE FROM episode_progress
                    WHERE user_id = ? AND tmdb_id = ?
                      AND season_number = ? AND episode_number = ?
                    """,
                    [
                        (user_id, details.tmdb_id, season, episode)
                        for season, episode in keys
                    ],
                )
            connection.execute(
                """
                UPDATE user_media_state
                SET user_name = ?, last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (user_name, chat_id, user_id, details.tmdb_id),
            )

        await self._write(operation)

    async def clear_series_progress(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            connection.execute(
                """
                DELETE FROM episode_progress
                WHERE user_id = ? AND tmdb_id = ?
                """,
                (user_id, details.tmdb_id),
            )
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = 'none',
                    user_name = ?,
                    last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (user_name, chat_id, user_id, details.tmdb_id),
            )

        await self._write(operation)

    async def watched_episode_keys(
        self, chat_id: int, user_id: int, tmdb_id: int
    ) -> set[tuple[int, int]]:
        def operation(connection: sqlite3.Connection) -> set[tuple[int, int]]:
            rows = connection.execute(
                """
                SELECT season_number, episode_number
                FROM episode_progress
                WHERE user_id = ? AND tmdb_id = ?
                """,
                (user_id, tmdb_id),
            ).fetchall()
            return {
                (int(row["season_number"]), int(row["episode_number"]))
                for row in rows
            }

        return await self._read(operation)

    async def sync_series_progress(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        released_keys: Iterable[tuple[int, int]],
    ) -> UserMediaState:
        released = set(released_keys)

        def operation(connection: sqlite3.Connection) -> UserMediaState:
            self._upsert_media(connection, details)
            existing = connection.execute(
                """
                SELECT viewing_status, watchlist
                FROM user_media_state
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (user_id, details.tmdb_id),
            ).fetchone()

            if existing is None:
                self._ensure_user_state(
                    connection,
                    chat_id,
                    details,
                    user_id,
                    user_name,
                )
            rows = connection.execute(
                """
                SELECT season_number, episode_number
                FROM episode_progress
                WHERE user_id = ? AND tmdb_id = ?
                """,
                (user_id, details.tmdb_id),
            ).fetchall()
            watched = {
                (int(row["season_number"]), int(row["episode_number"]))
                for row in rows
            }
            watched_released = watched & released if released else watched
            if not watched_released:
                status: ViewingStatus = "none"
            elif released and released.issubset(watched):
                status = "caught_up" if details.series_status == "ongoing" else "completed"
            else:
                status = "watching"

            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = ?,
                    watchlist = CASE WHEN ? != 'none' THEN 0 ELSE watchlist END,
                    user_name = ?,
                    last_chat_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                  AND (
                      viewing_status != ?
                      OR (? != 'none' AND watchlist != 0)
                  )
                """,
                (
                    status,
                    status,
                    user_name,
                    chat_id,
                    user_id,
                    details.tmdb_id,
                    status,
                    status,
                ),
            )
            row = connection.execute(
                """
                SELECT watchlist FROM user_media_state
                WHERE user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (user_id, details.tmdb_id),
            ).fetchone()
            return UserMediaState(
                watchlist=bool(row["watchlist"]),
                viewing_status=status,
                watched_episodes=len(watched),
            )

        return await self._write(operation)

    async def season_progress_counts(
        self, chat_id: int, user_id: int, tmdb_id: int
    ) -> dict[int, int]:
        def operation(connection: sqlite3.Connection) -> dict[int, int]:
            rows = connection.execute(
                """
                SELECT season_number, COUNT(*) AS count
                FROM episode_progress
                WHERE user_id = ? AND tmdb_id = ?
                GROUP BY season_number
                """,
                (user_id, tmdb_id),
            ).fetchall()
            return {
                int(row["season_number"]): int(row["count"])
                for row in rows
            }

        return await self._read(operation)

    # ------------------------------------------------------------------
    # Ratings and group suggestions
    # ------------------------------------------------------------------

    async def rate(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        rating: int,
    ) -> None:
        if not 1 <= rating <= 10:
            raise ValueError("rating must be between 1 and 10")

        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            connection.execute(
                """
                INSERT INTO ratings (
                    user_id, media_type, tmdb_id,
                    user_name, rating, rated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, media_type, tmdb_id)
                DO UPDATE SET user_name = excluded.user_name,
                              rating = excluded.rating,
                              rated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    details.media_type,
                    details.tmdb_id,
                    user_name,
                    rating,
                ),
            )

        await self._write(operation)

    async def toggle_suggestion(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            self._upsert_media(connection, details)
            row = connection.execute(
                """
                SELECT 1 FROM suggestions
                WHERE chat_id = ? AND media_type = ? AND tmdb_id = ? AND user_id = ?
                """,
                (chat_id, details.media_type, details.tmdb_id, user_id),
            ).fetchone()
            if row:
                connection.execute(
                    """
                    DELETE FROM suggestions
                    WHERE chat_id = ? AND media_type = ? AND tmdb_id = ? AND user_id = ?
                    """,
                    (chat_id, details.media_type, details.tmdb_id, user_id),
                )
                return False
            connection.execute(
                """
                INSERT INTO suggestions (
                    chat_id, media_type, tmdb_id, user_id, user_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, details.media_type, details.tmdb_id, user_id, user_name),
            )
            return True

        return await self._write(operation)

    async def group_rating(
        self, chat_id: int, media_type: MediaType, tmdb_id: int
    ) -> tuple[float | None, int]:
        def operation(connection: sqlite3.Connection) -> tuple[float | None, int]:
            row = connection.execute(
                """
                SELECT AVG(r.rating) AS average, COUNT(*) AS count
                FROM ratings r
                JOIN user_chats uc
                  ON uc.user_id = r.user_id
                WHERE uc.chat_id = ?
                  AND r.media_type = ?
                  AND r.tmdb_id = ?
                """,
                (chat_id, media_type, tmdb_id),
            ).fetchone()
            count = int(row["count"])
            return (float(row["average"]) if count else None, count)

        return await self._read(operation)

    # ------------------------------------------------------------------
    # Lists and group aggregates
    # ------------------------------------------------------------------



    async def store_release_snapshot(
        self,
        details: MediaDetails,
        *,
        region: str,
        checked_at: int,
        recover_future_checked_at: int | None = None,
    ) -> bool:
        """Cache metadata without modifying personal records.

        Clock recovery requires an explicitly observed future
        timestamp. A changed timestamp invalidates recovery.
        """

        if type(checked_at) is not int or checked_at < 0:
            raise ValueError("Invalid metadata check timestamp")

        if recover_future_checked_at is not None:
            if (
                type(recover_future_checked_at) is not int
                or recover_future_checked_at <= checked_at
            ):
                raise ValueError("Invalid future timestamp recovery")

        snapshot = build_release_snapshot(
            details,
            region=region,
        )

        events_json = json.dumps(
            [asdict(event) for event in snapshot.events],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                INSERT INTO release_cache (
                    media_type, tmdb_id, region,
                    checked_at, lifecycle, events_json
                )
                SELECT ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM media
                    WHERE media_type = ?
                      AND tmdb_id = ?
                )
                ON CONFLICT(media_type, tmdb_id, region)
                DO UPDATE SET
                    checked_at = excluded.checked_at,
                    lifecycle = excluded.lifecycle,
                    events_json = excluded.events_json
                WHERE (
                    ? IS NULL
                    AND excluded.checked_at
                        >= release_cache.checked_at
                )
                OR (
                    ? IS NOT NULL
                    AND release_cache.checked_at = ?
                    AND release_cache.checked_at
                        > excluded.checked_at
                )
                """,
                (
                    details.media_type,
                    details.tmdb_id,
                    region,
                    checked_at,
                    snapshot.lifecycle,
                    events_json,
                    details.media_type,
                    details.tmdb_id,
                    recover_future_checked_at,
                    recover_future_checked_at,
                    recover_future_checked_at,
                ),
            )

            return cursor.rowcount == 1

        return await self._write(operation)

    async def release_candidates(
        self,
        user_id: int,
        *,
        region: str,
    ) -> tuple[ReleaseCandidate, ...]:
        """Return only this user's followed media."""

        def operation(connection: sqlite3.Connection):
            rows = connection.execute(
                """
                SELECT m.media_type,
                       m.tmdb_id,
                       m.title,
                       m.media_class,
                       rc.checked_at,
                       rc.lifecycle,
                       rc.events_json
                FROM user_media_state ums
                JOIN media m USING (media_type, tmdb_id)
                LEFT JOIN release_cache rc
                    ON rc.media_type = m.media_type
                    AND rc.tmdb_id = m.tmdb_id
                    AND rc.region = ?
                WHERE ums.user_id = ?
                  AND (
                    ums.watchlist = 1
                    OR (
                        ums.media_type = 'tv'
                        AND ums.viewing_status IN (
                            'watching', 'caught_up'
                        )
                    )
                  )
                ORDER BY m.media_type, m.tmdb_id
                """,
                (region, user_id),
            ).fetchall()

            candidates = []

            for row in rows:
                snapshot = None

                if row["checked_at"] is not None:
                    snapshot = ReleaseSnapshot(
                        lifecycle=row["lifecycle"],
                        events=tuple(
                            ScheduledRelease(**event)
                            for event in json.loads(
                                row["events_json"]
                            )
                        ),
                    )

                candidates.append(
                    ReleaseCandidate(
                        media_type=row["media_type"],
                        tmdb_id=row["tmdb_id"],
                        title=row["title"],
                        media_class=row["media_class"],
                        checked_at=row["checked_at"],
                        snapshot=snapshot,
                    )
                )

            return tuple(candidates)

        return await self._read(operation)

    async def personal_library_page(
        self,
        chat_id: int,
        user_id: int,
        *,
        collection: str = "all",
        category: str = "all",
        sort: str = "updated",
        status: str = "all",
        page: int = 0,
        page_size: int = 8,
    ) -> PersonalLibraryPage:
        """Read one consistent page of the user's global library."""

        collections = {
            "all": "1 = 1",
            "watchlist": "watchlist = 1",
            "watched": (
                "viewing_status IN "
                "('watched', 'caught_up', 'completed')"
            ),
            "progress": "viewing_status != 'none'",
            "ratings": "rating IS NOT NULL",
        }

        documentary_classes = (
            "('documentary_film', 'documentary_series', "
            "'documentary_limited_series')"
        )

        categories = {
            "all": "1 = 1",
            "films": (
                "media_type = 'movie' AND media_class NOT IN "
                + documentary_classes
            ),
            "series": (
                "media_type = 'tv' AND media_class NOT IN "
                + documentary_classes
            ),
            "documentaries": (
                "media_class IN " + documentary_classes
            ),
        }

        sorts = {
            "title": "title COLLATE NOCASE ASC, year ASC",
            "updated": "julianday(updated_at) DESC",
            "added": "julianday(created_at) DESC",
            "year": "year DESC, title COLLATE NOCASE ASC",
            "rating": (
                "rating IS NULL ASC, rating DESC, "
                "title COLLATE NOCASE ASC"
            ),
        }

        statuses = {
            "all",
            "none",
            "watching",
            "watched",
            "caught_up",
            "completed",
        }

        if collection not in collections:
            raise ValueError("Invalid library collection")

        if category not in categories:
            raise ValueError("Invalid library category")

        if sort not in sorts:
            raise ValueError("Invalid library sort")

        if status not in statuses:
            raise ValueError("Invalid viewing status")

        if not isinstance(page, int) or page < 0:
            raise ValueError("Invalid page")

        if (
            not isinstance(page_size, int)
            or not 1 <= page_size <= 20
        ):
            raise ValueError("Invalid page size")

        filters = [
            collections[collection],
            categories[category],
        ]

        params = {
            "chat_id": chat_id,
            "user_id": user_id,
        }

        if status != "all":
            filters.append("viewing_status = :status")
            params["status"] = status

        where = " AND ".join(
            f"({item})" for item in filters
        )

        order = sorts[sort] + ", media_type ASC, tmdb_id ASC"

        if collection == "progress" and category == "all":
            order = (
                "CASE "
                "WHEN media_class LIKE 'documentary%' THEN 2 "
                "WHEN media_type = 'movie' THEN 0 "
                "ELSE 1 END ASC, "
                + order
            )

        base = """
            WITH identities AS (
                SELECT media_type, tmdb_id
                FROM user_media_state
                WHERE user_id = :user_id

                UNION

                SELECT media_type, tmdb_id
                FROM ratings
                WHERE user_id = :user_id
            ),
            library AS (
                SELECT
                    COALESCE(
                        ums.last_chat_id,
                        :chat_id
                    ) AS chat_id,
                    :user_id AS user_id,
                    m.media_type,
                    m.tmdb_id,
                    m.title,
                    m.year,
                    m.poster_url,
                    m.media_class,
                    COALESCE(ums.watchlist, 0) AS watchlist,
                    COALESCE(
                        ums.viewing_status, 'none'
                    ) AS viewing_status,
                    COALESCE(
                        ums.created_at, r.rated_at
                    ) AS created_at,
                    CASE
                        WHEN julianday(ums.updated_at) IS NULL
                            THEN COALESCE(
                                r.rated_at, ums.updated_at, ''
                            )
                        WHEN julianday(r.rated_at) IS NULL
                            THEN ums.updated_at
                        WHEN julianday(ums.updated_at)
                             >= julianday(r.rated_at)
                            THEN ums.updated_at
                        ELSE r.rated_at
                    END AS updated_at,
                    r.rating AS rating
                FROM identities i
                JOIN media m USING (media_type, tmdb_id)
                LEFT JOIN user_media_state ums
                    ON ums.user_id = :user_id
                    AND ums.media_type = m.media_type
                    AND ums.tmdb_id = m.tmdb_id
                LEFT JOIN ratings r
                    ON r.user_id = :user_id
                    AND r.media_type = m.media_type
                    AND r.tmdb_id = m.tmdb_id
            )
        """

        def operation(
            connection: sqlite3.Connection,
        ) -> PersonalLibraryPage:

            # Count and page must use the same SQLite snapshot.
            connection.execute("BEGIN")

            total = connection.execute(
                base + f"SELECT COUNT(*) FROM library WHERE {where}",
                params,
            ).fetchone()[0]

            page_count = max(
                1,
                (total + page_size - 1) // page_size,
            )

            current_page = min(page, page_count - 1)

            page_params = {
                **params,
                "limit": page_size,
                "offset": current_page * page_size,
            }

            rows = connection.execute(
                base + f"""
                SELECT library.*,
                       CASE WHEN media_type = 'tv' THEN (
                           SELECT COUNT(*)
                           FROM episode_progress ep
                           WHERE ep.user_id = :user_id
                             AND ep.tmdb_id = library.tmdb_id
                       ) ELSE 0 END AS watched_episodes
                FROM library
                WHERE {where}
                ORDER BY {order}
                LIMIT :limit OFFSET :offset
                """,
                page_params,
            ).fetchall()

            entries = tuple(
                replace(
                    self._personal_from_row(row),
                    rating=row["rating"],
                )
                for row in rows
            )

            return PersonalLibraryPage(
                entries=entries,
                total=total,
                page=current_page,
                page_size=page_size,
            )

        return await self._read(operation)


    async def list_personal_watchlist(
        self, chat_id: int, user_id: int, *, limit: int = 50
    ) -> list[PersonalEntry]:
        return await self._list_personal(
            chat_id, user_id, "ums.watchlist = 1", limit=limit
        )

    async def list_personal_watched(
        self, chat_id: int, user_id: int, *, limit: int = 50
    ) -> list[PersonalEntry]:
        return await self._list_personal(
            chat_id,
            user_id,
            "ums.viewing_status IN ('watched', 'caught_up', 'completed')",
            limit=limit,
        )

    async def list_personal_progress(
        self, chat_id: int, user_id: int, *, limit: int = 50
    ) -> list[PersonalEntry]:
        return await self._list_personal(
            chat_id,
            user_id,
            "ums.viewing_status != 'none'",
            limit=limit,
        )

    async def _list_personal(
        self,
        chat_id: int,
        user_id: int,
        where: str,
        *,
        limit: int,
    ) -> list[PersonalEntry]:
        def operation(connection: sqlite3.Connection) -> list[PersonalEntry]:
            rows = connection.execute(
                f"""
                SELECT COALESCE(ums.last_chat_id, ?) AS chat_id,
                       ums.user_id, ums.media_type, ums.tmdb_id,
                       m.title, m.year, m.poster_url, m.media_class,
                       ums.watchlist, ums.viewing_status, ums.updated_at,
                       CASE WHEN ums.media_type = 'tv' THEN (
                           SELECT COUNT(*) FROM episode_progress ep
                           WHERE ep.user_id = ums.user_id
                             AND ep.tmdb_id = ums.tmdb_id
                       ) ELSE 0 END AS watched_episodes
                FROM user_media_state ums
                JOIN media m USING (media_type, tmdb_id)
                WHERE ums.user_id = ? AND {where}
                ORDER BY ums.updated_at DESC
                LIMIT ?
                """,
                (chat_id, user_id, limit),
            ).fetchall()
            return [self._personal_from_row(row) for row in rows]

        return await self._read(operation)

    async def list_group_watchlist(
        self, chat_id: int, *, limit: int = 50
    ) -> list[GroupEntry]:
        def operation(connection: sqlite3.Connection) -> list[GroupEntry]:
            rows = connection.execute(
                """
                SELECT uc.chat_id, ums.media_type, ums.tmdb_id,
                       m.title, m.year, m.poster_url, m.media_class,
                       COUNT(DISTINCT ums.user_id) AS member_count,
                       GROUP_CONCAT(DISTINCT uc.user_name) AS member_names,
                       rating.average AS group_rating,
                       COALESCE(rating.count, 0) AS rating_count,
                       MAX(ums.updated_at) AS newest
                FROM user_chats uc
                JOIN user_media_state ums
                  ON ums.user_id = uc.user_id
                JOIN media m USING (media_type, tmdb_id)
                LEFT JOIN (
                    SELECT uc2.chat_id, r.media_type, r.tmdb_id,
                           AVG(r.rating) AS average,
                           COUNT(*) AS count
                    FROM user_chats uc2
                    JOIN ratings r
                      ON r.user_id = uc2.user_id
                    GROUP BY uc2.chat_id, r.media_type, r.tmdb_id
                ) rating
                  ON rating.chat_id = uc.chat_id
                 AND rating.media_type = ums.media_type
                 AND rating.tmdb_id = ums.tmdb_id
                WHERE uc.chat_id = ?
                  AND ums.watchlist = 1
                GROUP BY uc.chat_id, ums.media_type, ums.tmdb_id
                ORDER BY member_count DESC, newest DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
            return [self._group_from_row(row) for row in rows]

        return await self._read(operation)

    async def random_group_watchlist(self, chat_id: int) -> GroupEntry | None:
        entries = await self.list_group_watchlist(chat_id, limit=500)
        return random.choice(entries) if entries else None

    async def list_group_suggestions(
        self, chat_id: int, *, limit: int = 50
    ) -> list[SuggestionEntry]:
        def operation(connection: sqlite3.Connection) -> list[SuggestionEntry]:
            rows = connection.execute(
                """
                SELECT s.chat_id, s.media_type, s.tmdb_id,
                       m.title, m.year, m.media_class,
                       COUNT(DISTINCT s.user_id) AS suggested_count,
                       GROUP_CONCAT(DISTINCT s.user_name) AS suggested_by,
                       MAX(s.suggested_at) AS suggested_at
                FROM suggestions s
                JOIN media m USING (media_type, tmdb_id)
                WHERE s.chat_id = ?
                GROUP BY s.chat_id, s.media_type, s.tmdb_id
                ORDER BY suggested_count DESC, suggested_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
            return [self._suggestion_from_row(row) for row in rows]

        return await self._read(operation)

    async def list_my_suggestions(
        self, chat_id: int, user_id: int, *, limit: int = 50
    ) -> list[SuggestionEntry]:
        def operation(connection: sqlite3.Connection) -> list[SuggestionEntry]:
            rows = connection.execute(
                """
                SELECT s.chat_id, s.media_type, s.tmdb_id,
                       m.title, m.year, m.media_class,
                       1 AS suggested_count, s.user_name AS suggested_by,
                       s.suggested_at
                FROM suggestions s
                JOIN media m USING (media_type, tmdb_id)
                WHERE s.chat_id = ? AND s.user_id = ?
                ORDER BY s.suggested_at DESC
                LIMIT ?
                """,
                (chat_id, user_id, limit),
            ).fetchall()
            return [self._suggestion_from_row(row) for row in rows]

        return await self._read(operation)

    # ------------------------------------------------------------------
    # Schema helpers and migrations
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_media_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(media)").fetchall()
        }
        additions = {
            "media_class": "TEXT NOT NULL DEFAULT 'film'",
            "series_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "season_count": "INTEGER NOT NULL DEFAULT 0",
            "episode_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE media ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_known_chats_type_active
            ON known_chats(chat_type, is_active, last_seen_at);

            CREATE INDEX IF NOT EXISTS idx_user_watchlist
            ON user_media_state(user_id, watchlist, updated_at);

            CREATE INDEX IF NOT EXISTS idx_user_status
            ON user_media_state(user_id, viewing_status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_episode_progress_user
            ON episode_progress(user_id, tmdb_id, season_number);

            CREATE INDEX IF NOT EXISTS idx_user_chats_chat
            ON user_chats(chat_id, user_id, last_seen_at);

            CREATE INDEX IF NOT EXISTS idx_ratings_media
            ON ratings(media_type, tmdb_id, user_id);

            CREATE INDEX IF NOT EXISTS idx_suggestions_group
            ON suggestions(chat_id, suggested_at);
            """
        )

    @staticmethod
    def _table_columns(
        connection: sqlite3.Connection, table_name: str
    ) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
        }

    @staticmethod
    def _migration_applied(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _mark_migration(connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
        )

    @classmethod
    def _migrate_global_user_state(cls, connection: sqlite3.Connection) -> None:
        migration = "personal_state_global_by_user_v1"
        if cls._migration_applied(connection, migration):
            return

        state_columns = cls._table_columns(connection, "user_media_state")
        episode_columns = cls._table_columns(connection, "episode_progress")
        rating_columns = cls._table_columns(connection, "ratings")

        state_is_legacy = "chat_id" in state_columns
        episode_is_legacy = "chat_id" in episode_columns
        ratings_are_legacy = "chat_id" in rating_columns

        # Preserve group membership/interaction knowledge before chat_id is
        # removed from personal state.
        if state_is_legacy:
            connection.execute(
                """
                INSERT OR IGNORE INTO user_chats (
                    chat_id, user_id, user_name, first_seen_at, last_seen_at
                )
                SELECT
                    ums.chat_id,
                    ums.user_id,
                    MAX(ums.user_name),
                    MIN(ums.created_at),
                    MAX(ums.updated_at)
                FROM user_media_state ums
                JOIN known_chats kc ON kc.chat_id = ums.chat_id
                WHERE kc.chat_type IN ('group', 'supergroup')
                GROUP BY ums.chat_id, ums.user_id
                """
            )

        if ratings_are_legacy:
            connection.execute(
                """
                INSERT OR IGNORE INTO user_chats (
                    chat_id, user_id, user_name, first_seen_at, last_seen_at
                )
                SELECT
                    r.chat_id,
                    r.user_id,
                    MAX(r.user_name),
                    MIN(r.rated_at),
                    MAX(r.rated_at)
                FROM ratings r
                JOIN known_chats kc ON kc.chat_id = r.chat_id
                WHERE kc.chat_type IN ('group', 'supergroup')
                GROUP BY r.chat_id, r.user_id
                """
            )

        connection.execute(
            """
            INSERT OR IGNORE INTO user_chats (
                chat_id, user_id, user_name, first_seen_at, last_seen_at
            )
            SELECT
                s.chat_id,
                s.user_id,
                MAX(s.user_name),
                MIN(s.suggested_at),
                MAX(s.suggested_at)
            FROM suggestions s
            JOIN known_chats kc ON kc.chat_id = s.chat_id
            WHERE kc.chat_type IN ('group', 'supergroup')
            GROUP BY s.chat_id, s.user_id
            """
        )

        if state_is_legacy:
            connection.execute(
                """
                CREATE TABLE user_media_state_v2 (
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL
                        CHECK (media_type IN ('movie', 'tv')),
                    tmdb_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    watchlist INTEGER NOT NULL DEFAULT 0
                        CHECK (watchlist IN (0, 1)),
                    viewing_status TEXT NOT NULL DEFAULT 'none'
                        CHECK (
                            viewing_status IN (
                                'none', 'watching', 'watched',
                                'caught_up', 'completed'
                            )
                        ),
                    last_chat_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, media_type, tmdb_id),
                    FOREIGN KEY (media_type, tmdb_id)
                        REFERENCES media(media_type, tmdb_id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                WITH merged AS (
                    SELECT
                        user_id,
                        media_type,
                        tmdb_id,
                        MAX(watchlist) AS any_watchlist,
                        CASE
                            WHEN media_type = 'movie'
                                 AND MAX(
                                     CASE WHEN viewing_status = 'watched'
                                          THEN 1 ELSE 0 END
                                 ) = 1
                                THEN 'watched'
                            WHEN media_type = 'tv'
                                 AND MAX(
                                     CASE WHEN viewing_status = 'completed'
                                          THEN 1 ELSE 0 END
                                 ) = 1
                                THEN 'completed'
                            WHEN media_type = 'tv'
                                 AND MAX(
                                     CASE WHEN viewing_status = 'caught_up'
                                          THEN 1 ELSE 0 END
                                 ) = 1
                                THEN 'caught_up'
                            WHEN media_type = 'tv'
                                 AND MAX(
                                     CASE WHEN viewing_status = 'watching'
                                          THEN 1 ELSE 0 END
                                 ) = 1
                                THEN 'watching'
                            ELSE 'none'
                        END AS merged_status,
                        MIN(created_at) AS created_at,
                        MAX(updated_at) AS updated_at
                    FROM user_media_state
                    GROUP BY user_id, media_type, tmdb_id
                )
                INSERT INTO user_media_state_v2 (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    watchlist,
                    viewing_status,
                    last_chat_id,
                    created_at,
                    updated_at
                )
                SELECT
                    m.user_id,
                    m.media_type,
                    m.tmdb_id,
                    (
                        SELECT s.user_name
                        FROM user_media_state s
                        WHERE s.user_id = m.user_id
                          AND s.media_type = m.media_type
                          AND s.tmdb_id = m.tmdb_id
                        ORDER BY s.updated_at DESC, s.chat_id DESC
                        LIMIT 1
                    ),
                    CASE
                        WHEN m.merged_status = 'none'
                            THEN m.any_watchlist
                        ELSE 0
                    END,
                    m.merged_status,
                    (
                        SELECT s.chat_id
                        FROM user_media_state s
                        WHERE s.user_id = m.user_id
                          AND s.media_type = m.media_type
                          AND s.tmdb_id = m.tmdb_id
                        ORDER BY s.updated_at DESC, s.chat_id DESC
                        LIMIT 1
                    ),
                    m.created_at,
                    m.updated_at
                FROM merged m
                """
            )

            connection.execute("DROP TABLE user_media_state")
            connection.execute(
                "ALTER TABLE user_media_state_v2 RENAME TO user_media_state"
            )

        if episode_is_legacy:
            connection.execute(
                """
                CREATE TABLE episode_progress_v2 (
                    user_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    season_number INTEGER NOT NULL,
                    episode_number INTEGER NOT NULL,
                    watched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (
                        user_id, tmdb_id, season_number, episode_number
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO episode_progress_v2 (
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    watched_at
                )
                SELECT
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                    MAX(watched_at)
                FROM episode_progress
                GROUP BY
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number
                """
            )
            connection.execute("DROP TABLE episode_progress")
            connection.execute(
                "ALTER TABLE episode_progress_v2 RENAME TO episode_progress"
            )

        if ratings_are_legacy:
            connection.execute(
                """
                CREATE TABLE ratings_v2 (
                    user_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL
                        CHECK (media_type IN ('movie', 'tv')),
                    tmdb_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
                    rated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, media_type, tmdb_id),
                    FOREIGN KEY (media_type, tmdb_id)
                        REFERENCES media(media_type, tmdb_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ratings_v2 (
                    user_id,
                    media_type,
                    tmdb_id,
                    user_name,
                    rating,
                    rated_at
                )
                SELECT
                    r.user_id,
                    r.media_type,
                    r.tmdb_id,
                    r.user_name,
                    r.rating,
                    r.rated_at
                FROM ratings r
                WHERE r.rowid = (
                    SELECT r2.rowid
                    FROM ratings r2
                    WHERE r2.user_id = r.user_id
                      AND r2.media_type = r.media_type
                      AND r2.tmdb_id = r.tmdb_id
                    ORDER BY r2.rated_at DESC, r2.rowid DESC
                    LIMIT 1
                )
                """
            )
            connection.execute("DROP TABLE ratings")
            connection.execute("ALTER TABLE ratings_v2 RENAME TO ratings")

        cls._mark_migration(connection, migration)

    @classmethod
    def _migrate_legacy_group_state(
        cls, connection: sqlite3.Connection
    ) -> None:
        migration = "legacy_group_catalog_to_personal_v1"
        if cls._migration_applied(connection, migration):
            return

        # New installations already use global user state. group_catalog is
        # retained only for compatibility with databases created by older
        # Kokabiel versions.
        if "chat_id" not in cls._table_columns(
            connection, "user_media_state"
        ):
            cls._mark_migration(connection, migration)
            return

        connection.execute(
            """
            INSERT OR IGNORE INTO user_media_state (
                chat_id, user_id, media_type, tmdb_id, user_name,
                watchlist, viewing_status, created_at, updated_at
            )
            SELECT gc.chat_id, gc.added_by, gc.media_type, gc.tmdb_id,
                   gc.added_by_name, gc.watchlist,
                   CASE
                       WHEN gc.watched = 0 THEN 'none'
                       WHEN gc.media_type = 'movie' THEN 'watched'
                       ELSE 'completed'
                   END,
                   gc.added_at, gc.added_at
            FROM group_catalog gc
            """
        )
        cls._mark_migration(connection, migration)

    @classmethod
    def _migrate_recommendations(cls, connection: sqlite3.Connection) -> None:
        migration = "legacy_recommendations_to_suggestions_v1"
        if cls._migration_applied(connection, migration):
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO suggestions (
                chat_id, media_type, tmdb_id, user_id, user_name, suggested_at
            )
            SELECT chat_id, media_type, tmdb_id, user_id, user_name, recommended_at
            FROM recommendations
            """
        )
        cls._mark_migration(connection, migration)

    @staticmethod
    def _upsert_media(connection: sqlite3.Connection, details: MediaDetails) -> None:
        connection.execute(
            """
            INSERT INTO media (
                media_type, tmdb_id, title, year, poster_url,
                media_class, series_status, season_count, episode_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(media_type, tmdb_id)
            DO UPDATE SET title = excluded.title,
                          year = excluded.year,
                          poster_url = excluded.poster_url,
                          media_class = excluded.media_class,
                          series_status = excluded.series_status,
                          season_count = excluded.season_count,
                          episode_count = excluded.episode_count,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (
                details.media_type,
                details.tmdb_id,
                details.title,
                details.year,
                details.poster_url,
                details.media_class,
                details.series_status,
                details.number_of_seasons,
                details.number_of_episodes,
            ),
        )

    @staticmethod
    def _ensure_user_state(
        connection: sqlite3.Connection,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_media_state (
                user_id, media_type, tmdb_id,
                user_name, last_chat_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, media_type, tmdb_id)
            DO UPDATE SET user_name = excluded.user_name,
                          last_chat_id = excluded.last_chat_id
            """,
            (
                user_id,
                details.media_type,
                details.tmdb_id,
                user_name,
                chat_id,
            ),
        )

    @staticmethod
    def _personal_from_row(row: sqlite3.Row) -> PersonalEntry:
        return PersonalEntry(
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]),
            media_type=str(row["media_type"]),
            tmdb_id=int(row["tmdb_id"]),
            title=str(row["title"]),
            year=str(row["year"]),
            media_class=str(row["media_class"]),
            poster_url=row["poster_url"],
            watchlist=bool(row["watchlist"]),
            viewing_status=str(row["viewing_status"]),
            updated_at=str(row["updated_at"]),
            watched_episodes=int(row["watched_episodes"]),
        )

    @staticmethod
    def _group_from_row(row: sqlite3.Row) -> GroupEntry:
        names = tuple(
            name.strip()
            for name in str(row["member_names"] or "").split(",")
            if name.strip()
        )
        rating_count = int(row["rating_count"])
        return GroupEntry(
            chat_id=int(row["chat_id"]),
            media_type=str(row["media_type"]),
            tmdb_id=int(row["tmdb_id"]),
            title=str(row["title"]),
            year=str(row["year"]),
            media_class=str(row["media_class"]),
            poster_url=row["poster_url"],
            member_count=int(row["member_count"]),
            member_names=names,
            group_rating=float(row["group_rating"]) if rating_count else None,
            rating_count=rating_count,
        )

    @staticmethod
    def _suggestion_from_row(row: sqlite3.Row) -> SuggestionEntry:
        names = tuple(
            name.strip()
            for name in str(row["suggested_by"] or "").split(",")
            if name.strip()
        )
        return SuggestionEntry(
            chat_id=int(row["chat_id"]),
            media_type=str(row["media_type"]),
            tmdb_id=int(row["tmdb_id"]),
            title=str(row["title"]),
            year=str(row["year"]),
            media_class=str(row["media_class"]),
            suggested_count=int(row["suggested_count"]),
            suggested_by=names,
            suggested_at=str(row["suggested_at"]),
        )
