# Kokabiel — Film, TV & Documentary Guide

Kokabiel is a command-and-button-driven Telegram bot for discovering films, TV series, miniseries, documentary films, and documentary series inside a real group chat.

It does not monitor ordinary conversation. It responds only to commands and its own buttons.

## Folder layout on Arakiel

```text
~/bots/
├── Kokabiel/
│   ├── kokabiel.py
│   ├── app.py
│   ├── database.py
│   ├── tmdb.py
│   ├── handlers/
│   ├── assets/
│   ├── requirements.txt
│   └── README.md
└── config/
    └── kokabielrc.py
```

## Setup

```bash
cd "$HOME/bots/Kokabiel"
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Create the private configuration beside the other bot configs:

```bash
mkdir -p "$HOME/bots/config"
cp kokabielrc.example.py "$HOME/bots/config/kokabielrc.py"
chmod 600 "$HOME/bots/config/kokabielrc.py"
vim "$HOME/bots/config/kokabielrc.py"
```

Add the Telegram BotFather token, TMDB API Read Access Token, and owner ID:

```python
OWNER_USER_IDS = [123456789]
```

Only configured owners can use owner commands. Telegram group administrators do not inherit owner access.

## Run

```bash
cd "$HOME/bots/Kokabiel"
.venv/bin/python kokabiel.py
```

Kokabiel remains a direct Python bot. It is not installed as a package.

## Media-aware cards

Kokabiel classifies each result before building the card:

- Film
- Documentary Film
- TV Series
- Limited Series
- Documentary Series
- Documentary Limited Series

Movie cards emphasize runtime and director. Series cards show series status, seasons, episodes, episode length, creators, and network/platform information.

## Personal behavior

Every viewing action is stored by Telegram chat ID and Telegram user ID.

- **My Watchlist** is personal.
- Movie and documentary-film watched status is personal.
- TV, limited-series, and documentary-series progress is personal.
- Ratings are personal; Kokabiel calculates a separate group average.
- Personal menus are tied to the member who opened them.

For series, **My Progress** supports:

- Marking individual released episodes watched or unwatched
- Marking a released season watched
- Clearing a season
- Marking all released episodes watched
- Setting a series to Watching
- Clearing all personal progress
- Automatic **Caught up** status for ongoing series
- Automatic **Completed** status for ended series and miniseries

## Group behavior

The shared card remains neutral because everyone in the group sees it.

- `/watchlist` aggregates titles currently saved by members.
- `/suggestions` shows titles members suggested to the group.
- Duplicate suggestions are combined while preserving who suggested the title.
- `/random` chooses from the aggregated group watchlist.

## Commands

### Discovery

- `/movie <title>`
- `/tv <title>`
- `/documentary <title>`
- `/search <title>`
- `/random`

### Personal

- `/mywatchlist`
- `/watched`
- `/myprogress`
- `/mysuggestions`

### Group

- `/watchlist`
- `/suggestions`

### General

- `/about`
- `/help`

## Owner commands

- `/adminhelp`
- `/whoami`
- `/groups`
- `/leave`
- `/leavechat <chat_id>`
- `/status`
- `/reload`
- `/restart`
- `/shutdown`

`/leave`, `/leavechat`, `/restart`, and `/shutdown` require confirmation. Sensitive runtime commands must be used in Kokabiel's private chat.

## Database migration

On first startup after this update, Kokabiel creates the personal-state, episode-progress, and group-suggestion tables automatically.

Existing group watchlist/watched entries are preserved by assigning each old entry to the member who originally added it. Existing recommendations are migrated into group suggestions.

## TMDB attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
