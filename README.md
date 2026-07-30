# Kokabiel — Film, TV & Documentary Guide

Kokabiel is a command-and-button-driven Telegram bot for finding films, TV series, and documentaries and maintaining a shared group watchlist, watched list, ratings, and recommendations.

It does not monitor ordinary group conversation. It responds only to commands and its own buttons.

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

Clone the repository into the same bot workspace used by the other bots:

```bash
cd "$HOME/bots"
git clone <FORGEJO-SSH-URL> Kokabiel
cd Kokabiel
```

Install the two Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Create the external configuration beside the other bot configs:

```bash
mkdir -p "$HOME/bots/config"
cp kokabielrc.example.py "$HOME/bots/config/kokabielrc.py"
chmod 600 "$HOME/bots/config/kokabielrc.py"
vim "$HOME/bots/config/kokabielrc.py"
```

Add the Telegram BotFather token, TMDB API Read Access Token, and your Telegram user ID:

```python
OWNER_USER_IDS = [123456789]
```

Only these IDs can use owner commands. Telegram group administrators do not inherit owner access.

## Run

```bash
cd "$HOME/bots/Kokabiel"
python kokabiel.py
```

No package installation, `src/` layout, virtual command, environment variable, source manifest, deployment folder, or generated executable is required.

## First test

In a private Telegram chat with Kokabiel:

```text
/start
/movie The Thing
```

Kokabiel shows six matching titles at a time. Use **More results** or **Previous** to move through longer result lists, then choose the correct release and test Watchlist, Rate, Recommend, `/watchlist`, and `/random`.

## Commands

- `/movie <title>`
- `/tv <title>`
- `/documentary <title>`
- `/search <title>`
- `/watchlist`
- `/watched`
- `/random`
- `/about`
- `/help`

## Owner commands

Owner commands are checked against `OWNER_USER_IDS` in the private config.

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

## TMDB attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
