from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath
from secrets import token_hex
from typing import Literal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.common import (
    allowed,
    reply_personal_text,
    settings,
    tmdb,
    user_identity,
)
from imports.db_apply import (
    ImportApplyError,
    ImportBackupError,
    ImportPreWriteError,
    ImportPreviewStaleError,
    apply_database_import,
)
from imports.db_preview import (
    ImportDatabasePreview,
    load_existing_user_snapshot,
)
from imports.detect import ImportDetectionError
from imports.merge import analyze_import
from imports.parse import ImportParseError
from imports.resolve import (
    ResolutionPreview,
    resolve_import_plan,
)
from imports.selective import (
    SelectiveImportPreview,
    build_selective_import_preview,
)
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
    "preparing_verified",
    "verified_ready",
    "applying",
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

    if chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        await reply_personal_text(
            update,
            context,
            (
                "📥 <b>Import tracker data</b>\n\n"
                "Tracker export uploads are not accepted in "
                "group chats yet. Telegram can keep the "
                "/import command private, but an ordinary "
                "document uploaded by you to the group is not "
                "a private bot upload.\n\n"
                "Kokabiel will not ask you to expose your "
                "watch history or ratings to the group."
            ),
            parse_mode="HTML",
        )
        return

    if chat.type != ChatType.PRIVATE:
        return

    current = context.user_data.get(
        IMPORT_SESSION_KEY
    )

    if (
        isinstance(current, PendingImport)
        and current.state in {
            "processing",
            "preparing_verified",
            "applying",
        }
    ):
        await reply_personal_text(
            update,
            context,
            (
                "An import operation is already in progress. "
                "Please allow it to complete before starting "
                "another import."
            ),
        )
        return

    _discard_pending_import(
        context
    )

    user_id, user_name = (
        user_identity(update)
    )

    pending = PendingImport(
        token=token_hex(16),
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


async def _run_import_worker(function, *args, **kwargs):
    loop = asyncio.get_running_loop()

    worker = loop.run_in_executor(
        None,
        partial(function, *args, **kwargs),
    )

    try:
        return await asyncio.shield(worker)

    except asyncio.CancelledError:
        # Cancelling the awaiting coroutine does not stop
        # an already-running executor thread.
        while not worker.done():
            try:
                await asyncio.shield(worker)

            except asyncio.CancelledError:
                continue

            except Exception:
                break

        # Retrieve any worker exception before propagating
        # the original cancellation.
        if not worker.cancelled():
            try:
                worker.result()

            except Exception:
                LOGGER.exception(
                    "Import worker failed during cancellation"
                )

        raise


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

    # Never ingest a member's ordinary group document as personal
    # tracker data. Group uploads are visible outside the private
    # ephemeral interaction used by Kokabiel.
    if chat.type != ChatType.PRIVATE:
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

    if context.user_data.get(
        IMPORT_SESSION_KEY
    ) is not pending:
        return

    if pending.state != "awaiting_upload":
        await reply_personal_text(
            update,
            context,
            (
                "An import is already being processed or "
                "waiting for your decision. Wait for active "
                "operations to finish before starting another "
                "import, or use the available preview buttons."
            ),
        )
        return

    document = message.document

    try:
        source_name = _safe_upload_name(
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

    status_message = None

    async def update_status(text: str) -> None:
        if status_message is None:
            return

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not processing:
            return

        try:
            await status_message.edit_text(
                text=text,
                parse_mode=None,
                reply_markup=None,
            )
        except Exception as exc:
            LOGGER.warning(
                "Import progress update failed (%s)",
                type(exc).__name__,
            )

    async def deliver(
        text: str,
        *,
        parse_mode=None,
        reply_markup=None,
    ) -> None:
        expected = context.user_data.get(
            IMPORT_SESSION_KEY
        )

        if status_message is not None:
            try:
                await status_message.edit_text(
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return

            except Exception as exc:
                LOGGER.warning(
                    "Import message edit failed (%s)",
                    type(exc).__name__,
                )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not expected:
            return

        await reply_personal_text(
            update,
            context,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    try:
        # Uploads have already passed private-chat, ownership,
        # size and session validation.
        try:
            status_message = await message.reply_text(
                "📥 Kokabiel — Importing your library\n\n"
                "⏳ Downloading your tracker export…\n\n"
                "No import changes have been applied.",
                parse_mode=None,
                reply_markup=None,
            )
        except Exception as exc:
            LOGGER.warning(
                "Import progress acknowledgement failed (%s)",
                type(exc).__name__,
            )

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

        await update_status(
            "📥 Kokabiel — Importing your library\n\n"
            "✅ Export received\n"
            "⏳ Analyzing tracker records…\n\n"
            "No import changes have been applied."
        )

        plan = await _run_import_worker(
            analyze_import,
            str(upload_path),
            source_name=source_name,
        )

        await update_status(
            "📥 Kokabiel — Importing your library\n\n"
            "✅ Export received\n"
            "✅ Tracker records analyzed\n"
            "⏳ Resolving media identities…\n\n"
            "Large collections may take longer.\n"
            "No import changes have been applied."
        )

        resolution = (
            await resolve_import_plan(
                plan,
                tmdb(context),
            )
        )

        await update_status(
            "📥 Kokabiel — Importing your library\n\n"
            "✅ Export analyzed\n"
            "✅ Media identity resolution finished\n"
            "⏳ Comparing with your existing collection…\n\n"
            "Preparing a preview of new and updated records.\n"
            "No import changes have been applied."
        )

        selective = await asyncio.to_thread(
            preview_selective_database_import,
            resolution,
            database_path=settings(
                context
            ).database_path,
            user_id=pending.user_id,
        )

        preview = selective.full

        # The transactional writer needs only the approved preview.
        # Do not retain a member's raw tracker export while they decide.
        _delete_path(
            upload_path
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not processing:
            return

        processing = replace(
            processing,
            path=None,
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = processing

    except asyncio.CancelledError:
        _delete_path(upload_path)

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is processing:
            _discard_pending_import(context)

        raise

    except (
        ImportSecurityError,
        ImportDetectionError,
        ImportParseError,
    ) as exc:
        _delete_path(
            upload_path
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not processing:
            return

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await deliver(
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

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not processing:
            return

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await deliver(
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

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not processing:
            return

        context.user_data[
            IMPORT_SESSION_KEY
        ] = replace(
            pending,
            state="awaiting_upload",
            path=None,
            preview=None,
        )

        await deliver(
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

    # Keep the session non-actionable while delivering
    # its initial preview. The raw upload is already removed.
    try:
        await deliver(
            format_import_preview(
                preview
            ),
            parse_mode="HTML",
            reply_markup=(
                import_preview_keyboard(
                    preview,
                    pending.token,
                    selective=selective,
                )
            ),
        )

    except asyncio.CancelledError:
        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is processing:
            _discard_pending_import(context)

        raise

    except Exception as exc:
        LOGGER.warning(
            "Import preview delivery failed for user %s (%s)",
            pending.user_id,
            type(exc).__name__,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is processing:
            _discard_pending_import(context)

        return

    if context.user_data.get(
        IMPORT_SESSION_KEY
    ) is processing:
        context.user_data[
            IMPORT_SESSION_KEY
        ] = ready


def preview_selective_database_import(
    resolution: ResolutionPreview,
    *,
    database_path: str | Path,
    user_id: int,
) -> SelectiveImportPreview:
    snapshot = load_existing_user_snapshot(
        database_path,
        user_id,
    )

    return build_selective_import_preview(
        resolution,
        snapshot,
    )


def refresh_selective_import_preview(
    preview: ImportDatabasePreview,
    *,
    database_path: str | Path,
    user_id: int,
) -> SelectiveImportPreview:
    if preview.user_id != user_id:
        raise ValueError(
            "Import preview ownership mismatch."
        )

    resolution = ResolutionPreview(
        source=preview.source,
        raw_record_count=preview.raw_record_count,
        items=tuple(
            item.resolution
            for item in preview.items
        ),
    )

    return preview_selective_database_import(
        resolution,
        database_path=database_path,
        user_id=user_id,
    )


async def _edit_import_message_safely(
    query,
    text: str,
    **kwargs,
) -> None:
    try:
        await query.edit_message_text(
            text,
            **kwargs,
        )

    except Exception as exc:
        LOGGER.warning(
            "Import status message delivery failed (%s)",
            type(exc).__name__,
        )


async def import_callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    parts = (
        query.data or ""
    ).split("|")

    if (
        len(parts) != 3
        or parts[0] != "imp"
    ):
        await query.answer(
            "That import action is no longer valid.",
            show_alert=True,
        )
        return

    if not await allowed(
        update,
        context,
    ):
        return

    chat = update.effective_chat
    user = update.effective_user

    if (
        chat is None
        or user is None
        or chat.type != ChatType.PRIVATE
    ):
        await query.answer(
            "Import actions are available only in a private chat.",
            show_alert=True,
        )
        return

    action = parts[1]
    token = parts[2]

    pending = context.user_data.get(
        IMPORT_SESSION_KEY
    )

    if (
        not isinstance(
            pending,
            PendingImport,
        )
        or pending.token != token
        or pending.user_id != user.id
        or pending.chat_id != chat.id
    ):
        await query.answer(
            "That import preview expired or belongs to another session.",
            show_alert=True,
        )
        return

    if pending.state in {
        "processing",
        "preparing_verified",
        "applying",
    }:
        await query.answer(
            "An import operation is already in progress.",
            show_alert=True,
        )
        return

    if action == "cancel":
        _discard_pending_import(
            context
        )

        await query.answer(
            "Import cancelled."
        )

        await query.edit_message_text(
            "Import cancelled. No changes were requested."
        )
        return

    if (
        pending.state not in {
            "ready",
            "verified_ready",
        }
        or pending.preview is None
    ):
        await query.answer(
            "That import preview is no longer ready.",
            show_alert=True,
        )
        return

    preview = pending.preview

    if action == "prepare_verified":
        if (
            pending.state != "ready"
            or not preview.review_count
            or preview.user_id != pending.user_id
        ):
            await query.answer(
                "This preview cannot prepare a verified import.",
                show_alert=True,
            )
            return

        preparing = replace(
            pending,
            state="preparing_verified",
        )

        context.user_data[
            IMPORT_SESSION_KEY
        ] = preparing

        confirmation = None

        try:
            await query.answer(
                "Preparing verified records…"
            )

            selective = await asyncio.to_thread(
                refresh_selective_import_preview,
                preview,
                database_path=settings(
                    context
                ).database_path,
                user_id=pending.user_id,
            )

            # Never revive a replaced or invalidated session.
            if context.user_data.get(
                IMPORT_SESSION_KEY
            ) is not preparing:
                return

            # Fail closed if database changes altered the full
            # preview or the selective subset is unavailable.
            if (
                selective.full != preview
                or not selective_import_available(selective)
            ):
                _discard_pending_import(context)

                await _edit_import_message_safely(
                    query,
                    "The import preview changed or no verified "
                    "changes remain. Run /import again."
                )
                return

            confirmation_token = token_hex(16)

            if confirmation_token == pending.token:
                raise RuntimeError(
                    "Confirmation token was not rotated."
                )

            confirmation = replace(
                pending,
                token=confirmation_token,
                state="verified_ready",
                preview=selective.eligible,
            )

            await query.edit_message_text(
                format_selective_import_preview(
                    selective
                ),
                reply_markup=selective_import_keyboard(
                    confirmation_token
                ),
                parse_mode="HTML",
            )

            if context.user_data.get(
                IMPORT_SESSION_KEY
            ) is preparing:
                context.user_data[
                    IMPORT_SESSION_KEY
                ] = confirmation

        except asyncio.CancelledError:
            current = context.user_data.get(
                IMPORT_SESSION_KEY
            )

            if (
                current is preparing
                or (
                    confirmation is not None
                    and current is confirmation
                )
            ):
                _discard_pending_import(context)

            raise

        except Exception:
            LOGGER.exception(
                "Verified import preparation failed for user %s",
                pending.user_id,
            )

            current = context.user_data.get(
                IMPORT_SESSION_KEY
            )

            if (
                current is preparing
                or (
                    confirmation is not None
                    and current is confirmation
                )
            ):
                _discard_pending_import(context)

                await _edit_import_message_safely(
                    query,
                    "Kokabiel could not prepare a verified "
                    "import safely. Run /import again."
                )

        return

    if (
        pending.state == "verified_ready"
        and action != "apply_verified"
    ):
        await query.answer(
            "This confirmation authorizes only a verified import.",
            show_alert=True,
        )
        return

    if action == "review":
        if not preview.review_count:
            await query.answer(
                "This preview has no review-blocking items.",
                show_alert=True,
            )
            return

        await query.answer(
            (
                f"{preview.review_count} item(s) still require review. "
                "Kokabiel will not apply this import automatically."
            ),
            show_alert=True,
        )
        return

    expected_action = (
        "apply_verified"
        if pending.state == "verified_ready"
        else "apply"
    )

    if action != expected_action:
        await query.answer(
            "That import action is not valid.",
            show_alert=True,
        )
        return

    if preview.user_id != pending.user_id:
        await query.answer(
            "That import preview belongs to another user.",
            show_alert=True,
        )
        return

    if preview.review_count:
        await query.answer(
            "This import still contains review-required items.",
            show_alert=True,
        )
        return

    if not any(
        item.has_changes
        for item in preview.items
    ):
        await query.answer(
            "There is nothing new to import.",
            show_alert=True,
        )
        return

    applying = replace(
        pending,
        state="applying",
    )

    context.user_data[
        IMPORT_SESSION_KEY
    ] = applying

    try:
        await query.answer(
            "Applying approved import…"
        )

    except asyncio.CancelledError:
        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is applying:
            _discard_pending_import(context)

        raise

    except Exception as exc:
        LOGGER.warning(
            "Import acknowledgement failed for user %s (%s)",
            pending.user_id,
            type(exc).__name__,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not applying:
            return

        _discard_pending_import(context)

        await _edit_import_message_safely(
            query,
            "Import could not start. No changes were made. "
            "Run /import again."
        )

        return

    if context.user_data.get(
        IMPORT_SESSION_KEY
    ) is not applying:
        return

    try:
        try:
            current_settings = settings(
                context
            )

        except Exception as exc:
            raise ImportPreWriteError(
                "Import settings could not be prepared."
            ) from exc

        try:
            backup_path = _backup_path(
                current_settings.database_path,
                pending.user_id,
                pending.token,
            )

            _prepare_backup_directory(
                backup_path.parent
            )

        except Exception as exc:
            raise ImportBackupError(
                "Backup destination could not be prepared."
            ) from exc

        result = await _run_import_worker(
            apply_database_import,
            preview,
            database_path=(
                current_settings.database_path
            ),
            backup_path=backup_path,
            user_name=pending.user_name,
            chat_id=pending.chat_id,
        )

    except asyncio.CancelledError:
        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is applying:
            _discard_pending_import(context)

        LOGGER.warning(
            "Import handler cancelled after worker completion "
            "for user %s",
            pending.user_id,
        )

        raise

    except ImportPreviewStaleError:
        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not applying:
            return

        _discard_pending_import(
            context
        )

        await _edit_import_message_safely(
            query,
            (
                "The database changed after this preview was created. "
                "Nothing further was attempted from this preview. "
                "Run /import again to build a fresh one."
            )
        )
        return

    except (ImportBackupError, ImportPreWriteError) as exc:
        LOGGER.exception(
            "Import failed before mutations for user %s (%s)",
            pending.user_id,
            type(exc).__name__,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not applying:
            return

        _discard_pending_import(context)

        if isinstance(exc, ImportBackupError):
            message = (
                "Kokabiel could not prepare a verified "
                "database backup. "
                "No import changes were made. "
                "Run /import again before retrying."
            )

        else:
            message = (
                "Kokabiel could not start the database import. "
                "No import changes were made. "
                "Run /import again before retrying."
            )

        await _edit_import_message_safely(
            query,
            message,
        )

        return

    except ImportApplyError:
        LOGGER.exception(
            "Transactional import failed for user %s",
            pending.user_id,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not applying:
            return

        _discard_pending_import(
            context
        )

        await _edit_import_message_safely(
            query,
            (
                "Kokabiel could not complete this import safely. "
                "Run /import again before retrying."
            )
        )
        return

    except Exception:
        LOGGER.exception(
            "Unexpected import apply failure for user %s",
            pending.user_id,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not applying:
            return

        _discard_pending_import(
            context
        )

        await _edit_import_message_safely(
            query,
            (
                "Kokabiel could not verify that this import completed "
                "normally. Run /import again before retrying."
            )
        )
        return

    if context.user_data.get(
        IMPORT_SESSION_KEY
    ) is not applying:
        return

    _discard_pending_import(
        context
    )

    await _edit_import_message_safely(
        query,
        format_import_result(
            result
        ),
        parse_mode="HTML",
    )


def format_import_result(
    result,
) -> str:
    if result.already_applied:
        return (
            "✅ <b>Import already up to date</b>\n\n"
            "Your current records already satisfy this approved preview. "
            "No duplicate changes were added."
        )

    return "\n".join(
        [
            "✅ <b>Import complete</b>",
            "",
            f"• Catalog rows added: {result.media_added}",
            f"• Personal states added: {result.states_added}",
            f"• Personal states updated: {result.states_updated}",
            f"• Episodes added: {result.episodes_added}",
            f"• Ratings added: {result.ratings_added}",
            "",
            "A verified pre-import database backup was created.",
        ]
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
    *,
    selective: SelectiveImportPreview | None = None,
) -> InlineKeyboardMarkup:
    has_changes = any(
        item.has_changes
        for item in preview.items
    )

    rows: list[
        list[InlineKeyboardButton]
    ] = []

    if preview.review_count:
        if (
            selective is not None
            and selective.full == preview
            and selective_import_available(selective)
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        "📥 Prepare Verified Import",
                        callback_data=(
                            f"imp|prepare_verified|{token}"
                        ),
                    )
                ]
            )

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


def selective_import_available(
    selective: SelectiveImportPreview,
) -> bool:
    return (
        selective.full.review_count > 0
        and bool(selective.excluded)
        and selective.eligible.review_count == 0
        and selective.full.user_id == selective.eligible.user_id
        and selective.full.source == selective.eligible.source
        and any(
            item.has_changes
            for item in selective.eligible.items
        )
    )


def selective_import_keyboard(
    token: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Import Verified Records",
                    callback_data=(
                        f"imp|apply_verified|{token}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=(
                        f"imp|cancel|{token}"
                    ),
                )
            ],
        ]
    )


def format_selective_import_preview(
    selective: SelectiveImportPreview,
) -> str:
    eligible = selective.eligible

    return "\n".join(
        [
            "📥 <b>Confirm verified import</b>",
            "",
            f"<b>Source:</b> {eligible.source.capitalize()}",
            "",
            "<b>Import scope</b>",
            f"• Total source records: {selective.full.raw_record_count}",
            f"• Eligible source records: {eligible.raw_record_count}",
            f"• Excluded source records: {selective.excluded_raw_record_count}",
            f"• Eligible merged items: {len(eligible.items)}",
            f"• Excluded merged items: {len(selective.excluded)}",
            f"• Review-required items: {selective.full.review_count}",
            f"• Dependency-excluded items: {selective.dependency_excluded_count}",
            "",
            "<b>Planned database changes</b>",
            f"• New catalog rows: {eligible.media_add_count}",
            f"• Personal states added: {eligible.state_add_count}",
            f"• Personal states updated: {eligible.state_update_count}",
            f"• Episodes added: {eligible.episode_add_count}",
            f"• Ratings added: {eligible.rating_add_count}",
            f"• Unchanged/skipped: {eligible.skip_count}",
            "",
            "🛡 <b>Import safeguards</b>",
            "Excluded records and their linked dependencies "
            "are outside this approval.",
            "Existing ratings and episode history are protected "
            "by transactional validation.",
            "",
            "No changes have been written.",
            "Choose <b>Import Verified Records</b> to approve "
            "only this exact eligible preview.",
        ]
    )


def _safe_upload_name(
    file_name: str | None,
) -> str:
    if not file_name:
        raise ValueError(
            "The uploaded document must have a CSV, JSON, "
            "or ZIP filename."
        )

    name = PurePosixPath(
        file_name.replace("\\", "/")
    ).name

    if not name:
        raise ValueError(
            "The uploaded document must have a CSV, JSON, "
            "or ZIP filename."
        )

    suffix = Path(
        name
    ).suffix.casefold()

    if (
        suffix
        not in ALLOWED_UPLOAD_SUFFIXES
    ):
        raise ValueError(
            "Only CSV, JSON, and ZIP tracker exports are accepted."
        )

    return name


def _safe_upload_suffix(
    file_name: str | None,
) -> str:
    return Path(
        _safe_upload_name(file_name)
    ).suffix.casefold()


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


def _backup_path(
    database_path: Path,
    user_id: int,
    token: str,
) -> Path:
    return (
        database_path.resolve().parent
        / "imports"
        / "backups"
        / str(user_id)
        / f"preimport-{token}.db"
    )


def _prepare_backup_directory(
    directory: Path,
) -> None:
    backup_root = (
        directory.parent
    )

    backup_root.mkdir(
        parents=True,
        exist_ok=True,
    )
    backup_root.chmod(
        0o700
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    directory.chmod(
        0o700
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
