# Changelog

## Unreleased

### Added

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

- Shared media cards now use neutral controls that make sense to every member.
- `Recommend` is now `Suggest to Group`.
- `/watched` now shows the requesting member's watched, caught-up, and completed titles.
- Ratings remain individual while the card displays the calculated group average.
- Ongoing series automatically return from Caught up to Watching when new released episodes appear.
- Owner callbacks bypass the normal group allowlist while still requiring an owner ID.
- Logging reconfiguration closes replaced handlers cleanly.

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
