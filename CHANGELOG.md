# Changelog

## Media Library and Release Intelligence (development)

### Added

- Interactive personal library with complete pagination.
- Film, television and documentary category filters.
- Personal rating, watchlist and viewing-progress navigation.
- Regional film release dates, series premieres and upcoming episodes.
- Distinct continuing, ended and canceled lifecycle information.
- Persistent region-scoped release metadata cache.
- Personal upcoming release schedule with 7, 30 and 90-day windows.
- Bounded asynchronous single-batch and multi-batch metadata refresh.
- Refresh-all progress reporting in the release schedule.
- Visible tracker import processing feedback.

### Changed

- Personal progress groups films, television series and documentaries
  before pagination.
- Released episode totals can be retrieved for the visible page.
- Media cards include available release information.
- Import application and release metadata refresh are mutually exclusive.

### Fixed

- Library state refreshes after watched and watchlist changes,
  invalidating previously issued action buttons.
- Future-dated release metadata can recover after clock correction.
- Persistent metadata failures no longer repeatedly monopolize batches
  while the same refresh worker remains active.
- Personal library timestamp sorting handles mixed formats and offsets.

### Database

- Adds the `release_cache` table without replacing existing personal
  viewing records.
- Existing tracker imports, ratings and episode history are retained.


## Unreleased

### Added

- Native IMDb, Letterboxd, Trakt, and Simkl tracker imports.
- Private import previews with explicit approval.
- Selective approval of verified records and dependency exclusion.
- Conservative TMDB identity resolution with per-import caching.
- Transactional database application and verified pre-import backups.
- Supervised background import execution with bounded concurrency.

- Repository-level source license notice, security policy, and third-party attribution documentation for public GitHub publication.
- Explicit JustWatch attribution when streaming-provider availability is displayed.
- Media-aware classification for films, documentary films, TV series, limited series, documentary series, and documentary limited series.
- Classification-specific output cards with series status, season count, episode count, episode runtime, network/platform, and original-run dates.
- Personal watchlists keyed by chat ID and Telegram user ID.
- Personal movie and documentary-film watched status.
- Personal TV, miniseries, and documentary-series episode progress.
- Individual episode toggles, season-wide controls, mark-all-released, Watching, Caught up, Completed, and clear-progress actions.
- Owner-bound personal rating and progress menus.
- `/mywatchlist`, `/myprogress`, `/suggestions`, and `/mysuggestions`.
- Group suggestions with per-member attribution and duplicate-title aggregation.
- Aggregated group watchlist based on members' personal watchlists.
- Automatic migration of legacy group catalog entries and recommendations.
- TMDB details and season caches to limit repeated API calls during progress selection.
- Owner-only command authorization through `OWNER_USER_IDS`.
- `/adminhelp`, `/whoami`, `/groups`, `/leave`, `/leavechat`, `/status`, `/reload`, `/restart`, and `/shutdown`.

### Changed

- Reorganized Telegram handlers under `handlers/` and artwork under `assets/` so the repository layout matches the runtime imports and asset paths.
- Revamped the project README for standalone GitHub publication and deployment.
- Expanded `.gitignore` coverage for runtime data, private configuration, credentials, build output, editor files, and local archives.
- Shared media cards now use neutral controls that make sense to every member.
- `Recommend` is now `Suggest to Group`.
- `/watched` now shows the requesting member's watched, caught-up, and completed titles.
- Ratings remain individual while the card displays the calculated group average.
- Ongoing series automatically return from Caught up to Watching when new released episodes appear.
- Owner callbacks bypass the normal group allowlist while still requiring an owner ID.
- Logging reconfiguration closes replaced handlers cleanly.

### Fixed

- Order-dependent verification of imported series and episode history.
- Import session recovery following failed Telegram message delivery.
- Duplicate confirmation and stale-session authorization safeguards.
- Unnecessary backup creation for stale previews and semantic no-ops.
- Backup collision handling and cooperative backup deadlines.
- SQLite connection cleanup.
- Group command responsiveness during active imports.
- Graceful owner restart and shutdown during import processing.
- Unverified Simkl anime episode numbering now requires review.
- Import no-op messages distinguish current database state from
  proof of a previous transaction.

## 0.1 — 2026-07-28

### Added

- Official Kokabiel identity: **Film, TV & Documentary Guide**.
- Direct-script Telegram bot foundation using `kokabiel.py`.
- TMDB film, TV, documentary, and combined search.
- Same-title disambiguation with paginated inline buttons.
- Poster-based media cards with trailer, IMDb, TMDB, and watch-provider links.
- SQLite catalog foundation.
- Per-member 1–10 ratings and recommendations.
- Random selection from the watchlist.
- External private configuration under `~/bots/config/`.
- Explicit command-only privacy boundary.
- `/about` credits card with the required TMDB notice and approved logo assets.
