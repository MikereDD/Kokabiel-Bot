from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from config import Settings
from database import Database
from tmdb import TmdbClient

LOGGER = logging.getLogger(__name__)


def settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def tmdb(context: ContextTypes.DEFAULT_TYPE) -> TmdbClient:
    return context.application.bot_data["tmdb"]


async def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    current = settings(context)
    configured = current.allowed_chat_ids

    owner_private_chat = (
        chat is not None
        and chat.type == ChatType.PRIVATE
        and user is not None
        and user.id in current.owner_user_ids
    )
    configured_chat = (
        chat is not None
        and (not configured or chat.id in configured)
    )

    if owner_private_chat or configured_chat:
        try:
            await database(context).record_chat(
                chat_id=chat.id,
                chat_type=chat.type,
                title=_chat_title(update),
                username=chat.username,
            )
        except Exception:
            LOGGER.exception("Could not record chat %s", chat.id)
        return True

    if update.callback_query:
        await update.callback_query.answer(
            "This bot is not enabled in this chat.", show_alert=True
        )
    elif update.effective_message:
        await update.effective_message.reply_text("This bot is not enabled in this chat.")
    return False


def user_identity(update: Update) -> tuple[int, str]:
    user = update.effective_user
    if user is None:
        return 0, "Unknown member"
    return user.id, user.full_name or user.username or str(user.id)


def media_type_from_short(value: str) -> str:
    if value == "m":
        return "movie"
    if value == "t":
        return "tv"
    raise ValueError("invalid media type")


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
