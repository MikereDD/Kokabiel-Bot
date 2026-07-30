# Changelog

## Unreleased

### Added

- Owner-only command authorization through `OWNER_USER_IDS`.
- `/adminhelp`, `/whoami`, `/groups`, `/leave`, `/leavechat`, `/status`, `/reload`, `/restart`, and `/shutdown`.
- Confirmation buttons for leaving groups, restarting, and shutting down.
- Known-group tracking with active/left status.
- Runtime-safe configuration reloads.

### Changed

- Owner callbacks bypass the normal group allowlist while still requiring an owner ID.
- Logging reconfiguration now closes replaced handlers cleanly.

## 0.1 — 2026-07-28

### Added

- Official Kokabiel identity: **Film, TV & Documentary Guide**.
- Standalone `kokabiel` Python package and command-line entry point.
- Kokabiel-specific configuration, database, log, and systemd service names.
- Command-only Telegram bot foundation.
- TMDB film, TV, documentary, and combined search.
- Same-title disambiguation through inline buttons.
- Poster-based media cards with external links.
- Group-scoped SQLite watchlist and watched catalog.
- Per-member 1–10 ratings and recommendations.
- Random selection from the shared watchlist.
- External runtime configuration and Arakiel deployment example.
- Explicit privacy documentation and no ordinary-message handler.
- `/about` credits command with the required TMDB notice and official links.
- Approved TMDB logo assets bundled for the Telegram About/Credits presentation.
- TMDB attribution and asset-provenance documentation.
