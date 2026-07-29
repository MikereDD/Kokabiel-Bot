from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from config import Settings
from database import Database
from tmdb import TmdbClient


def settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def tmdb(context: ContextTypes.DEFAULT_TYPE) -> TmdbClient:
    return context.application.bot_data["tmdb"]


async def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    configured = settings(context).allowed_chat_ids
    if chat is not None and (not configured or chat.id in configured):
        return True

    if update.callback_query:
        await update.callback_query.answer("This bot is not enabled in this chat.", show_alert=True)
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
