from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from html import escape
from secrets import token_hex

import aiohttp
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from config import ConfigError, load_settings
from handlers.common import database, settings
from keyboards import owner_confirmation_keyboard
from logging_setup import configure_logging
from tmdb import TmdbClient
from version import __version__

LOGGER = logging.getLogger(__name__)
CONFIRMATION_TTL_SECONDS = 10 * 60

OWNER_HELP_TEXT = """<b>Kokabiel owner commands</b>

/adminhelp — show this owner-only command list
/whoami — show your Telegram user ID and the current chat ID
/groups — list groups Kokabiel has encountered
/leave — leave the current group after confirmation
/leavechat &lt;chat_id&gt; — leave a group from this private chat
/status — show Kokabiel runtime status
/reload — reload runtime-safe configuration
/restart — restart Kokabiel after confirmation
/shutdown — stop Kokabiel after confirmation

Only IDs in <code>OWNER_USER_IDS</code> can use these commands.
Group administrators receive no Kokabiel owner privileges.
"""


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id in settings(context).owner_user_ids


async def _require_owner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    private_only: bool = False,
) -> bool:
    if not _is_owner(update, context):
        if update.callback_query:
            await update.callback_query.answer("Owner-only action.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ Owner-only command.")
        return False

    chat = update.effective_chat
    if private_only and (chat is None or chat.type != ChatType.PRIVATE):
        if update.callback_query:
            await update.callback_query.answer(
                "Use this owner command in a private chat with Kokabiel.",
                show_alert=True,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "Use this owner command in a private chat with Kokabiel."
            )
        return False
    return True


async def adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    await update.effective_message.reply_text(OWNER_HELP_TEXT, parse_mode=ParseMode.HTML)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return

    user = update.effective_user
    chat = update.effective_chat
    title = _chat_title(update)
    await update.effective_message.reply_text(
        "<b>Kokabiel identity</b>\n\n"
        f"Owner: {_escape(user.full_name or user.username or str(user.id))}\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Chat: {_escape(title)}\n"
        f"Chat type: <code>{_escape(chat.type)}</code>\n"
        f"Chat ID: <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return

    known = await database(context).list_known_groups(include_inactive=True)
    if not known:
        await update.effective_message.reply_text(
            "Kokabiel has not encountered any groups yet."
        )
        return

    lines = ["<b>Kokabiel groups</b>", ""]
    for index, chat in enumerate(known, start=1):
        status = "active" if chat.is_active else "left"
        username = f" · @{_escape(chat.username)}" if chat.username else ""
        lines.extend(
            [
                f"{index}. <b>{_escape(chat.title)}</b>{username}",
                f"   <code>{chat.chat_id}</code> · {status}",
            ]
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return

    application = context.application
    current = settings(context)
    started_at = application.bot_data.get("started_at")
    uptime = _format_uptime(started_at)
    active_groups = await database(context).count_active_groups()
    me = await context.bot.get_me()
    allowed_text = (
        "all chats (testing mode)"
        if not current.allowed_chat_ids
        else str(len(current.allowed_chat_ids))
    )

    await update.effective_message.reply_text(
        "<b>Kokabiel status</b>\n\n"
        f"Version: <code>v{_escape(__version__)}</code>\n"
        f"Bot: @{_escape(me.username or me.first_name)}\n"
        f"Uptime: {_escape(uptime)}\n"
        f"Process ID: <code>{os.getpid()}</code>\n"
        f"Active groups: {active_groups}\n"
        f"Allowed chats: {allowed_text}\n"
        f"Owners: {len(current.owner_user_ids)}\n"
        f"TMDB region: {_escape(current.region)}\n"
        f"Local Bot API: {'yes' if current.local_bot_api_url else 'no'}\n"
        f"Database: <code>{_escape(str(current.database_path))}</code>\n"
        f"Log: <code>{_escape(str(current.log_path))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text(
            "Use /leave inside the group Kokabiel should leave."
        )
        return

    await database(context).record_chat(
        chat_id=chat.id,
        chat_type=chat.type,
        title=chat.title or str(chat.id),
        username=chat.username,
    )
    await _request_confirmation(
        update,
        context,
        action="leave",
        target_chat_id=chat.id,
        target_title=chat.title or str(chat.id),
        prompt=f"Leave <b>{_escape(chat.title or str(chat.id))}</b>?",
    )


async def leavechat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /leavechat <chat_id>")
        return

    try:
        target_chat_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("The chat ID must be an integer.")
        return

    try:
        target = await context.bot.get_chat(target_chat_id)
    except (BadRequest, Forbidden):
        await update.effective_message.reply_text(
            "Kokabiel cannot access that group ID. Check /groups and try again."
        )
        return

    if target.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.effective_message.reply_text("That ID is not a group chat.")
        return

    title = target.title or str(target_chat_id)
    await database(context).record_chat(
        chat_id=target_chat_id,
        chat_type=target.type,
        title=title,
        username=target.username,
    )
    await _request_confirmation(
        update,
        context,
        action="leave",
        target_chat_id=target_chat_id,
        target_title=title,
        prompt=f"Leave <b>{_escape(title)}</b>\n<code>{target_chat_id}</code>?",
    )


async def reload_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return

    old = settings(context)
    try:
        new = load_settings()
    except ConfigError as exc:
        await update.effective_message.reply_text(
            f"Configuration was not reloaded:\n<code>{_escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    restart_fields: list[str] = []
    if new.bot_token != old.bot_token:
        restart_fields.append("BOT_TOKEN")
    if new.database_path != old.database_path:
        restart_fields.append("DATABASE_PATH")
    if new.local_bot_api_url != old.local_bot_api_url:
        restart_fields.append("LOCAL_BOT_API_URL")
    if new.local_bot_api_file_url != old.local_bot_api_file_url:
        restart_fields.append("LOCAL_BOT_API_FILE_URL")

    if restart_fields:
        fields = ", ".join(restart_fields)
        await update.effective_message.reply_text(
            f"Those changes require /restart: <code>{_escape(fields)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    new_session = aiohttp.ClientSession(timeout=timeout)
    new_tmdb = TmdbClient(
        new_session,
        new.tmdb_bearer_token,
        language=new.language,
        region=new.region,
        include_adult=new.include_adult,
    )

    old_session = context.application.bot_data.get("http_session")
    context.application.bot_data["settings"] = new
    context.application.bot_data["http_session"] = new_session
    context.application.bot_data["tmdb"] = new_tmdb
    configure_logging(new.log_path, new.log_level)
    if old_session and not old_session.closed:
        await old_session.close()

    await update.effective_message.reply_text(
        "Configuration reloaded.\n"
        f"Owners: {len(new.owner_user_ids)} · "
        f"Allowed chats: {'all' if not new.allowed_chat_ids else len(new.allowed_chat_ids)}"
    )
    LOGGER.info("Runtime configuration reloaded by owner %s", update.effective_user.id)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return
    await _request_confirmation(
        update,
        context,
        action="restart",
        target_chat_id=update.effective_chat.id,
        target_title="Kokabiel",
        prompt="Restart <b>Kokabiel</b>?",
    )


async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context, private_only=True):
        return
    await _request_confirmation(
        update,
        context,
        action="shutdown",
        target_chat_id=update.effective_chat.id,
        target_title="Kokabiel",
        prompt="Shut down <b>Kokabiel</b>?",
    )


async def owner_callback_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or not await _require_owner(update, context):
        return

    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "own":
        await query.answer("That owner action is no longer valid.", show_alert=True)
        return

    decision, token = parts[1], parts[2]
    confirmations = context.user_data.get("kokabiel_owner_confirmations", {})
    pending = confirmations.pop(token, None)
    if pending is None:
        await query.answer("That confirmation expired or belongs to another owner.", show_alert=True)
        return

    if time.time() - pending["created_at"] > CONFIRMATION_TTL_SECONDS:
        await query.answer("That confirmation expired.", show_alert=True)
        return

    if decision == "no":
        await query.answer("Cancelled.")
        await query.edit_message_text("Owner action cancelled.")
        return
    if decision != "ok":
        await query.answer("That owner action is not valid.", show_alert=True)
        return

    action = pending["action"]
    if action == "leave":
        await _confirm_leave(update, context, pending)
    elif action == "restart":
        await query.answer("Restarting Kokabiel.")
        await query.edit_message_text("Restarting Kokabiel…")
        LOGGER.warning("Restart requested by owner %s", update.effective_user.id)
        asyncio.create_task(_restart_after_delay())
    elif action == "shutdown":
        await query.answer("Shutting down Kokabiel.")
        await query.edit_message_text("Shutting down Kokabiel…")
        LOGGER.warning("Shutdown requested by owner %s", update.effective_user.id)
        asyncio.create_task(_shutdown_after_delay())
    else:
        await query.answer("That owner action is not available.", show_alert=True)


async def _request_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    action: str,
    target_chat_id: int,
    target_title: str,
    prompt: str,
) -> None:
    token = token_hex(4)
    confirmations = context.user_data.setdefault("kokabiel_owner_confirmations", {})
    now = time.time()
    stale = [
        key
        for key, value in confirmations.items()
        if now - value.get("created_at", 0) > CONFIRMATION_TTL_SECONDS
    ]
    for key in stale:
        confirmations.pop(key, None)
    confirmations[token] = {
        "action": action,
        "target_chat_id": target_chat_id,
        "target_title": target_title,
        "created_at": now,
    }
    await update.effective_message.reply_text(
        prompt,
        parse_mode=ParseMode.HTML,
        reply_markup=owner_confirmation_keyboard(token),
    )


async def _confirm_leave(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict[str, object],
) -> None:
    query = update.callback_query
    target_chat_id = int(pending["target_chat_id"])
    title = str(pending["target_title"])
    source_chat_id = update.effective_chat.id

    await query.answer("Leaving group.")
    if source_chat_id == target_chat_id:
        await query.edit_message_text(f"Kokabiel is leaving {title}.")
        try:
            await context.bot.leave_chat(target_chat_id)
        except TelegramError:
            LOGGER.exception("Failed to leave chat %s", target_chat_id)
            return
    else:
        try:
            await context.bot.leave_chat(target_chat_id)
        except TelegramError as exc:
            LOGGER.exception("Failed to leave chat %s", target_chat_id)
            await query.edit_message_text(
                f"Kokabiel could not leave {title}:\n{exc}"
            )
            return
        await query.edit_message_text(f"Kokabiel left {title}.")

    await database(context).mark_chat_inactive(target_chat_id)
    LOGGER.warning(
        "Owner %s removed Kokabiel from chat %s (%s)",
        update.effective_user.id,
        target_chat_id,
        title,
    )


async def _restart_after_delay() -> None:
    await asyncio.sleep(1.0)
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def _shutdown_after_delay() -> None:
    await asyncio.sleep(1.0)
    os.kill(os.getpid(), signal.SIGINT)


def _format_uptime(started_at: object) -> str:
    if not isinstance(started_at, datetime):
        return "unknown"
    elapsed = datetime.now(timezone.utc) - started_at
    seconds = max(0, int(elapsed.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _chat_title(update: Update) -> str:
    chat = update.effective_chat
    if chat is None:
        return "Unknown chat"
    if chat.title:
        return chat.title
    user = update.effective_user
    if user:
        return user.full_name or user.username or str(user.id)
    return str(chat.id)


def _escape(value: object) -> str:
    return escape(str(value))
