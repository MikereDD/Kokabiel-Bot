from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from models import MediaDetails, MediaType


CACHE_MAX_AGE = 24 * 60 * 60
REFRESH_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class ScheduledRelease:
    date: str
    label: str
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True, slots=True)
class ReleaseSnapshot:
    lifecycle: str
    events: tuple[ScheduledRelease, ...]


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    media_type: MediaType
    tmdb_id: int
    title: str
    media_class: str
    checked_at: int | None
    snapshot: ReleaseSnapshot | None

    def is_stale(self, now: int) -> bool:
        return (
            self.snapshot is None
            or self.checked_at is None
            or now < self.checked_at
            or now - self.checked_at >= CACHE_MAX_AGE
        )


def valid_date(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None

    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None

    return value if parsed.isoformat() == value else None


def build_release_snapshot(
    details: MediaDetails,
    *,
    region: str,
) -> ReleaseSnapshot:

    events: set[ScheduledRelease] = set()

    if details.media_type == "movie":
        if (
            details.release_region is not None
            and details.release_region != region
        ):
            raise ValueError("Release metadata region mismatch")

        for event in details.release_events:
            released = valid_date(event.date)

            if released and event.region == region:
                events.add(
                    ScheduledRelease(
                        date=released,
                        label=f"{region} {event.label}",
                    )
                )

        original = valid_date(details.release_date)

        if original and not any(
            event.date == original for event in events
        ):
            events.add(
                ScheduledRelease(
                    date=original,
                    label="Original release",
                )
            )

    elif details.lifecycle_label not in {"Ended", "Canceled"}:

        premiere = valid_date(details.first_air_date)

        if premiere:
            events.add(
                ScheduledRelease(
                    date=premiere,
                    label="Series premiere",
                )
            )

        for season in details.seasons:
            air_date = valid_date(season.air_date)

            if air_date and season.season_number > 0:
                events.add(
                    ScheduledRelease(
                        date=air_date,
                        label="Season premiere",
                        season=season.season_number,
                    )
                )

        episode = details.next_episode

        if episode is not None:
            air_date = valid_date(episode.air_date)

            if air_date:
                events.add(
                    ScheduledRelease(
                        date=air_date,
                        label="Episode",
                        season=episode.season_number,
                        episode=episode.episode_number,
                    )
                )

    return ReleaseSnapshot(
        lifecycle=details.lifecycle_label,
        events=tuple(
            sorted(
                events,
                key=lambda event: (
                    event.date,
                    event.season or 0,
                    event.episode or 0,
                    event.label,
                ),
            )
        ),
    )


def upcoming_events(
    snapshot: ReleaseSnapshot,
    *,
    today: date,
    through: date,
) -> tuple[ScheduledRelease, ...]:

    if through < today:
        raise ValueError("Invalid schedule date range")

    start = today.isoformat()
    end = through.isoformat()

    return tuple(
        event
        for event in snapshot.events
        if start <= event.date <= end
    )


SCHEDULE_WINDOWS = (7, 30, 90)
SCHEDULE_PAGE_SIZE = 8


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    candidate: ReleaseCandidate
    event: ScheduledRelease
    stale: bool


@dataclass(frozen=True, slots=True)
class SchedulePage:
    entries: tuple[ScheduleItem, ...]
    total: int
    page: int
    page_count: int
    tracked: int
    fresh: int
    stale: int
    unchecked: int
    start: str
    end: str


def personal_schedule_page(
    candidates: tuple[ReleaseCandidate, ...],
    *,
    today: date,
    now: int,
    days: int = 30,
    page: int = 0,
) -> SchedulePage:

    if days not in SCHEDULE_WINDOWS:
        raise ValueError("Invalid schedule window")

    if type(page) is not int or page < 0:
        raise ValueError("Invalid schedule page")

    through = today + timedelta(days=days - 1)

    fresh = 0
    stale = 0
    unchecked = 0
    entries = []

    for candidate in candidates:
        if candidate.snapshot is None:
            unchecked += 1
            continue

        outdated = candidate.is_stale(now)

        if outdated:
            stale += 1
        else:
            fresh += 1

        for event in upcoming_events(
            candidate.snapshot,
            today=today,
            through=through,
        ):
            entries.append(
                ScheduleItem(
                    candidate=candidate,
                    event=event,
                    stale=outdated,
                )
            )

    entries.sort(
        key=lambda item: (
            item.event.date,
            item.candidate.title.casefold(),
            item.candidate.media_type,
            item.candidate.tmdb_id,
            item.event.season or 0,
            item.event.episode or 0,
            item.event.label,
        )
    )

    total = len(entries)

    page_count = max(
        1,
        (total + SCHEDULE_PAGE_SIZE - 1)
        // SCHEDULE_PAGE_SIZE,
    )

    page = min(page, page_count - 1)
    start = page * SCHEDULE_PAGE_SIZE

    return SchedulePage(
        entries=tuple(
            entries[start:start + SCHEDULE_PAGE_SIZE]
        ),
        total=total,
        page=page,
        page_count=page_count,
        tracked=len(candidates),
        fresh=fresh,
        stale=stale,
        unchecked=unchecked,
        start=today.isoformat(),
        end=through.isoformat(),
    )
