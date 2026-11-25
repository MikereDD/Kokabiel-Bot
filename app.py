from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

import aiohttp
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

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
from handlers.imports import (
    import_command,
    receive_import_document,
)
from handlers.import_tasks import (
    IMPORT_TASK_MANAGER_KEY,
    ImportTaskManager,
    dispatch_callback,
    dispatch_import_command,
    dispatch_import_document,
    import_task_manager,
)
from handlers.owner import (
    RESTART_REQUESTED_KEY,
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
    BotCommand(
        "import",
        "Import tracker data privately",
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


async def post_stop(application: Application) -> None:
    await import_task_manager(application).drain()


async def _dispatch_import_command(update, context) -> None:
    await dispatch_import_command(
        update,
        context,
        import_command,
    )


async def _dispatch_import_document(update, context) -> None:
    await dispatch_import_document(
        update,
        context,
        receive_import_document,
    )


async def _dispatch_callback(update, context) -> None:
    await dispatch_callback(
        update,
        context,
        callback_router,
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
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
    )
    if settings.local_bot_api_url:
        builder = builder.base_url(settings.local_bot_api_url)
    if settings.local_bot_api_file_url:
        builder = builder.base_file_url(settings.local_bot_api_file_url)

    application = builder.build()
    application.bot_data[
        IMPORT_TASK_MANAGER_KEY
    ] = ImportTaskManager()

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
    application.add_handler(CommandHandler("import", _dispatch_import_command))

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.Document.ALL,
            _dispatch_import_document,
        )
    )

    application.add_handler(CommandHandler("adminhelp", adminhelp))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("groups", groups))
    application.add_handler(CommandHandler("leave", leave))
    application.add_handler(CommandHandler("leavechat", leavechat))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reload", reload_config))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("shutdown", shutdown))

    application.add_handler(CallbackQueryHandler(_dispatch_callback))
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

    # A successful return occurs after framework shutdown
    # and the configured post_shutdown resource cleanup.
    if application.bot_data.get(RESTART_REQUESTED_KEY, False):
        LOGGER.info("Graceful shutdown complete; restarting Kokabiel")

        os.execv(
            sys.executable,
            [sys.executable, *sys.argv],
        )


if __name__ == "__main__":
    main()
