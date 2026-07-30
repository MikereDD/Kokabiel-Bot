from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from models import CatalogEntry, KnownChat, MediaDetails, MediaType

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
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (media_type, tmdb_id)
);

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

CREATE INDEX IF NOT EXISTS idx_catalog_watchlist
ON group_catalog(chat_id, watchlist, added_at);

CREATE INDEX IF NOT EXISTS idx_catalog_watched
ON group_catalog(chat_id, watched, added_at);
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

    async def set_watchlist(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        enabled: bool = True,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            self._ensure_catalog(connection, chat_id, details, user_id, user_name)
            connection.execute(
                """
                UPDATE group_catalog
                SET watchlist = ?, watched = CASE WHEN ? = 1 THEN 0 ELSE watched END
                WHERE chat_id = ? AND media_type = ? AND tmdb_id = ?
                """,
                (int(enabled), int(enabled), chat_id, details.media_type, details.tmdb_id),
            )

        await self._write(operation)

    async def set_watched(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
        enabled: bool = True,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._upsert_media(connection, details)
            self._ensure_catalog(connection, chat_id, details, user_id, user_name)
            connection.execute(
                """
                UPDATE group_catalog
                SET watched = ?, watchlist = CASE WHEN ? = 1 THEN 0 ELSE watchlist END
                WHERE chat_id = ? AND media_type = ? AND tmdb_id = ?
                """,
                (int(enabled), int(enabled), chat_id, details.media_type, details.tmdb_id),
            )

        await self._write(operation)

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
            self._ensure_catalog(connection, chat_id, details, user_id, user_name)
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

    async def recommend(
        self,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            self._upsert_media(connection, details)
            self._ensure_catalog(connection, chat_id, details, user_id, user_name)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO recommendations (
                    chat_id, media_type, tmdb_id, user_id, user_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, details.media_type, details.tmdb_id, user_id, user_name),
            )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def catalog_state(
        self, chat_id: int, media_type: MediaType, tmdb_id: int
    ) -> tuple[bool, bool]:
        def operation(connection: sqlite3.Connection) -> tuple[bool, bool]:
            row = connection.execute(
                """
                SELECT watchlist, watched
                FROM group_catalog
                WHERE chat_id = ? AND media_type = ? AND tmdb_id = ?
                """,
                (chat_id, media_type, tmdb_id),
            ).fetchone()
            if row is None:
                return False, False
            return bool(row["watchlist"]), bool(row["watched"])

        return await self._read(operation)

    async def list_entries(
        self, chat_id: int, *, watchlist: bool = False, watched: bool = False, limit: int = 50
    ) -> list[CatalogEntry]:
        if watchlist == watched:
            raise ValueError("choose exactly one catalog state")
        column = "watchlist" if watchlist else "watched"

        def operation(connection: sqlite3.Connection) -> list[CatalogEntry]:
            rows = connection.execute(
                f"""
                SELECT gc.chat_id, gc.media_type, gc.tmdb_id, m.title, m.year, m.poster_url,
                       gc.added_by, gc.added_by_name, gc.added_at,
                       gc.watchlist, gc.watched,
                       COALESCE(AVG(r.rating), 0) AS group_rating,
                       COUNT(DISTINCT r.user_id) AS rating_count,
                       GROUP_CONCAT(DISTINCT rec.user_name) AS recommended_by
                FROM group_catalog gc
                JOIN media m USING (media_type, tmdb_id)
                LEFT JOIN ratings r
                  ON r.chat_id = gc.chat_id
                 AND r.media_type = gc.media_type
                 AND r.tmdb_id = gc.tmdb_id
                LEFT JOIN recommendations rec
                  ON rec.chat_id = gc.chat_id
                 AND rec.media_type = gc.media_type
                 AND rec.tmdb_id = gc.tmdb_id
                WHERE gc.chat_id = ? AND gc.{column} = 1
                GROUP BY gc.chat_id, gc.media_type, gc.tmdb_id
                ORDER BY gc.added_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
            return [self._entry_from_row(row) for row in rows]

        return await self._read(operation)

    async def random_watchlist(self, chat_id: int) -> CatalogEntry | None:
        entries = await self.list_entries(chat_id, watchlist=True, limit=500)
        return random.choice(entries) if entries else None

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

    @staticmethod
    def _upsert_media(connection: sqlite3.Connection, details: MediaDetails) -> None:
        connection.execute(
            """
            INSERT INTO media (media_type, tmdb_id, title, year, poster_url, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(media_type, tmdb_id)
            DO UPDATE SET title = excluded.title,
                          year = excluded.year,
                          poster_url = excluded.poster_url,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (
                details.media_type,
                details.tmdb_id,
                details.title,
                details.year,
                details.poster_url,
            ),
        )

    @staticmethod
    def _ensure_catalog(
        connection: sqlite3.Connection,
        chat_id: int,
        details: MediaDetails,
        user_id: int,
        user_name: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO group_catalog (
                chat_id, media_type, tmdb_id, added_by, added_by_name
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, details.media_type, details.tmdb_id, user_id, user_name),
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> CatalogEntry:
        recommended = tuple(
            name for name in str(row["recommended_by"] or "").split(",") if name
        )
        rating_count = int(row["rating_count"])
        return CatalogEntry(
            chat_id=int(row["chat_id"]),
            media_type=row["media_type"],
            tmdb_id=int(row["tmdb_id"]),
            title=str(row["title"]),
            year=str(row["year"]),
            poster_url=row["poster_url"],
            added_by=int(row["added_by"]),
            added_by_name=str(row["added_by_name"]),
            added_at=str(row["added_at"]),
            watchlist=bool(row["watchlist"]),
            watched=bool(row["watched"]),
            recommended_by=recommended,
            group_rating=float(row["group_rating"]) if rating_count else None,
            rating_count=rating_count,
        )
