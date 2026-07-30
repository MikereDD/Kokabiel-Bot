from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

from models import (
    GroupEntry,
    KnownChat,
    MediaClass,
    MediaDetails,
    MediaType,
    PersonalEntry,
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
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    tmdb_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    watchlist INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
    viewing_status TEXT NOT NULL DEFAULT 'none'
        CHECK (viewing_status IN ('none', 'watching', 'watched', 'caught_up', 'completed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, user_id, media_type, tmdb_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_progress (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    watched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, user_id, tmdb_id, season_number, episode_number)
);

CREATE TABLE IF NOT EXISTS ratings (
    chat_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
    rated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, media_type, tmdb_id, user_id),
    FOREIGN KEY (media_type, tmdb_id) REFERENCES media(media_type, tmdb_id)
        ON DELETE CASCADE
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

CREATE INDEX IF NOT EXISTS idx_known_chats_type_active
ON known_chats(chat_type, is_active, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_user_watchlist
ON user_media_state(chat_id, user_id, watchlist, updated_at);

CREATE INDEX IF NOT EXISTS idx_user_status
ON user_media_state(chat_id, user_id, viewing_status, updated_at);

CREATE INDEX IF NOT EXISTS idx_episode_progress_user
ON episode_progress(chat_id, user_id, tmdb_id, season_number);

CREATE INDEX IF NOT EXISTS idx_suggestions_group
ON suggestions(chat_id, suggested_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_media_columns(connection)
            self._migrate_legacy_group_state(connection)
            self._migrate_recommendations(connection)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:
            return await asyncio.to_thread(self._run_write_sync, operation)

    def _run_write_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._connect() as connection:
            result = operation(connection)
            connection.commit()
            return result

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run_read_sync, operation)

    def _run_read_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._connect() as connection:
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
                           WHERE ep.chat_id = ums.chat_id
                             AND ep.user_id = ums.user_id
                             AND ep.tmdb_id = ums.tmdb_id
                       ) ELSE 0 END AS watched_episodes
                FROM user_media_state ums
                WHERE ums.chat_id = ? AND ums.user_id = ?
                  AND ums.media_type = ? AND ums.tmdb_id = ?
                """,
                (chat_id, user_id, media_type, tmdb_id),
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
                WHERE chat_id = ? AND user_id = ?
                  AND media_type = ? AND tmdb_id = ?
                """,
                (chat_id, user_id, details.media_type, details.tmdb_id),
            ).fetchone()
            enabled = not bool(row["watchlist"])
            connection.execute(
                """
                UPDATE user_media_state
                SET watchlist = ?, user_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
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
                WHERE chat_id = ? AND user_id = ?
                  AND media_type = 'movie' AND tmdb_id = ?
                """,
                (chat_id, user_id, details.tmdb_id),
            ).fetchone()
            enabled = str(row["viewing_status"]) != "watched"
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = ?,
                    watchlist = CASE WHEN ? = 1 THEN 0 ELSE watchlist END,
                    user_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
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
                    user_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
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
                        chat_id, user_id, tmdb_id, season_number, episode_number
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (chat_id, user_id, details.tmdb_id, season, episode)
                        for season, episode in keys
                    ],
                )
            else:
                connection.executemany(
                    """
                    DELETE FROM episode_progress
                    WHERE chat_id = ? AND user_id = ? AND tmdb_id = ?
                      AND season_number = ? AND episode_number = ?
                    """,
                    [
                        (chat_id, user_id, details.tmdb_id, season, episode)
                        for season, episode in keys
                    ],
                )
            connection.execute(
                """
                UPDATE user_media_state
                SET user_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
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
                WHERE chat_id = ? AND user_id = ? AND tmdb_id = ?
                """,
                (chat_id, user_id, details.tmdb_id),
            )
            connection.execute(
                """
                UPDATE user_media_state
                SET viewing_status = 'none', user_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
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
                WHERE chat_id = ? AND user_id = ? AND tmdb_id = ?
                """,
                (chat_id, user_id, tmdb_id),
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
            self._ensure_user_state(connection, chat_id, details, user_id, user_name)
            rows = connection.execute(
                """
                SELECT season_number, episode_number
                FROM episode_progress
                WHERE chat_id = ? AND user_id = ? AND tmdb_id = ?
                """,
                (chat_id, user_id, details.tmdb_id),
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
                    user_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (status, status, user_name, chat_id, user_id, details.tmdb_id),
            )
            row = connection.execute(
                """
                SELECT watchlist FROM user_media_state
                WHERE chat_id = ? AND user_id = ?
                  AND media_type = 'tv' AND tmdb_id = ?
                """,
                (chat_id, user_id, details.tmdb_id),
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
                WHERE chat_id = ? AND user_id = ? AND tmdb_id = ?
                GROUP BY season_number
                """,
                (chat_id, user_id, tmdb_id),
            ).fetchall()
            return {int(row["season_number"]): int(row["count"]) for row in rows}

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
                    chat_id, media_type, tmdb_id, user_id, user_name, rating, rated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, media_type, tmdb_id, user_id)
                DO UPDATE SET user_name = excluded.user_name,
                              rating = excluded.rating,
                              rated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, details.media_type, details.tmdb_id, user_id, user_name, rating),
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
                SELECT AVG(rating) AS average, COUNT(*) AS count
                FROM ratings
                WHERE chat_id = ? AND media_type = ? AND tmdb_id = ?
                """,
                (chat_id, media_type, tmdb_id),
            ).fetchone()
            count = int(row["count"])
            return (float(row["average"]) if count else None, count)

        return await self._read(operation)

    # ------------------------------------------------------------------
    # Lists and group aggregates
    # ------------------------------------------------------------------

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
            "ums.media_type = 'tv' AND ums.viewing_status != 'none'",
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
                SELECT ums.chat_id, ums.user_id, ums.media_type, ums.tmdb_id,
                       m.title, m.year, m.poster_url, m.media_class,
                       ums.watchlist, ums.viewing_status, ums.updated_at,
                       CASE WHEN ums.media_type = 'tv' THEN (
                           SELECT COUNT(*) FROM episode_progress ep
                           WHERE ep.chat_id = ums.chat_id
                             AND ep.user_id = ums.user_id
                             AND ep.tmdb_id = ums.tmdb_id
                       ) ELSE 0 END AS watched_episodes
                FROM user_media_state ums
                JOIN media m USING (media_type, tmdb_id)
                WHERE ums.chat_id = ? AND ums.user_id = ? AND {where}
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
                SELECT ums.chat_id, ums.media_type, ums.tmdb_id,
                       m.title, m.year, m.poster_url, m.media_class,
                       COUNT(DISTINCT ums.user_id) AS member_count,
                       GROUP_CONCAT(DISTINCT ums.user_name) AS member_names,
                       rating.average AS group_rating,
                       COALESCE(rating.count, 0) AS rating_count,
                       MAX(ums.updated_at) AS newest
                FROM user_media_state ums
                JOIN media m USING (media_type, tmdb_id)
                LEFT JOIN (
                    SELECT chat_id, media_type, tmdb_id,
                           AVG(rating) AS average, COUNT(*) AS count
                    FROM ratings
                    GROUP BY chat_id, media_type, tmdb_id
                ) rating
                  ON rating.chat_id = ums.chat_id
                 AND rating.media_type = ums.media_type
                 AND rating.tmdb_id = ums.tmdb_id
                WHERE ums.chat_id = ? AND ums.watchlist = 1
                GROUP BY ums.chat_id, ums.media_type, ums.tmdb_id
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
    def _migrate_legacy_group_state(cls, connection: sqlite3.Connection) -> None:
        migration = "legacy_group_catalog_to_personal_v1"
        if cls._migration_applied(connection, migration):
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
                chat_id, user_id, media_type, tmdb_id, user_name
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id, media_type, tmdb_id)
            DO UPDATE SET user_name = excluded.user_name
            """,
            (chat_id, user_id, details.media_type, details.tmdb_id, user_name),
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
