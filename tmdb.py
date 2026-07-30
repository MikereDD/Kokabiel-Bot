from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote_plus

import aiohttp

from models import (
    EpisodeDetails,
    MediaClass,
    MediaDetails,
    MediaType,
    SearchResult,
    SeasonDetails,
    SeasonSummary,
    SeriesStatus,
)

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DOCUMENTARY_GENRE_ID = 99
CACHE_SECONDS = 600.0


class TmdbError(RuntimeError):
    """Raised when a TMDB request fails or returns malformed data."""


class TmdbClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        bearer_token: str,
        *,
        language: str = "en-US",
        region: str = "US",
        include_adult: bool = False,
    ) -> None:
        self._session = session
        self._bearer_token = bearer_token
        self._language = language
        self._region = region
        self._include_adult = include_adult
        self._details_cache: dict[tuple[str, int], tuple[float, MediaDetails]] = {}
        self._season_cache: dict[tuple[int, int], tuple[float, SeasonDetails]] = {}

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = {"language": self._language, **params}
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                f"{API_BASE}{path}", headers=headers, params=query
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    message = payload.get("status_message", f"HTTP {response.status}")
                    raise TmdbError(f"TMDB request failed: {message}")
                if not isinstance(payload, dict):
                    raise TmdbError("TMDB returned an unexpected response.")
                return payload
        except TimeoutError as exc:
            raise TmdbError("TMDB request timed out.") from exc
        except aiohttp.ClientError as exc:
            raise TmdbError(f"TMDB network request failed: {exc}") from exc

    async def search_movie(self, query: str, limit: int = 8) -> list[SearchResult]:
        payload = await self._get(
            "/search/movie",
            query=query,
            include_adult=str(self._include_adult).lower(),
            page=1,
        )
        return self._parse_results(payload.get("results", []), "movie")[:limit]

    async def search_tv(self, query: str, limit: int = 8) -> list[SearchResult]:
        payload = await self._get(
            "/search/tv",
            query=query,
            include_adult=str(self._include_adult).lower(),
            page=1,
        )
        return self._parse_results(payload.get("results", []), "tv")[:limit]

    async def search_multi(self, query: str, limit: int = 8) -> list[SearchResult]:
        payload = await self._get(
            "/search/multi",
            query=query,
            include_adult=str(self._include_adult).lower(),
            page=1,
        )
        parsed: list[SearchResult] = []
        for item in payload.get("results", []):
            media_type = item.get("media_type")
            if media_type in {"movie", "tv"}:
                parsed.extend(self._parse_results([item], media_type))
        return parsed[:limit]

    async def search_documentary(self, query: str, limit: int = 8) -> list[SearchResult]:
        movie_results, tv_results = await asyncio.gather(
            self.search_movie(query, limit=20),
            self.search_tv(query, limit=20),
        )
        combined = [
            item
            for item in (*movie_results, *tv_results)
            if DOCUMENTARY_GENRE_ID in item.genre_ids
        ]
        combined.sort(key=lambda item: item.popularity, reverse=True)
        return combined[:limit]

    async def details(self, media_type: MediaType, tmdb_id: int) -> MediaDetails:
        cache_key = (media_type, tmdb_id)
        cached = self._details_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

        appended = "credits,videos,external_ids,watch/providers"
        if media_type == "movie":
            appended += ",release_dates"
        else:
            appended += ",content_ratings"

        payload = await self._get(
            f"/{media_type}/{tmdb_id}",
            append_to_response=appended,
            include_video_language=f"{self._language.split('-')[0]},en,null",
        )
        details = self._parse_details(media_type, payload)
        self._details_cache[cache_key] = (now, details)
        self._trim_cache(self._details_cache, 200)
        return details

    async def season_details(self, tmdb_id: int, season_number: int) -> SeasonDetails:
        cache_key = (tmdb_id, season_number)
        cached = self._season_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

        payload = await self._get(f"/tv/{tmdb_id}/season/{season_number}")
        episodes: list[EpisodeDetails] = []
        for item in payload.get("episodes", []):
            episode_number = item.get("episode_number")
            if not isinstance(episode_number, int):
                continue
            runtime = item.get("runtime")
            episodes.append(
                EpisodeDetails(
                    season_number=int(item.get("season_number", season_number)),
                    episode_number=episode_number,
                    name=str(item.get("name") or f"Episode {episode_number}"),
                    air_date=str(item.get("air_date")) if item.get("air_date") else None,
                    runtime=int(runtime) if isinstance(runtime, int) and runtime > 0 else None,
                )
            )

        details = SeasonDetails(
            tmdb_id=tmdb_id,
            season_number=season_number,
            name=str(payload.get("name") or f"Season {season_number}"),
            episodes=tuple(episodes),
        )
        self._season_cache[cache_key] = (now, details)
        self._trim_cache(self._season_cache, 300)
        return details

    async def all_regular_seasons(self, details: MediaDetails) -> tuple[SeasonDetails, ...]:
        season_numbers = [
            season.season_number
            for season in details.seasons
            if season.season_number > 0
        ]
        if not season_numbers:
            return ()

        semaphore = asyncio.Semaphore(4)

        async def fetch(number: int) -> SeasonDetails:
            async with semaphore:
                return await self.season_details(details.tmdb_id, number)

        return tuple(await asyncio.gather(*(fetch(number) for number in season_numbers)))

    async def released_episode_keys(
        self, details: MediaDetails, *, today: str
    ) -> set[tuple[int, int]]:
        """Return regular-season episode keys that have aired by ``today``.

        Completed series and older seasons can be resolved from TMDB's season
        summaries. Only the current/latest season of an ongoing series needs a
        season-details request, which keeps large-series progress menus fast.
        """
        regular = sorted(
            (season for season in details.seasons if season.season_number > 0),
            key=lambda season: season.season_number,
        )
        if not regular:
            return set()

        latest_number = regular[-1].season_number
        keys: set[tuple[int, int]] = set()
        for summary in regular:
            if details.series_status == "ended":
                keys.update(
                    (summary.season_number, episode_number)
                    for episode_number in range(1, summary.episode_count + 1)
                )
                continue

            if summary.air_date and summary.air_date > today:
                continue

            if summary.air_date and summary.season_number < latest_number:
                keys.update(
                    (summary.season_number, episode_number)
                    for episode_number in range(1, summary.episode_count + 1)
                )
                continue

            # The latest ongoing season, or a season missing its summary air
            # date, needs episode-level dates so future episodes stay unwatched.
            season = await self.season_details(
                details.tmdb_id, summary.season_number
            )
            keys.update(
                (episode.season_number, episode.episode_number)
                for episode in season.episodes
                if episode.air_date and episode.air_date <= today
            )
        return keys

    def _parse_results(
        self, items: Iterable[dict[str, Any]], media_type: MediaType
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in items:
            tmdb_id = item.get("id")
            if not isinstance(tmdb_id, int):
                continue
            title_key = "title" if media_type == "movie" else "name"
            original_key = "original_title" if media_type == "movie" else "original_name"
            date_key = "release_date" if media_type == "movie" else "first_air_date"
            title = str(item.get(title_key) or item.get(original_key) or "Untitled")
            original_title = str(item.get(original_key) or title)
            date = str(item.get(date_key) or "")
            year = date[:4] if len(date) >= 4 else "Unknown year"
            genre_ids = tuple(
                int(value) for value in item.get("genre_ids", []) if isinstance(value, int)
            )
            results.append(
                SearchResult(
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    title=title,
                    original_title=original_title,
                    year=year,
                    overview=str(item.get("overview") or ""),
                    poster_path=item.get("poster_path"),
                    genre_ids=genre_ids,
                    popularity=float(item.get("popularity") or 0.0),
                )
            )
        return results

    def _parse_details(self, media_type: MediaType, payload: dict[str, Any]) -> MediaDetails:
        title_key = "title" if media_type == "movie" else "name"
        original_key = "original_title" if media_type == "movie" else "original_name"
        date_key = "release_date" if media_type == "movie" else "first_air_date"

        title = str(payload.get(title_key) or payload.get(original_key) or "Untitled")
        original_title = str(payload.get(original_key) or title)
        date = str(payload.get(date_key) or "")
        year = date[:4] if len(date) >= 4 else "Unknown year"
        poster_path = payload.get("poster_path")
        poster_url = f"{IMAGE_BASE}{poster_path}" if poster_path else None
        genre_items = [item for item in payload.get("genres", []) if item.get("name")]
        genres = tuple(str(item["name"]) for item in genre_items)
        genre_ids = {
            int(item["id"])
            for item in genre_items
            if isinstance(item.get("id"), int)
        }
        is_documentary = DOCUMENTARY_GENRE_ID in genre_ids or any(
            name.casefold() == "documentary" for name in genres
        )

        series_status: SeriesStatus = "unknown"
        series_type: str | None = None
        tmdb_status: str | None = None
        first_air_date: str | None = None
        last_air_date: str | None = None
        number_of_seasons = 0
        number_of_episodes = 0
        networks: tuple[str, ...] = ()
        seasons: tuple[SeasonSummary, ...] = ()

        if media_type == "movie":
            runtime = self._format_runtime(payload.get("runtime"))
            leads = self._directors(payload)
            certification = self._movie_certification(payload)
            media_class: MediaClass = (
                "documentary_film" if is_documentary else "film"
            )
        else:
            episode_runtime = next(
                (value for value in payload.get("episode_run_time", []) if value), None
            )
            runtime = self._format_runtime(episode_runtime)
            leads = tuple(
                str(item.get("name"))
                for item in payload.get("created_by", [])
                if item.get("name")
            )
            certification = self._tv_certification(payload)
            series_type = str(payload.get("type") or "").strip() or None
            tmdb_status = str(payload.get("status") or "").strip() or None
            series_status = self._series_status(tmdb_status)
            first_air_date = str(payload.get("first_air_date")) if payload.get("first_air_date") else None
            last_air_date = str(payload.get("last_air_date")) if payload.get("last_air_date") else None
            number_of_seasons = int(payload.get("number_of_seasons") or 0)
            number_of_episodes = int(payload.get("number_of_episodes") or 0)
            networks = tuple(
                str(item.get("name"))
                for item in payload.get("networks", [])
                if item.get("name")
            )[:4]
            seasons = tuple(
                SeasonSummary(
                    season_number=int(item.get("season_number", 0)),
                    name=str(item.get("name") or f"Season {item.get('season_number', 0)}"),
                    episode_count=int(item.get("episode_count") or 0),
                    air_date=str(item.get("air_date")) if item.get("air_date") else None,
                )
                for item in payload.get("seasons", [])
                if isinstance(item.get("season_number"), int)
            )
            is_limited = (series_type or "").casefold() in {
                "miniseries",
                "limited series",
            }
            if is_documentary and is_limited:
                media_class = "documentary_limited_series"
            elif is_documentary:
                media_class = "documentary_series"
            elif is_limited:
                media_class = "limited_series"
            else:
                media_class = "tv_series"

        credits = payload.get("credits", {})
        cast = tuple(
            str(item.get("name"))
            for item in credits.get("cast", [])[:5]
            if item.get("name")
        )
        external_ids = payload.get("external_ids", {})
        imdb_id = external_ids.get("imdb_id") or payload.get("imdb_id")
        trailer_url = self._trailer_url(payload.get("videos", {}).get("results", []))
        providers = payload.get("watch/providers", {}).get("results", {}).get(self._region, {})

        return MediaDetails(
            media_type=media_type,
            tmdb_id=int(payload["id"]),
            title=title,
            original_title=original_title,
            year=year,
            overview=str(payload.get("overview") or "No synopsis is currently available."),
            poster_url=poster_url,
            genres=genres,
            runtime=runtime,
            certification=certification,
            directors_or_creators=leads,
            cast=cast,
            vote_average=float(payload.get("vote_average") or 0.0),
            vote_count=int(payload.get("vote_count") or 0),
            imdb_id=str(imdb_id) if imdb_id else None,
            trailer_url=trailer_url,
            tmdb_url=f"https://www.themoviedb.org/{media_type}/{payload['id']}",
            streaming=self._provider_names(providers.get("flatrate", [])),
            rent=self._provider_names(providers.get("rent", [])),
            buy=self._provider_names(providers.get("buy", [])),
            media_class=media_class,
            is_documentary=is_documentary,
            series_status=series_status,
            tmdb_status=tmdb_status,
            series_type=series_type,
            first_air_date=first_air_date,
            last_air_date=last_air_date,
            number_of_seasons=number_of_seasons,
            number_of_episodes=number_of_episodes,
            networks=networks,
            seasons=seasons,
        )

    @staticmethod
    def _series_status(value: str | None) -> SeriesStatus:
        normalized = (value or "").casefold()
        if normalized in {"ended", "canceled", "cancelled"}:
            return "ended"
        if normalized in {
            "returning series",
            "in production",
            "planned",
            "pilot",
        }:
            return "ongoing"
        return "unknown"

    @staticmethod
    def _format_runtime(minutes: Any) -> str | None:
        if not isinstance(minutes, int) or minutes <= 0:
            return None
        hours, remainder = divmod(minutes, 60)
        if hours:
            return f"{hours}h {remainder}m" if remainder else f"{hours}h"
        return f"{remainder}m"

    @staticmethod
    def _directors(payload: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(item.get("name"))
            for item in payload.get("credits", {}).get("crew", [])
            if item.get("job") == "Director" and item.get("name")
        )[:3]

    def _movie_certification(self, payload: dict[str, Any]) -> str | None:
        for region in payload.get("release_dates", {}).get("results", []):
            if region.get("iso_3166_1") != self._region:
                continue
            for release in region.get("release_dates", []):
                value = str(release.get("certification") or "").strip()
                if value:
                    return value
        return None

    def _tv_certification(self, payload: dict[str, Any]) -> str | None:
        for rating in payload.get("content_ratings", {}).get("results", []):
            if rating.get("iso_3166_1") == self._region and rating.get("rating"):
                return str(rating["rating"])
        return None

    @staticmethod
    def _trailer_url(videos: Iterable[dict[str, Any]]) -> str | None:
        candidates = [
            item
            for item in videos
            if item.get("site") == "YouTube"
            and item.get("type") == "Trailer"
            and item.get("key")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: bool(item.get("official")), reverse=True)
        return f"https://www.youtube.com/watch?v={quote_plus(str(candidates[0]['key']))}"

    @staticmethod
    def _provider_names(items: Iterable[dict[str, Any]]) -> tuple[str, ...]:
        seen: set[str] = set()
        names: list[str] = []
        for item in items:
            name = str(item.get("provider_name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return tuple(names[:6])

    @staticmethod
    def _trim_cache(cache: dict[Any, tuple[float, Any]], limit: int) -> None:
        if len(cache) <= limit:
            return
        oldest = sorted(cache.items(), key=lambda item: item[1][0])[: len(cache) - limit]
        for key, _ in oldest:
            cache.pop(key, None)
