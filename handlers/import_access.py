"""Import-specific authorization without general private-chat access."""

from __future__ import annotations

from dataclasses import dataclass

from telegram.constants import ChatType
from telegram.error import TelegramError

from handlers.common import settings


@dataclass(frozen=True, slots=True)
class ImportOrigin:
    user_id: int
    group_id: int


def private_import_identity(update) -> int | None:
    """Accept only a real user's own direct conversation."""

    chat = update.effective_chat
    user = update.effective_user

    if (
        chat is None
        or user is None
        or chat.type != ChatType.PRIVATE
        or user.id <= 0
        or chat.id != user.id
        or getattr(user, "is_bot", False)
    ):
        return None

    return user.id


async def verify_member_origin(
    context,
    origin: ImportOrigin,
    user_id: int,
) -> bool:
    """Revalidate the exact user and originating authorized group."""

    if (
        not isinstance(origin, ImportOrigin)
        or user_id <= 0
        or origin.user_id != user_id
        or origin.group_id >= 0
    ):
        return False

    configured = settings(context).allowed_chat_ids

    # Ordinary members need an explicitly authorized group.
    # An empty configuration must not enable public imports.
    if origin.group_id not in configured:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat_id=origin.group_id,
            user_id=user_id,
        )
    except TelegramError:
        return False

    identity = getattr(member, "user", None)

    if (
        identity is None
        or identity.id != user_id
        or getattr(identity, "is_bot", False)
    ):
        return False

    if origin.group_id not in settings(context).allowed_chat_ids:
        return False

    status = member.status

    if status in {
        "creator",
        "administrator",
        "member",
    }:
        return True

    if status == "restricted":
        return getattr(member, "is_member", False) is True

    # Left, banned, unknown or unverifiable.
    return False


async def group_import_origin(
    update,
    context,
) -> ImportOrigin | None:
    """Authorize a named member's explicit group import request."""

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    if (
        chat is None
        or user is None
        or message is None
        or chat.type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }
        or chat.id >= 0
        or user.id <= 0
        or getattr(user, "is_bot", False)
        or getattr(message, "sender_chat", None) is not None
    ):
        return None

    origin = ImportOrigin(
        user_id=user.id,
        group_id=chat.id,
    )

    if not await verify_member_origin(
        context,
        origin,
        user.id,
    ):
        return None

    return origin
