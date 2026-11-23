from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from config import ConfigError, load_settings
from database import Database
from handlers.callbacks import callback_router
from handlers.commands import (
    about,
    documentary,
    help_command,
    movie,
    myprogress,
    mysuggestions,
    mywatchlist,
    random_pick,
    search,
    start,
    suggestions,
    tv,
    watched,
    watchlist,
)
from handlers.errors import error_handler
from handlers.owner import (
    adminhelp,
    groups,
    leave,
    leavechat,
    reload_config,
    restart,
    shutdown,
    status,
    whoami,
)
from logging_setup import configure_logging
from tmdb import TmdbClient
from version import __version__

LOGGER = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("movie", "Search for a film"),
    BotCommand("tv", "Search for a TV series"),
    BotCommand("documentary", "Search for a documentary"),
    BotCommand("search", "Search films and TV together"),
    BotCommand(
        "mywatchlist",
        "Show your personal watchlist",
        api_kwargs={"is_ephemeral": True},
    ),
    BotCommand(
        "watched",
        "Show your watched titles",
        api_kwargs={"is_ephemeral": True},
    ),
    BotCommand(
        "myprogress",
        "Show your media progress",
        api_kwargs={"is_ephemeral": True},
    ),
    BotCommand("watchlist", "Show the group watchlist"),
    BotCommand("suggestions", "Show group suggestions"),
    BotCommand(
        "mysuggestions",
        "Show your suggestions",
        api_kwargs={"is_ephemeral": True},
    ),
    BotCommand("random", "Pick from the group watchlist"),
    BotCommand("about", "Show credits and TMDB attribution"),
    BotCommand("help", "Show commands and privacy behavior"),
]


async def post_init(application: Application) -> None:
    settings = application.bot_data["settings"]
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    session = aiohttp.ClientSession(timeout=timeout)
    application.bot_data["http_session"] = session
    application.bot_data["tmdb"] = TmdbClient(
        session,
        settings.tmdb_bearer_token,
        language=settings.language,
        region=settings.region,
        include_adult=settings.include_adult,
    )
    application.bot_data["started_at"] = datetime.now(timezone.utc)
    await application.bot_data["database"].initialize()
    await application.bot.set_my_commands(COMMANDS)
    LOGGER.info(
        "Kokabiel v%s initialized with %s configured owner(s)",
        __version__,
        len(settings.owner_user_ids),
    )


async def post_shutdown(application: Application) -> None:
    session = application.bot_data.get("http_session")
    if session and not session.closed:
        await session.close()
    LOGGER.info("Kokabiel stopped")


def build_application() -> Application:
    settings = load_settings()
    configure_logging(settings.log_path, settings.log_level)

    builder = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if settings.local_bot_api_url:
        builder = builder.base_url(settings.local_bot_api_url)
    if settings.local_bot_api_file_url:
        builder = builder.base_file_url(settings.local_bot_api_file_url)

    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["database"] = Database(settings.database_path)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("movie", movie))
    application.add_handler(CommandHandler("tv", tv))
    application.add_handler(CommandHandler("documentary", documentary))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("watchlist", watchlist))
    application.add_handler(CommandHandler("mywatchlist", mywatchlist))
    application.add_handler(CommandHandler("watched", watched))
    application.add_handler(CommandHandler("myprogress", myprogress))
    application.add_handler(CommandHandler("suggestions", suggestions))
    application.add_handler(CommandHandler("mysuggestions", mysuggestions))
    application.add_handler(CommandHandler("random", random_pick))

    application.add_handler(CommandHandler("adminhelp", adminhelp))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("groups", groups))
    application.add_handler(CommandHandler("leave", leave))
    application.add_handler(CommandHandler("leavechat", leavechat))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reload", reload_config))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("shutdown", shutdown))

    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    try:
        application = build_application()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
