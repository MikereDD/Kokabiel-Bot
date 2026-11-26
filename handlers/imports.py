from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath
from secrets import token_hex
from time import monotonic
from typing import Literal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from handlers.common import (
    allowed,
    reply_personal_text,
    settings,
    tmdb,
    user_identity,
)
from handlers.import_access import (
    ImportOrigin,
    group_import_origin,
    private_import_identity,
    verify_member_origin,
)
from imports.undo import (
    ImportUndoPreview,
    apply_import_undo,
    list_import_batches,
    preview_import_undo,
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
    "undo_history",
    "undo_ready",
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
    origin: ImportOrigin | None = None
    expires_at: float | None = None
    undo_preview: ImportUndoPreview | None = None


IMPORT_ACTIVATION_KEY = "kokabiel_import_activation"
IMPORT_ACTIVATION_TTL = 600
MEMBER_IMPORT_SESSION_TTL = 86400

MEMBER_IMPORT_PROMPT = (
    "📥 <b>Import your tracker data</b>\n\n"
    "Send your original tracker export as a "
    "<code>.csv</code>, <code>.json</code>, or "
    "<code>.zip</code> document.\n\n"
    "Supported sources: Trakt, IMDb, Letterboxd, and Simkl.\n\n"
    "Your import is private. Kokabiel will show a preview "
    "before anything can be written."
)


@dataclass(frozen=True, slots=True)
class ImportActivation:
    token: str
    origin: ImportOrigin
    expires_at: float


def _member_import_blocked(context, user_id: int) -> str | None:
    from handlers.import_tasks import import_task_manager

    manager = import_task_manager(context.application)

    if not manager.accepting or not context.application.running:
        return "Kokabiel is shutting down. Try again after restart."

    pending = context.user_data.get(IMPORT_SESSION_KEY)

    if manager.busy(user_id) or (
        isinstance(pending, PendingImport)
        and pending.state in {
            "processing",
            "preparing_verified",
            "applying",
        }
    ):
        return "An import operation is already in progress."

    return None


def _import_entry_keyboard(token: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📚 Import History",
                callback_data=f"imp|history|{token}",
            )
        ],
        [
            InlineKeyboardButton(
                "Cancel Import",
                callback_data=f"imp|cancel|{token}",
            )
        ],
    ])


async def _open_member_dm(
    update,
    context,
    origin: ImportOrigin,
    *,
    allow_activation: bool,
) -> None:
    user_id, user_name = user_identity(update)

    if origin.user_id != user_id:
        return

    blocked = _member_import_blocked(context, user_id)

    if blocked:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=blocked,
            )
        except TelegramError:
            pass
        return

    context.user_data.pop(IMPORT_ACTIVATION_KEY, None)

    pending = PendingImport(
        token=token_hex(16),
        user_id=user_id,
        user_name=user_name,
        chat_id=user_id,
        state="awaiting_upload",
        origin=origin,
        expires_at=monotonic() + MEMBER_IMPORT_SESSION_TTL,
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=MEMBER_IMPORT_PROMPT,
            parse_mode="HTML",
            reply_markup=_import_entry_keyboard(pending.token),
        )

    except Forbidden:
        if not allow_activation:
            return

        activation = ImportActivation(
            token=token_hex(16),
            origin=origin,
            expires_at=monotonic() + IMPORT_ACTIVATION_TTL,
        )

        context.user_data[IMPORT_ACTIVATION_KEY] = activation

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Open private import",
                url=(
                    f"https://t.me/{context.bot.username}"
                    f"?start=imp_{activation.token}"
                ),
            )
        ]])

        try:
            await reply_personal_text(
                update,
                context,
                "Open Kokabiel privately to continue.",
                reply_markup=keyboard,
            )
        except TelegramError:
            context.user_data.pop(IMPORT_ACTIVATION_KEY, None)

        return

    except TelegramError:
        return

    _discard_pending_import(context)
    context.user_data[IMPORT_SESSION_KEY] = pending


async def handle_member_import_start(update, context) -> bool:
    """Consume only the import-specific /start payload."""

    args = getattr(context, "args", None) or []

    if not args or not args[0].startswith("imp_"):
        return False

    if len(args) != 1:
        return True

    user_id = private_import_identity(update)

    if user_id is None:
        return True

    activation = context.user_data.get(IMPORT_ACTIVATION_KEY)

    if not isinstance(activation, ImportActivation):
        return True

    if (
        activation.origin.user_id != user_id
        or args[0] != f"imp_{activation.token}"
    ):
        return True

    if monotonic() >= activation.expires_at:
        context.user_data.pop(IMPORT_ACTIVATION_KEY, None)
        return True

    context.user_data.pop(IMPORT_ACTIVATION_KEY, None)

    if not await verify_member_origin(
        context,
        activation.origin,
        user_id,
    ):
        return True

    await _open_member_dm(
        update,
        context,
        activation.origin,
        allow_activation=False,
    )

    return True


async def import_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None:
        return

    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        origin = await group_import_origin(update, context)

        if origin is not None:
            await _open_member_dm(
                update,
                context,
                origin,
                allow_activation=True,
            )

        return

    if chat.type != ChatType.PRIVATE:
        return

    if not await allowed(update, context):
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
        reply_markup=_import_entry_keyboard(pending.token),
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


def _member_session_expired(pending: PendingImport) -> bool:
    return (
        pending.origin is not None
        and (
            pending.expires_at is None
            or not monotonic() < pending.expires_at
        )
    )


def _discard_expired_member_session(context, pending) -> None:
    # Never interfere with an admitted operation or its files.
    if (
        _member_session_expired(pending)
        and pending.state in {
            "awaiting_upload",
            "ready",
            "verified_ready",
            "undo_history",
            "undo_ready",
        }
        and context.user_data.get(IMPORT_SESSION_KEY) is pending
    ):
        _discard_pending_import(context)


IMPORT_WRITER_BUSY = (
    "An import is being applied. "
    "Please try this action again shortly."
)


def _foreign_import_writer(context) -> bool:
    from handlers.import_tasks import IMPORT_TASK_MANAGER_KEY

    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", {})
    manager = bot_data.get(IMPORT_TASK_MANAGER_KEY)

    return bool(
        manager is not None
        and manager.writer_count
        and not manager.is_current_writer()
    )


async def _session_import_allowed(
    update,
    context,
    pending: PendingImport,
) -> Literal["allowed", "busy", "denied"]:
    """Distinguish authorization from transient writer contention."""

    if pending.origin is None:
        if _foreign_import_writer(context):
            return "busy"

        authorized = await allowed(update, context)

        if not authorized:
            return "denied"

        return (
            "busy"
            if _foreign_import_writer(context)
            else "allowed"
        )

    user_id = private_import_identity(update)

    if (
        user_id is None
        or pending.user_id != user_id
        or pending.chat_id != user_id
        or pending.origin.user_id != user_id
    ):
        return "denied"

    if _member_session_expired(pending):
        _discard_expired_member_session(context, pending)
        return "denied"

    if not await verify_member_origin(
        context,
        pending.origin,
        user_id,
    ):
        return "denied"

    if _member_session_expired(pending):
        _discard_expired_member_session(context, pending)
        return "denied"

    return (
        "busy"
        if _foreign_import_writer(context)
        else "allowed"
    )


async def _restore_busy_import_session(
    context,
    active: PendingImport,
    original: PendingImport,
) -> None:
    """Restore the original session without changing its token."""

    if context.user_data.get(IMPORT_SESSION_KEY) is not active:
        return

    context.user_data[IMPORT_SESSION_KEY] = original

    # Do not edit the original message: its existing keyboard
    # remains valid and can be retried after the writer finishes.

    try:
        await context.bot.send_message(
            chat_id=original.chat_id,
            text=IMPORT_WRITER_BUSY,
        )
    except TelegramError:
        pass


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

    access = await _session_import_allowed(
        update,
        context,
        pending,
    )

    if access == "busy":
        await update.effective_message.reply_text(
            IMPORT_WRITER_BUSY
        )
        return

    if access != "allowed":
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


async def _handle_undo_action(
    update,
    context,
    pending: PendingImport,
    action: str,
) -> bool:
    """Handle history and undo within the authorized import session."""

    history = (
        action == "history"
        or action.startswith("history_")
    )

    selection = (
        action.startswith("undo_")
        and action != "undo_apply"
    )

    applying = action == "undo_apply"
    returning = action == "upload"

    if not (history or selection or applying or returning):
        return False

    query = update.callback_query

    valid = (
        history
        and pending.state in {
            "awaiting_upload",
            "undo_history",
            "undo_ready",
        }
    ) or (
        returning
        and pending.state in {
            "undo_history",
            "undo_ready",
        }
    ) or (
        selection
        and pending.state == "undo_history"
    ) or (
        applying
        and pending.state == "undo_ready"
        and isinstance(
            pending.undo_preview,
            ImportUndoPreview,
        )
        and pending.undo_preview.user_id == pending.user_id
        and bool(pending.undo_preview.eligible)
    )

    if not valid:
        await query.answer(
            "That import action is no longer available.",
            show_alert=True,
        )
        return True

    from handlers.import_tasks import import_task_manager

    manager = import_task_manager(context.application)

    if applying and not manager.is_current_writer():
        await query.answer(
            "Undo requires a supervised writer.",
            show_alert=True,
        )
        return True

    number = None

    if selection or action.startswith("history_"):
        raw = action.split("_", 1)[1]

        if (
            not raw.isascii()
            or not raw.isdecimal()
            or len(raw) > 19
            or not 0 < int(raw) <= 9223372036854775807
        ):
            await query.answer(
                "Invalid import selection.",
                show_alert=True,
            )
            return True

        number = int(raw)

    active = replace(
        pending,
        state="applying" if applying else "processing",
    )

    context.user_data[IMPORT_SESSION_KEY] = active

    try:
        await query.answer(
            "Applying approved undo..."
            if applying
            else "Loading import history..."
        )

        # Membership and ownership are checked again after the
        # acknowledgement and before database access.

        authorized = await _session_import_allowed(
            update,
            context,
            active,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not active:
            return True

        if authorized == "busy":
            await _restore_busy_import_session(
                context, active, pending
            )
            return True

        if authorized != "allowed":
            _discard_pending_import(context)

            await _edit_import_message_safely(
                query,
                "Import authorization is unavailable. "
                "Request /import again.",
            )
            return True

        database_path = settings(context).database_path

        if applying:
            backup = _backup_path(
                database_path,
                active.user_id,
                active.token,
            ).with_name(
                f"preundo-{active.token}.db"
            )

            _prepare_backup_directory(backup.parent)

            result = await _run_import_worker(
                apply_import_undo,
                active.undo_preview,
                database_path=database_path,
                backup_path=backup,
            )

            if context.user_data.get(
                IMPORT_SESSION_KEY
            ) is active:
                _discard_pending_import(context)

                await _edit_import_message_safely(
                    query,
                    (
                        f"✅ Import #{result.batch_id} — Undo complete\n\n"
                        f"Titles restored: {result.restored_count}\n"
                        f"Titles retained: {result.skipped_count}\n\n"
                        "Other members' records and shared media "
                        "metadata were preserved."
                    ),
                )

            return True

        token = token_hex(16)

        if returning:
            next_session = replace(
                active,
                token=token,
                state="awaiting_upload",
                preview=None,
                undo_preview=None,
            )

            await query.edit_message_text(
                MEMBER_IMPORT_PROMPT,
                parse_mode="HTML",
                reply_markup=_import_entry_keyboard(token),
            )

            if context.user_data.get(
                IMPORT_SESSION_KEY
            ) is active:
                context.user_data[
                    IMPORT_SESSION_KEY
                ] = next_session

            return True

        if history:
            rows = await _run_import_worker(
                list_import_batches,
                database_path,
                user_id=active.user_id,
                before_id=number,
            )

            buttons = []

            for batch_id, tracker, created_at, count in rows[:8]:
                buttons.append([
                    InlineKeyboardButton(
                        (
                            f"#{batch_id} · {tracker[:20]} · "
                            f"{created_at[:10]} · {count} titles"
                        ),
                        callback_data=f"imp|undo_{batch_id}|{token}",
                    )
                ])

            if len(rows) > 8:
                buttons.append([
                    InlineKeyboardButton(
                        "Older imports",
                        callback_data=(
                            f"imp|history_{rows[7][0]}|{token}"
                        ),
                    )
                ])

            if number is not None:
                buttons.append([
                    InlineKeyboardButton(
                        "Newest imports",
                        callback_data=f"imp|history|{token}",
                    )
                ])

            text = (
                "📚 Your import history\n\n"
                "Select a completed import to preview its undo.\n"
                "Previously modified titles will be preserved.\n\n"
                "Imports made before journal support cannot "
                "be automatically reversed."
            )

            if not rows:
                text += "\n\nNo available import batches."

            next_session = replace(
                active,
                token=token,
                state="undo_history",
                preview=None,
                undo_preview=None,
            )

        else:
            plan = await _run_import_worker(
                preview_import_undo,
                database_path,
                user_id=active.user_id,
                batch_id=number,
            )

            if plan.user_id != active.user_id:
                raise RuntimeError(
                    "Undo preview identity mismatch."
                )

            text = (
                f"⚠️ Undo import #{plan.batch_id}\n\n"
                f"Source: {plan.source}\n"
                f"Imported: {plan.created_at}\n\n"
                f"Titles to restore: {plan.restore_count}\n"
                f"Titles to retain: {plan.skipped_count}\n\n"
                "Eligible titles will return to their pre-import "
                "personal state. Titles modified afterward will "
                "remain unchanged.\n\n"
                "Confirmation is required."
            )

            buttons = []

            if plan.eligible:
                buttons.append([
                    InlineKeyboardButton(
                        "Confirm Undo",
                        callback_data=f"imp|undo_apply|{token}",
                    )
                ])
            else:
                text += "\n\nNothing can be safely undone."

            buttons.append([
                InlineKeyboardButton(
                    "Back to Import History",
                    callback_data=f"imp|history|{token}",
                )
            ])

            next_session = replace(
                active,
                token=token,
                state="undo_ready",
                preview=None,
                undo_preview=plan,
            )

        buttons.append([
            InlineKeyboardButton(
                "Back to Upload",
                callback_data=f"imp|upload|{token}",
            )
        ])

        buttons.append([
            InlineKeyboardButton(
                "Cancel",
                callback_data=f"imp|cancel|{token}",
            )
        ])

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is not active:
            return True

        if _member_session_expired(active):
            _discard_pending_import(context)

            await _edit_import_message_safely(
                query,
                "Import session expired. Request /import again.",
            )
            return True

        if _foreign_import_writer(context):
            await _restore_busy_import_session(
                context, active, pending
            )
            return True

        if active.origin is not None:
            authorized = await _session_import_allowed(
                update,
                context,
                active,
            )

            if context.user_data.get(
                IMPORT_SESSION_KEY
            ) is not active:
                return True

            if authorized == "busy":
                await _restore_busy_import_session(
                    context, active, pending
                )
                return True

            if authorized != "allowed":
                _discard_pending_import(context)

                await _edit_import_message_safely(
                    query,
                    "Import authorization is unavailable. "
                    "Request /import again.",
                )
                return True

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is active:
            context.user_data[
                IMPORT_SESSION_KEY
            ] = next_session

    except asyncio.CancelledError:
        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is active:
            _discard_pending_import(context)

        raise

    except Exception as exc:
        LOGGER.warning(
            "Import management failed for user %s (%s)",
            pending.user_id,
            type(exc).__name__,
        )

        if context.user_data.get(
            IMPORT_SESSION_KEY
        ) is active:
            _discard_pending_import(context)

            await _edit_import_message_safely(
                query,
                (
                    "Kokabiel could not verify that the undo "
                    "completed normally. Request /import again "
                    "to inspect the current history."
                    if applying
                    else "Import history could not be loaded. "
                    "Request /import again."
                ),
            )

    return True


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

    if action == "cancel":
        from handlers.import_tasks import IMPORT_TASK_MANAGER_KEY

        application = getattr(context, "application", None)
        manager = getattr(
            application, "bot_data", {}
        ).get(IMPORT_TASK_MANAGER_KEY)

        if (
            private_import_identity(update) != pending.user_id
            or (
                pending.origin is not None
                and pending.origin.user_id != pending.user_id
            )
        ):
            await query.answer(
                "That import session is unavailable.",
                show_alert=True,
            )
            return

        if pending.state in {
            "processing",
            "preparing_verified",
            "applying",
        } or (
            manager is not None
            and manager.busy(pending.user_id)
        ):
            await query.answer(
                "An import operation is already in progress.",
                show_alert=True,
            )
            return

        _discard_pending_import(context)

        await query.answer("Import cancelled.")

        await query.edit_message_text(
            "Import cancelled. No changes were requested."
        )
        return

    access = await _session_import_allowed(
        update,
        context,
        pending,
    )

    if access == "busy":
        await query.answer(
            IMPORT_WRITER_BUSY,
            show_alert=True,
        )
        return

    if access != "allowed":
        if pending.origin is not None:
            await query.answer(
                "Import authorization is unavailable. "
                "Request /import again in your authorized group.",
                show_alert=True,
            )
        return

    if context.user_data.get(IMPORT_SESSION_KEY) is not pending:
        await query.answer(
            "That import session has changed.",
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

    if await _handle_undo_action(
        update,
        context,
        pending,
        action,
    ):
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
        if applying.origin is not None:
            from handlers.import_tasks import import_task_manager

            manager = import_task_manager(context.application)

            authorized = (
                manager.is_current_writer()
                and (
                    await _session_import_allowed(
                        update,
                        context,
                        applying,
                    )
                ) == "allowed"
            )

            if context.user_data.get(IMPORT_SESSION_KEY) is not applying:
                return

            if not authorized:
                _discard_pending_import(context)

                await _edit_import_message_safely(
                    query,
                    "Import authorization could not be verified. "
                    "No import changes were made. "
                    "Request /import again in your authorized group.",
                )
                return

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
            **(
                {"origin_group_id": pending.origin.group_id}
                if pending.origin is not None
                else {}
            ),
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
