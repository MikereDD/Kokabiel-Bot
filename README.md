<div align="center">

<img src="assets/kokabiel-avatar.png" alt="Kokabiel" width="180">

# Kokabiel

[![Version](https://img.shields.io/badge/version-0.2.1-gold)](https://github.com/MikereDD/Kokabiel-Bot/releases)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Telegram-26A5E4)
![Library](https://img.shields.io/badge/python--telegram--bot-22.x-blue)
![Database](https://img.shields.io/badge/database-SQLite-07405E)
![API](https://img.shields.io/badge/data-TMDB-01B4E4)
![License](https://img.shields.io/badge/license-Typezer%E2%88%85%20All%20Rights%20Reserved-darkred)

### Film, TV & Documentary Guide for Telegram

**Command-driven discovery · Personal progress · Group watchlists · TMDB-powered metadata**

**Current version: `v0.2.1`**

[Release notes](docs/releases/v0.2.1.md)

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
- Personal watchlists, ratings, watched status, and episode progress are
  scoped globally to each Telegram user. Group associations determine
  participation in group aggregates.
- Group watchlists aggregate members' personal selections without erasing
  individual ownership.
- Group suggestions combine duplicate titles while preserving who suggested
  them.
- Series progress supports episode and season controls, `Watching`, `Caught up`,
  and `Completed` states.
- Owner-only administration includes runtime status, configuration reload,
  restart/shutdown controls, group inspection, and leave-group actions.
- SQLite keeps the deployment lightweight and self-contained.
- Native tracker imports from IMDb, Letterboxd, Trakt, and Simkl.
- Reviewable import previews and selective approval of verified records.
- Transactional imports with verified pre-import database backups.
- Private tracker imports for members of authorized groups.
- User-scoped import history and transactional undo.
- Conservative preservation of personal records modified after import.

## Commands

| Area | Commands |
| --- | --- |
| Discovery | `/movie <title>` · `/tv <title>` · `/documentary <title>` · `/search <title>` · `/random` |
| Personal | `/mywatchlist` · `/watched` · `/myprogress` · `/mysuggestions` |
| Group | `/watchlist` · `/suggestions` |
| Imports | `/import` (authorized group or owner-private chat) |
| General | `/about` · `/help` |
| Owner | `/adminhelp` · `/whoami` · `/groups` · `/leave` · `/leavechat <chat_id>` · `/status` · `/reload` · `/restart` · `/shutdown` |

Destructive or disruptive owner actions require confirmation. Sensitive runtime
commands are restricted to Kokabiel's private chat.

## Tracker imports

Members of an explicitly configured authorized group can use
`/import` in that group. Kokabiel opens a private import session
with the requesting member.

Owners can also initiate imports directly in their private chat.

All export uploads, previews, confirmations, progress messages,
and results are handled privately. If Kokabiel cannot initiate
a DM, it attempts to provide a user-bound, single-use activation
link through its private in-group response mechanism.

Ordinary members do not receive general private-command access.

Kokabiel prepares a preview before applying imported personal
watchlists, watched states, ratings, and supported episode history.

When records require review, eligible records can be prepared
and approved separately. Related records with unresolved
identities or conflicts are conservatively excluded.

Simkl anime episode mappings require review rather than
assuming that source episode numbers match TMDB.

See [Selective Tracker Import Approval](docs/import-selective-approval.md)
for the workflow, safeguards, and limitations.

### Import history and undo

The private import prompt includes **Import History** and
**Cancel Import** controls.

Choose a journaled import to preview the number of titles that
can be restored and the number that must be retained.

- Undo requires explicit confirmation and a verified database backup.
- Only the requesting user's eligible personal records are restored.
- Subsequent changes to an imported title invalidate automatic undo
  eligibility for that title.
- Shared media metadata and other users' records are preserved.
- Older imports made before journal support cannot be automatically undone.
- A completed partial undo closes its batch and preserves skipped titles.

Cancellation discards the unfinished session. It does not erase
previously committed library data.

See [Private Member Imports](docs/private-member-imports.md)
for access requirements and operational limitations.

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
set. Member imports additionally require successful verification of the
requesting user's current group membership. Telegram only guarantees
membership lookup for other users when the bot is a group administrator.
If verification is unavailable, member imports are denied.

## Supported runtime

Kokabiel is deployed on ARAKIEL, a Raspberry Pi 5 running
Arch Linux ARM. Windows runtime support is outside project scope.

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
├── imports/                 # tracker intake, resolution, and transactions
├── tests/                   # automated regression tests
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
python -m unittest discover -s tests -v
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


## Personal Media Library and Release Intelligence

Kokabiel provides an interactive, paginated personal media
library with category filters, sorting, viewing progress,
watchlists and ratings.

Commands:

- `/mylibrary` — browse your complete personal library.
- `/mywatchlist` — browse your personal watchlist.
- `/watched` — browse watched titles.
- `/myprogress` — browse viewing progress.
- `/myratings` — browse your personal ratings.
- `/myreleases` — view upcoming releases from tracked titles.

The release schedule supports 7, 30 and 90-day windows,
persistent metadata caching and user-requested refresh.
Choose a single batch or refresh all tracked titles with
background progress reporting.

See [Media Library and Release Intelligence](docs/media-library-release-intelligence.md)
for behavior, metadata limitations and migration information.
