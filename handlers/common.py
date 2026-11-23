from __future__ import annotations

import logging

from telegram import ReplyParameters, Update
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


def _ephemeral_message_id(message) -> int | None:
    if message is None:
        return None
    value = (message.api_kwargs or {}).get("ephemeral_message_id")
    return value if isinstance(value, int) else None


async def reply_personal_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode=None,
):
    """Send personal output without exposing it to the group."""

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    if chat is None or user is None or message is None:
        return None

    # Owner-private access remains a normal private-chat response.
    if chat.type == ChatType.PRIVATE:
        return await message.reply_text(text, parse_mode=parse_mode)

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return None

    incoming_ephemeral_id = _ephemeral_message_id(message)

    kwargs = {
        "chat_id": chat.id,
        "text": text,
        "parse_mode": parse_mode,
        "api_kwargs": {
            "ephemeral_message_parameters": {
                "receiver_user_id": user.id,
            },
        },
    }

    # Non-admin ephemeral interactions can be tied to the incoming
    # ephemeral command. Administrators may send directly to the user.
    if incoming_ephemeral_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=None,
            api_kwargs={
                "ephemeral_message_id": incoming_ephemeral_id,
            },
        )

    return await context.bot.send_message(**kwargs)


async def send_personal_callback_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode=None,
    reply_markup=None,
):
    """Send a private in-group response to the user who pressed a button."""

    query = update.callback_query
    chat = update.effective_chat
    user = update.effective_user

    if query is None or chat is None or user is None:
        return None

    return await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        api_kwargs={
            "ephemeral_message_parameters": {
                "receiver_user_id": user.id,
            },
        },
    )


async def edit_personal_text(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    receiver_user_id: int,
    text: str,
    *,
    parse_mode=None,
    reply_markup=None,
) -> None:
    """Edit either a normal private reply or an ephemeral group reply."""

    ephemeral_id = _ephemeral_message_id(message)
    if ephemeral_id is None:
        await message.edit_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return

    data = {
        "chat_id": message.chat.id,
        "receiver_user_id": receiver_user_id,
        "ephemeral_message_id": ephemeral_id,
        "text": text,
        "reply_markup": (
            reply_markup.to_dict()
            if reply_markup is not None
            else {"inline_keyboard": []}
        ),
    }
    if parse_mode is not None:
        data["parse_mode"] = parse_mode

    await context.bot._post(
        "editEphemeralMessageText",
        data=data,
    )


async def delete_personal_message(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    receiver_user_id: int,
) -> None:
    """Delete either a normal private message or an ephemeral group message."""

    ephemeral_id = _ephemeral_message_id(message)
    if ephemeral_id is None:
        await message.delete()
        return

    await context.bot._post(
        "deleteEphemeralMessage",
        data={
            "chat_id": message.chat.id,
            "receiver_user_id": receiver_user_id,
            "ephemeral_message_id": ephemeral_id,
        },
    )


async def allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
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

    allowed_group_chat = (
        chat is not None
        and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and (not configured or chat.id in configured)
    )

    if owner_private_chat or allowed_group_chat:
        try:
            await database(context).record_chat(
                chat_id=chat.id,
                chat_type=chat.type,
                title=_chat_title(update),
                username=chat.username,
            )
            if allowed_group_chat and user is not None:
                await database(context).record_user_chat(
                    chat_id=chat.id,
                    user_id=user.id,
                    user_name=user.full_name or user.username or str(user.id),
                )
        except Exception:
            LOGGER.exception(
                "Could not record chat/user activity for chat %s",
                chat.id,
            )
        return True

    if update.callback_query:
        await update.callback_query.answer(
            "This bot is not enabled in this chat.",
            show_alert=True,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "This bot is not enabled in this chat."
        )
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
