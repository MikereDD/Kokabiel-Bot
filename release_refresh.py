from __future__ import annotations

import asyncio
import time

from dataclasses import dataclass

from release_intelligence import REFRESH_BATCH_SIZE
from tmdb import TmdbError


RELEASE_REFRESH_ACTIVE_KEY = "kokabiel_release_refresh_active"
RELEASE_REFRESH_WORKER_KEY = "kokabiel_release_refresh_worker"

RETRY_DELAY = 300
BATCH_START_BUDGET = 30
REFRESH_ALL_MAX_BATCHES = 50
REFRESH_ALL_START_BUDGET = 600


@dataclass(frozen=True, slots=True)
class RefreshResult:
    eligible: int = 0
    stale: int = 0
    attempted: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0
    stop_reason: str | None = None


class ReleaseRefreshWorker:

    def __init__(
        self,
        database,
        client,
        *,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self._database = database
        self._client = client
        self._clock = clock
        self._monotonic = monotonic

        self._lock = asyncio.Lock()
        self._retry_after = {}
        self._paused_until = 0.0

    async def refresh(
        self,
        user_id: int,
        *,
        region: str,
    ) -> RefreshResult:

        if self._lock.locked():
            return RefreshResult(stop_reason="busy")

        async with self._lock:
            return await self._refresh(
                user_id,
                region=region,
            )

    async def refresh_all(
        self,
        user_id: int,
        *,
        region: str,
        should_continue=None,
        on_progress=None,
    ) -> RefreshResult:
        """Refresh multiple bounded batches under one reservation."""

        if self._lock.locked():
            return RefreshResult(stop_reason="busy")

        async with self._lock:
            started = self._monotonic()

            eligible = 0
            stale = 0
            attempted = 0
            updated = 0
            failed = 0
            skipped = 0
            stop_reason = None
            attempted_keys = set()

            for batch in range(1, REFRESH_ALL_MAX_BATCHES + 1):
                if (
                    should_continue is not None
                    and not should_continue()
                ):
                    stop_reason = "shutdown"
                    break

                if (
                    self._monotonic() - started
                    >= REFRESH_ALL_START_BUDGET
                ):
                    stop_reason = "overall_time_budget"
                    break

                result = await self._refresh(
                    user_id,
                    region=region,
                    attempted_keys=attempted_keys,
                )

                eligible = result.eligible
                stale = result.stale
                attempted += result.attempted
                updated += result.updated
                failed += result.failed
                skipped += result.skipped

                if on_progress is not None:
                    await on_progress(
                        batch,
                        RefreshResult(
                            eligible=eligible,
                            stale=stale,
                            attempted=attempted,
                            updated=updated,
                            failed=failed,
                            skipped=skipped,
                            stop_reason=result.stop_reason,
                        ),
                    )

                if result.stop_reason in {
                    "paused",
                    "rate_limited",
                    "authorization",
                }:
                    stop_reason = result.stop_reason
                    break

                if not result.attempted:
                    break

                if result.stop_reason not in {
                    "batch_limit",
                    "time_budget",
                }:
                    break

            else:
                stop_reason = "overall_batch_limit"

            candidates = await self._database.release_candidates(
                user_id,
                region=region,
            )

            now = int(self._clock())
            eligible = len(candidates)

            stale = sum(
                item.is_stale(now)
                for item in candidates
            )

            if stale and stop_reason is None:
                stop_reason = "remaining"

            return RefreshResult(
                eligible=eligible,
                stale=stale,
                attempted=attempted,
                updated=updated,
                failed=failed,
                skipped=skipped,
                stop_reason=stop_reason,
            )

    async def _refresh(
        self,
        user_id: int,
        *,
        region: str,
        attempted_keys=None,
    ) -> RefreshResult:

        started = self._monotonic()

        if started < self._paused_until:
            return RefreshResult(stop_reason="paused")

        candidates = await self._database.release_candidates(
            user_id,
            region=region,
        )

        now = int(self._clock())

        def refresh_priority(item):
            key = (
                region,
                item.media_type,
                item.tmdb_id,
            )

            retry_deadline = self._retry_after.get(key)

            return (
                retry_deadline is not None,
                retry_deadline or 0,
                item.checked_at is not None,
                item.checked_at or 0,
                item.media_type,
                item.tmdb_id,
            )

        stale = sorted(
            (
                item for item in candidates
                if item.is_stale(now)
                and (
                    attempted_keys is None
                    or (
                        region,
                        item.media_type,
                        item.tmdb_id,
                    ) not in attempted_keys
                )
            ),
            key=refresh_priority,
        )

        attempted = 0
        updated = 0
        failed = 0
        skipped = 0
        stop_reason = None

        for item in stale:
            if attempted >= REFRESH_BATCH_SIZE:
                stop_reason = "batch_limit"
                break

            if self._monotonic() - started >= BATCH_START_BUDGET:
                stop_reason = "time_budget"
                break

            key = (
                region,
                item.media_type,
                item.tmdb_id,
            )

            if (
                self._retry_after.get(key, 0)
                > self._monotonic()
            ):
                skipped += 1
                continue

            attempted += 1

            if attempted_keys is not None:
                attempted_keys.add(key)

            checked_at = int(self._clock())

            try:
                details = await self._client.details(
                    item.media_type,
                    item.tmdb_id,
                    force_refresh=True,
                )

                if (
                    details.media_type != item.media_type
                    or details.tmdb_id != item.tmdb_id
                ):
                    raise TmdbError(
                        "Release metadata identity mismatch"
                    )

                if (
                    details.media_type == "movie"
                    and details.release_region != region
                ):
                    raise TmdbError(
                        "Release metadata region mismatch"
                    )

            except TmdbError as exc:
                failed += 1

                deadline = (
                    self._monotonic() + RETRY_DELAY
                )

                self._retry_after[key] = deadline

                if exc.status_code in {401, 403, 429}:
                    self._paused_until = deadline

                    stop_reason = (
                        "rate_limited"
                        if exc.status_code == 429
                        else "authorization"
                    )
                    break

                continue

            except (ValueError, TypeError, KeyError, AttributeError):
                failed += 1

                self._retry_after[key] = (
                    self._monotonic() + RETRY_DELAY
                )

                continue

            recovery = {}

            if (
                item.checked_at is not None
                and item.checked_at > checked_at
            ):
                recovery["recover_future_checked_at"] = (
                    item.checked_at
                )

            stored = await self._database.store_release_snapshot(
                details,
                region=region,
                checked_at=checked_at,
                **recovery,
            )

            if stored:
                updated += 1
                self._retry_after.pop(key, None)

            else:
                skipped += 1

                self._retry_after[key] = (
                    self._monotonic() + RETRY_DELAY
                )

        return RefreshResult(
            eligible=len(candidates),
            stale=len(stale),
            attempted=attempted,
            updated=updated,
            failed=failed,
            skipped=skipped,
            stop_reason=stop_reason,
        )
