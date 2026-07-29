from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote_plus

import aiohttp

from models import MediaDetails, MediaType, SearchResult

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DOCUMENTARY_GENRE_ID = 99


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
        return self._parse_details(media_type, payload)

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
        genres = tuple(
            str(item.get("name"))
            for item in payload.get("genres", [])
            if item.get("name")
        )

        if media_type == "movie":
            runtime = self._format_runtime(payload.get("runtime"))
            leads = self._directors(payload)
            certification = self._movie_certification(payload)
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
        )

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
