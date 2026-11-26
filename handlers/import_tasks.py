from __future__ import annotations

import asyncio
import logging

from collections.abc import Awaitable, Callable

from telegram.constants import ChatType

from handlers.common import (
    reply_personal_text,
    settings,
)
from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
)


from release_refresh import RELEASE_REFRESH_ACTIVE_KEY


IMPORT_TASK_MANAGER_KEY = "kokabiel_import_task_manager"

MAX_ACTIVE_IMPORTS = 2
MAX_ACTIVE_WRITERS = 1
MAX_DEFERRED_ACTIVITY = 32

LOGGER = logging.getLogger(__name__)


class ImportTaskManager:
    """Bounded, event-loop-owned import task reservations."""

    def __init__(self):
        self._accepting = True

        self._deferred_activity: list[
            Callable[[], Awaitable[None]]
        ] = []

        self._flushing_activity = False
        self._writer_started = False

        self._active: dict[
            int,
            tuple[asyncio.Task, bool],
        ] = {}

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def writer_count(self) -> int:
        return sum(
            writer
            for _, writer in self._active.values()
        )

    @property
    def deferred_activity_count(self) -> int:
        return len(self._deferred_activity)

    def is_current_writer(self) -> bool:
        current = asyncio.current_task()

        return any(
            writer and task is current
            for task, writer in self._active.values()
        )

    def defer_activity(
        self,
        operation: Callable[[], Awaitable[None]],
    ) -> bool:
        if (
            not self.accepting
            or not self.writer_count
            or not self._writer_started
            or self._flushing_activity
            or self.deferred_activity_count
            >= MAX_DEFERRED_ACTIVITY
        ):
            return False

        self._deferred_activity.append(operation)

        return True

    async def _flush_activity(self) -> None:
        # Stop further admission before flushing the
        # bounded queue. Keep the writer reservation
        # until all accepted records have been attempted.
        self._flushing_activity = True

        pending = tuple(self._deferred_activity)
        self._deferred_activity.clear()

        for operation in pending:
            try:
                await operation()
            except Exception:
                LOGGER.exception(
                    "Deferred activity recording failed."
                )

    async def _flush_activity_safely(self) -> None:
        # Close activity admission before scheduling
        # the supervised flush operation.
        self._flushing_activity = True

        if not self._deferred_activity:
            return

        flush = asyncio.create_task(
            self._flush_activity(),
            name="kokabiel-import-activity-flush",
        )

        try:
            await asyncio.shield(flush)

        except asyncio.CancelledError:
            # The writer still owns its reservation.
            # Repeated cancellation requests must not
            # interrupt accepted activity operations.
            while not flush.done():
                try:
                    await asyncio.shield(flush)
                except asyncio.CancelledError:
                    continue

            # Retrieve the completed operation's result
            # before propagating writer cancellation.
            flush.result()
            raise

    def busy(self, user_id: int) -> bool:
        return user_id in self._active

    def close(self) -> None:
        self._accepting = False

    def admission_error(
        self,
        application,
        user_id: int,
        *,
        writer: bool = False,
    ) -> str | None:

        if not self.accepting or not application.running:
            return (
                "Kokabiel is shutting down. "
                "Try your import again after restart."
            )

        if self.busy(user_id):
            return (
                "An import operation is already "
                "in progress for your account."
            )

        if self.active_count >= MAX_ACTIVE_IMPORTS:
            return (
                "Kokabiel is processing other imports. "
                "Please try again shortly."
            )

        if (
            writer
            and application.bot_data.get(
                RELEASE_REFRESH_ACTIVE_KEY
            )
        ):
            return (
                "Release information is being refreshed. "
                "Please apply your import after it finishes."
            )

        if writer and self.writer_count >= MAX_ACTIVE_WRITERS:
            return (
                "Another import is being applied. "
                "Please try again shortly."
            )

        return None

    def submit(
        self,
        update,
        context,
        handler,
        pending: PendingImport,
        *,
        writer: bool = False,
    ) -> str | None:

        application = context.application

        error = self.admission_error(
            application,
            pending.user_id,
            writer=writer,
        )

        if error is not None:
            return error

        async def run():
            try:
                # Never execute an operation against a session
                # that changed before the scheduled task began.
                if context.user_data.get(
                    IMPORT_SESSION_KEY
                ) is not pending:
                    return

                if writer:
                    self._writer_started = True

                await handler(update, context)

            finally:
                if writer:
                    self._writer_started = False
                    await self._flush_activity_safely()

        if writer:
            self._flushing_activity = False
            self._writer_started = False

        coroutine = run()

        try:
            task = application.create_task(
                coroutine,
                update=update,
                name=f"kokabiel-import-{pending.user_id}",
            )

        except BaseException:
            coroutine.close()
            raise

        # No await occurs between admission and reservation.
        self._active[pending.user_id] = (
            task,
            writer,
        )

        def release(completed):
            current = self._active.get(
                pending.user_id
            )

            if (
                current is not None
                and current[0] is completed
            ):
                self._active.pop(
                    pending.user_id,
                    None,
                )

                if writer:
                    self._flushing_activity = False
                    self._writer_started = False

        task.add_done_callback(release)

        return None

    async def drain(self) -> None:
        """Close admission and wait for every admitted task."""

        self.close()

        while self._active:
            tasks = tuple(
                task
                for task, _ in self._active.values()
            )

            # Cancelling a drain waiter must never cancel
            # the operations it is responsible for draining.
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

            # Completed callbacks may not yet have run.
            for user_id, (task, _) in tuple(
                self._active.items()
            ):
                if task.done():
                    self._active.pop(user_id, None)


def import_task_manager(application) -> ImportTaskManager:
    return application.bot_data[
        IMPORT_TASK_MANAGER_KEY
    ]


def _private_owner(update, context) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    return (
        chat is not None
        and user is not None
        and chat.type == ChatType.PRIVATE
        and user.id in settings(context).owner_user_ids
    )


def _owned_session(update, context) -> PendingImport | None:
    pending = context.user_data.get(
        IMPORT_SESSION_KEY
    )

    if (
        isinstance(pending, PendingImport)
        and pending.user_id == update.effective_user.id
        and pending.chat_id == update.effective_chat.id
    ):
        return pending

    return None


async def dispatch_import_command(
    update,
    context,
    handler,
) -> None:

    if _private_owner(update, context):
        manager = import_task_manager(
            context.application
        )

        if manager.busy(update.effective_user.id):
            await reply_personal_text(
                update,
                context,
                "An import operation is already in progress.",
            )
            return

        if (
            not manager.accepting
            or not context.application.running
        ):
            await reply_personal_text(
                update,
                context,
                "Kokabiel is shutting down. Try again after restart.",
            )
            return

    # Preserve existing group and authorization behavior.
    await handler(update, context)


async def dispatch_import_document(
    update,
    context,
    handler,
) -> None:

    if not _private_owner(update, context):
        await handler(update, context)
        return

    pending = _owned_session(update, context)

    manager = import_task_manager(
        context.application
    )

    if manager.busy(update.effective_user.id):
        await reply_personal_text(
            update,
            context,
            "An import operation is already in progress.",
        )
        return

    if (
        pending is None
        or pending.state != "awaiting_upload"
    ):
        await handler(update, context)
        return

    error = manager.submit(
        update,
        context,
        handler,
        pending,
    )

    if error is not None:
        await reply_personal_text(
            update,
            context,
            error,
        )


async def dispatch_callback(
    update,
    context,
    handler,
) -> None:

    query = update.callback_query

    if (
        query is None
        or not (query.data or "").startswith("imp|")
        or not _private_owner(update, context)
    ):
        # All ordinary callbacks remain sequential.
        await handler(update, context)
        return

    manager = import_task_manager(
        context.application
    )

    if manager.busy(update.effective_user.id):
        await query.answer(
            "An import operation is already in progress.",
            show_alert=True,
        )
        return

    pending = _owned_session(update, context)
    parts = (query.data or "").split("|")

    if (
        pending is None
        or len(parts) != 3
        or parts[2] != pending.token
    ):
        await handler(update, context)
        return

    action = parts[1]

    preparation = (
        action == "prepare_verified"
        and pending.state == "ready"
    )

    writer = (
        (
            action == "apply"
            and pending.state == "ready"
        )
        or (
            action == "apply_verified"
            and pending.state == "verified_ready"
        )
    )

    if not preparation and not writer:
        # Cancellation, review and invalid actions retain
        # the existing synchronous dispatcher behavior.
        await handler(update, context)
        return

    error = manager.submit(
        update,
        context,
        handler,
        pending,
        writer=writer,
    )

    if error is not None:
        await query.answer(
            error,
            show_alert=True,
        )
