<div align="center">

<img src="assets/kokabiel-avatar.png" alt="Kokabiel" width="180">

# Kokabiel

[![Version](https://img.shields.io/badge/version-0.2.0--dev-gold)](https://github.com/MikereDD/Kokabiel-Bot/releases)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Telegram-26A5E4)
![Library](https://img.shields.io/badge/python--telegram--bot-21.x-blue)
![Database](https://img.shields.io/badge/database-SQLite-07405E)
![API](https://img.shields.io/badge/data-TMDB-01B4E4)
![License](https://img.shields.io/badge/license-Typezer%E2%88%85%20All%20Rights%20Reserved-darkred)

### Film, TV & Documentary Guide for Telegram

**Command-driven discovery · Personal progress · Group watchlists · TMDB-powered metadata**

**Current development version: `v0.2.0-dev`**

</div>

Kokabiel is a Telegram bot built for groups that discover, discuss, and keep
track of films, television series, miniseries, documentary films, and
documentary series together.

Kokabiel is deliberately **command-and-button driven**. It does not passively
read ordinary group conversation, scan message history for titles, or post
unsolicited recommendations. A member invokes the bot, selects the correct
production, and interacts with Kokabiel through explicit controls.

## Highlights

- Search movies, television, documentaries, or both media types together.
- Disambiguate remakes and same-name productions before posting a card.
- Media-aware cards distinguish films, documentary films, TV series, limited
  series, documentary series, and documentary limited series.
- Personal watchlists, ratings, watched status, and episode progress are scoped
  to both Telegram chat ID and user ID.
- Group watchlists aggregate members' personal selections without erasing
  individual ownership.
- Group suggestions combine duplicate titles while preserving who suggested
  them.
- Series progress supports episode and season controls, `Watching`, `Caught up`,
  and `Completed` states.
- Owner-only administration includes runtime status, configuration reload,
  restart/shutdown controls, group inspection, and leave-group actions.
- SQLite keeps the deployment lightweight and self-contained.

## Commands

| Area | Commands |
| --- | --- |
| Discovery | `/movie <title>` · `/tv <title>` · `/documentary <title>` · `/search <title>` · `/random` |
| Personal | `/mywatchlist` · `/watched` · `/myprogress` · `/mysuggestions` |
| Group | `/watchlist` · `/suggestions` |
| General | `/about` · `/help` |
| Owner | `/adminhelp` · `/whoami` · `/groups` · `/leave` · `/leavechat <chat_id>` · `/status` · `/reload` · `/restart` · `/shutdown` |

Destructive or disruptive owner actions require confirmation. Sensitive runtime
commands are restricted to Kokabiel's private chat.

## Privacy model

Kokabiel registers command handlers and callback-button handlers; it has no
ordinary-text message handler for passive title detection or conversation
monitoring.

Keep **Privacy Mode enabled** in BotFather:

1. Open BotFather.
2. Run `/setprivacy`.
3. Select Kokabiel.
4. Choose **Enable**.

Kokabiel does not require Telegram administrator status for its normal feature
set.

## Requirements

- Python 3.11 or newer recommended
- Telegram bot token from BotFather
- TMDB API Read Access Token
- Network access to Telegram and TMDB

Install the Python dependencies from `requirements.txt`.

## Repository layout

```text
Kokabiel/
├── assets/                  # Kokabiel and required attribution artwork
├── docs/                    # third-party attribution and project documentation
├── handlers/                # Telegram command/callback/owner handlers
├── app.py                   # application construction and handler registration
├── config.py                # external runtime configuration loader
├── database.py              # SQLite schema, migrations, and persistence
├── formatting.py            # Telegram-safe presentation formatting
├── keyboards.py             # inline keyboard builders
├── kokabiel.py              # direct-script entry point
├── kokabielrc.example.py    # safe configuration template
├── models.py                # domain models
├── tmdb.py                  # async TMDB client
├── requirements.txt
├── CHANGELOG.md
├── LICENSE.md
├── SECURITY.md
└── README.md
```

## Installation

Kokabiel remains a direct Python bot rather than an installed Python package.

```bash
cd "$HOME/bots/Kokabiel"
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Create a private configuration **outside the repository**:

```bash
mkdir -p "$HOME/bots/config"
cp kokabielrc.example.py "$HOME/bots/config/kokabielrc.py"
chmod 600 "$HOME/bots/config/kokabielrc.py"
vim "$HOME/bots/config/kokabielrc.py"
```

At minimum, configure `BOT_TOKEN`, `TMDB_BEARER_TOKEN`, and the owner user ID.
Use `ALLOWED_CHAT_IDS` when Kokabiel should be restricted to known groups.

```python
OWNER_USER_IDS = [123456789]

ALLOWED_CHAT_IDS = [
    # -1001234567890,
]
```

Telegram group administrators do **not** automatically inherit Kokabiel owner
access.

## Running Kokabiel

```bash
cd "$HOME/bots/Kokabiel"
.venv/bin/python kokabiel.py
```

The default private configuration path is:

```text
$HOME/bots/config/kokabielrc.py
```

Override it with `KOKABIEL_CONFIG` when necessary.

## Data and runtime files

The example configuration stores runtime state outside the repository:

```text
$HOME/bots/data/kokabiel/kokabiel.db
$HOME/bots/logs/kokabiel/kokabiel.log
```

Database, log, local configuration, virtual-environment, Python-cache, and
common secret-key files are excluded by `.gitignore`.

## Media data and attribution

TMDB provides Kokabiel's primary media metadata and imagery. Kokabiel links to
IMDb only when TMDB supplies an IMDb title ID; Kokabiel does not scrape IMDb.
Streaming-provider availability returned through TMDB is sourced from
JustWatch and is attributed when displayed.

> This product uses the TMDB API but is not endorsed or certified by TMDB.

See [`docs/ATTRIBUTION.md`](docs/ATTRIBUTION.md) for third-party attribution and
asset details.

## Validation before deployment

From the repository root:

```bash
python -m compileall -q .
```

For behavior that depends on Telegram, TMDB, and real group state, use the live
checklist in [`TEST-PERSONAL-GROUP.md`](TEST-PERSONAL-GROUP.md) after deploying
with non-production test credentials where practical.

## Source availability and license

Kokabiel's source is publicly viewable for transparency, review, and reference,
but **Kokabiel is not open-source software**. Copyright and control remain with
Typezer∅. Public availability of this repository does not grant blanket
permission to redistribute, repackage, sell, incorporate, or publish modified
versions of the project.

See [`LICENSE.md`](LICENSE.md) for the complete source-code notice.

---

<div align="center">

**Kokabiel · Typezer∅**

</div>
