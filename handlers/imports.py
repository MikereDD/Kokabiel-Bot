from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, replace
from pathlib import Path
from secrets import token_hex
from typing import Literal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from handlers.common import (
    allowed,
    reply_personal_text,
    settings,
    tmdb,
    user_identity,
)
from imports.db_preview import (
    ImportDatabasePreview,
    preview_database_import,
)
from imports.detect import ImportDetectionError
from imports.merge import analyze_import
from imports.parse import ImportParseError
from imports.resolve import resolve_import_plan
from imports.security import (
    ALLOWED_UPLOAD_SUFFIXES,
    DEFAULT_LIMITS,
    ImportSecurityError,
)
from tmdb import TmdbError


LOGGER = logging.getLogger(__name__)

IMPORT_SESSION_KEY = (
    "kokabiel_import_session"
)

ImportSessionState = Literal[
    "awaiting_upload",
    "processing",
    "ready",
]


@dataclass(frozen=True, slots=True)
class PendingImport:
    token: str
    user_id: int
    user_name: str
    chat_id: int
    state: ImportSessionState
    path: Path | None = None
    preview: ImportDatabasePreview | None = None


async def import_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await allowed(
        update,
        context,
    ):
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None:
        return

    _discard_pending_import(
        context
    )

    user_id, user_name = (
        user_identity(update)
    )

    pending = PendingImport(
        token=token_hex(4),
        user_id=user_id,
        user_name=user_name,
        chat_id=chat.id,
        state="awaiting_upload",
    )

    context.user_data[
        IMPORT_SESSION_KEY
    ] = pending

    await reply_personal_text(
        update,
        context,
        (
            "📥 <b>Import tracker data</b>\n\n"
            "Send your original tracker export as a "
            "<code>.csv</code>, <code>.json</code>, or "
            "<code>.zip</code> document.\n\n"
            "Supported sources: Trakt, IMDb, Letterboxd, "
            "and Simkl.\n\n"
            "Kokabiel will validate the file, resolve titles "
            "through TMDB, compare it with your existing "
            "personal state, and show a preview before "
            "anything can be written."
        ),
        parse_mode="HTML",
    )


async def receive_import_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Process a document only when this user explicitly armed /import.

    Ordinary Telegram documents are ignored silently.
    """

    pending = context.user_data.get(
        IMPORT_SESSION_KEY
    )

    if not isinstance(
        pending,
        PendingImport,
    ):
        return

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    if (
        chat is None
        or user is None
        or message is None
        or message.document is None
    ):
        return

    if (
        user.id != pending.user_id
        or chat.id != pending.chat_id
    ):
        return

    if not await allowed(
        update,
        context,
    ):
        return

    if pending.state != "awaiting_upload":
        await reply_personal_text(
            update,
            context,
            (
                "An import is already being processed or "
                "waiting for your decision. Use its buttons, "
                "or run /import again to replace it."
            ),
        )
        return

    document = message.document

    try:
        suffix = _safe_upload_suffix(
            document.file_name
        )
    except ValueError as exc:
        await reply_personal_text(
            update,
            context,
            str(exc),
        )
        return

    if (
        document.file_size is not None
        and document.file_size
        > DEFAULT_LIMITS.max_upload_bytes
    ):
        await reply_personal_text(
            update,
            context,
            (
                "That export is larger than Kokabiel's "
                "50 MiB import limit."
            ),
        )
        return

    upload_path = _quarantine_path(
        settings(
            context
        ).database_path,
        pending.user_id,
        pending.token,
        document.file_name,
    )

    if upload_path.exists():
        await reply_personal_text(
            update,
            context,
            (
                "That import session cannot safely create "
                "its quarantine file. Run /import again."
            ),
        )
        return

    _prepare_quarantine_directory(
        upload_path.parent
    )

    processing = replace(
        pending,
        state="processing",
        path=upload_path,
    )

    context.user_data[
        IMPORT_SESSION_KEY
    ] = processing

    try:
        telegram_file = (
            await context.bot.get_file(
                document.file_id
            )
        )

        await telegram_file.download_to_drive(
            custom_path=upload_path
        )

        upload_path.chmod(
            0o600
        )

        plan = await asyncio.to_thread(
            analyze_import,
            str(upload_path),
        )

        resolution = (
            await resolve_import_plan(
                plan,
                tmdb(context),
            )
        )

        preview = await asyncio.to_thread(
            preview_database_import,
            resolution,
            database_path=settings(
                context
            ).database_path,
            user_id=pending.user_id,
        )

    except (
        ImportSecurityError,
        ImportDetectionError,
        ImportParseError,
    ) as exc:
        _delete_path(
            upload_path
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await reply_personal_text(
            update,
            context,
            (
                "Import rejected by Kokabiel's intake "
                f"checks:\n\n{exc}\n\n"
                "You can send another native export without "
                "running /import again."
            ),
        )
        return

    except TmdbError:
        _delete_path(
            upload_path
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await reply_personal_text(
            update,
            context,
            (
                "TMDB could not resolve this import right now. "
                "No database changes were made. You can retry "
                "the upload."
            ),
        )
        return

    except Exception:
        LOGGER.exception(
            "Import preview failed for user %s",
            pending.user_id,
        )

        _delete_path(
            upload_path
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await reply_personal_text(
            update,
            context,
            (
                "Kokabiel could not build the import preview. "
                "No database changes were made."
            ),
        )
        return

    ready = replace(
        processing,
        state="ready",
        preview=preview,
    )

    context.user_data[
        IMPORT_SESSION_KEY
    ] = ready

    await reply_personal_text(
        update,
        context,
        format_import_preview(
            preview
        ),
        parse_mode="HTML",
        reply_markup=(
            import_preview_keyboard(
                preview,
                pending.token,
            )
        ),
    )


def format_import_preview(
    preview: ImportDatabasePreview,
) -> str:
    resolved_count = sum(
        item.resolution.status
        == "resolved"
        for item in preview.items
    )

    identity_review_count = sum(
        item.resolution.status
        == "needs_review"
        for item in preview.items
    )

    unresolved_count = sum(
        item.resolution.status
        == "unresolved"
        for item in preview.items
    )

    change_count = sum(
        item.has_changes
        for item in preview.items
    )

    source = (
        preview.source.capitalize()
    )

    lines = [
        "📋 <b>Import preview</b>",
        "",
        f"<b>Source:</b> {source}",
        (
            "<b>Source records:</b> "
            f"{preview.raw_record_count}"
        ),
        f"<b>Resolved:</b> {resolved_count}",
        (
            "<b>Identity review:</b> "
            f"{identity_review_count}"
        ),
        (
            "<b>Unresolved:</b> "
            f"{unresolved_count}"
        ),
        "",
        "<b>Planned database changes</b>",
        (
            "• New catalog rows: "
            f"{preview.media_add_count}"
        ),
        (
            "• Personal states added: "
            f"{preview.state_add_count}"
        ),
        (
            "• Personal states updated: "
            f"{preview.state_update_count}"
        ),
        (
            "• Episodes added: "
            f"{preview.episode_add_count}"
        ),
        (
            "• Ratings added: "
            f"{preview.rating_add_count}"
        ),
        (
            "• Unchanged/skipped: "
            f"{preview.skip_count}"
        ),
        (
            "• Items blocking import: "
            f"{preview.review_count}"
        ),
    ]

    if preview.review_count:
        lines.extend(
            [
                "",
                (
                    "⚠️ This import cannot be applied until "
                    "the review items are resolved."
                ),
            ]
        )

    elif change_count == 0:
        lines.extend(
            [
                "",
                (
                    "✅ Kokabiel found nothing new to import."
                ),
            ]
        )

    else:
        lines.extend(
            [
                "",
                (
                    "No changes have been written. Choose "
                    "<b>Import</b> to approve this exact preview."
                ),
            ]
        )

    return "\n".join(
        lines
    )


def import_preview_keyboard(
    preview: ImportDatabasePreview,
    token: str,
) -> InlineKeyboardMarkup:
    has_changes = any(
        item.has_changes
        for item in preview.items
    )

    rows: list[
        list[InlineKeyboardButton]
    ] = []

    if preview.review_count:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔎 Review",
                    callback_data=(
                        f"imp|review|{token}"
                    ),
                )
            ]
        )

    elif has_changes:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Import",
                    callback_data=(
                        f"imp|apply|{token}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "Cancel",
                callback_data=(
                    f"imp|cancel|{token}"
                ),
            )
        ]
    )

    return InlineKeyboardMarkup(
        rows
    )


def _safe_upload_suffix(
    file_name: str | None,
) -> str:
    if not file_name:
        raise ValueError(
            "The uploaded document must have a CSV, JSON, "
            "or ZIP filename."
        )

    suffix = Path(
        file_name
    ).suffix.casefold()

    if (
        suffix
        not in ALLOWED_UPLOAD_SUFFIXES
    ):
        raise ValueError(
            "Only CSV, JSON, and ZIP tracker exports are accepted."
        )

    return suffix


def _quarantine_path(
    database_path: Path,
    user_id: int,
    token: str,
    file_name: str | None,
) -> Path:
    suffix = _safe_upload_suffix(
        file_name
    )

    return (
        database_path.resolve().parent
        / "imports"
        / "quarantine"
        / str(user_id)
        / f"{token}{suffix}"
    )


def _prepare_quarantine_directory(
    directory: Path,
) -> None:
    quarantine_root = (
        directory.parent
    )

    quarantine_root.mkdir(
        parents=True,
        exist_ok=True,
    )
    quarantine_root.chmod(
        0o700
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    directory.chmod(
        0o700
    )


def _delete_path(
    path: Path | None,
) -> None:
    if path is None:
        return

    try:
        path.unlink(
            missing_ok=True
        )
    except OSError:
        LOGGER.warning(
            "Could not remove import quarantine file."
        )


def _discard_pending_import(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pending = context.user_data.pop(
        IMPORT_SESSION_KEY,
        None,
    )

    if isinstance(
        pending,
        PendingImport,
    ):
        _delete_path(
            pending.path
        )
