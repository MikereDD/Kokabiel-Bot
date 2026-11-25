from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType

from imports.selective import SelectiveImportPreview

from handlers.imports import (
    IMPORT_SESSION_KEY,
    PendingImport,
    _prepare_quarantine_directory,
    _quarantine_path,
    _safe_upload_suffix,
    format_import_preview,
    import_callback_router,
    import_command,
    import_preview_keyboard,
    receive_import_document,
)


def preview(
    *,
    review_count=0,
    has_changes=True,
):
    status = (
        "needs_review"
        if review_count
        else "resolved"
    )

    item = SimpleNamespace(
        resolution=SimpleNamespace(
            status=status
        ),
        has_changes=has_changes,
    )

    return SimpleNamespace(
        source="trakt",
        raw_record_count=12,
        items=(item,),
        media_add_count=1 if has_changes else 0,
        state_add_count=1 if has_changes else 0,
        state_update_count=0,
        episode_add_count=10 if has_changes else 0,
        rating_add_count=0,
        skip_count=0 if has_changes else 1,
        review_count=review_count,
    )


class ImportTelegramWorkflowTests(
    unittest.TestCase
):
    def test_supported_suffix_is_normalized(self):
        self.assertEqual(
            _safe_upload_suffix(
                "EXPORT.CSV"
            ),
            ".csv",
        )

        self.assertEqual(
            _safe_upload_suffix(
                "backup.ZIP"
            ),
            ".zip",
        )

    def test_unsupported_or_missing_suffix_is_rejected(self):
        for name in (
            None,
            "",
            "export",
            "export.exe",
            "export.csv.exe",
        ):
            with self.subTest(
                name=name
            ):
                with self.assertRaises(
                    ValueError
                ):
                    _safe_upload_suffix(
                        name
                    )

    def test_quarantine_path_never_uses_untrusted_basename(self):
        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "kokabiel.db"
            )

            path = _quarantine_path(
                database,
                123,
                "deadbeef",
                "../../evil.csv",
            )

            self.assertEqual(
                path.name,
                "deadbeef.csv",
            )
            self.assertEqual(
                path.parent.name,
                "123",
            )
            self.assertNotIn(
                "evil",
                str(path),
            )

    def test_quarantine_directory_is_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = (
                Path(temp)
                / "imports"
                / "quarantine"
                / "123"
            )

            _prepare_quarantine_directory(
                directory
            )

            self.assertEqual(
                directory.stat().st_mode
                & 0o777,
                0o700,
            )

            self.assertEqual(
                directory.parent.stat().st_mode
                & 0o777,
                0o700,
            )

    def test_preview_text_reports_action_counts(self):
        text = format_import_preview(
            preview()
        )

        self.assertIn(
            "Source records:</b> 12",
            text,
        )
        self.assertIn(
            "Episodes added: 10",
            text,
        )
        self.assertIn(
            "Items blocking import: 0",
            text,
        )

    def test_clean_preview_offers_import_and_cancel(self):
        keyboard = (
            import_preview_keyboard(
                preview(),
                "deadbeef",
            )
        )

        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertEqual(
            callbacks,
            [
                "imp|apply|deadbeef",
                "imp|cancel|deadbeef",
            ],
        )

    def test_review_preview_never_offers_apply(self):
        keyboard = (
            import_preview_keyboard(
                preview(
                    review_count=1
                ),
                "deadbeef",
            )
        )

        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertEqual(
            callbacks,
            [
                "imp|review|deadbeef",
                "imp|cancel|deadbeef",
            ],
        )


class ImportTelegramWorkflowAsyncTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_group_import_does_not_arm_document_upload(self):
        context = SimpleNamespace(
            user_data={}
        )

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=-100123,
                type=ChatType.SUPERGROUP,
            ),
            effective_user=SimpleNamespace(
                id=123,
                full_name="Mike",
                username=None,
            ),
        )

        reply = AsyncMock()

        with (
            patch(
                "handlers.imports.allowed",
                new=AsyncMock(
                    return_value=True
                ),
            ),
            patch(
                "handlers.imports.reply_personal_text",
                new=reply,
            ),
        ):
            await import_command(
                update,
                context,
            )

        self.assertNotIn(
            IMPORT_SESSION_KEY,
            context.user_data,
        )

        reply.assert_awaited_once()

        self.assertIn(
            "not accepted in group chats",
            reply.await_args.args[2],
        )

    async def test_private_import_arms_upload_session(self):
        context = SimpleNamespace(
            user_data={}
        )

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(
                id=123,
                type=ChatType.PRIVATE,
            ),
            effective_user=SimpleNamespace(
                id=123,
                full_name="Mike",
                username=None,
            ),
        )

        with (
            patch(
                "handlers.imports.allowed",
                new=AsyncMock(
                    return_value=True
                ),
            ),
            patch(
                "handlers.imports.reply_personal_text",
                new=AsyncMock(),
            ),
        ):
            await import_command(
                update,
                context,
            )

        pending = context.user_data.get(
            IMPORT_SESSION_KEY
        )

        self.assertIsInstance(
            pending,
            PendingImport,
        )
        self.assertEqual(
            pending.state,
            "awaiting_upload",
        )
        self.assertEqual(
            pending.user_id,
            123,
        )
        self.assertEqual(
            pending.chat_id,
            123,
        )


class ImportTelegramApprovalTests(
    unittest.IsolatedAsyncioTestCase
):
    def callback_update(
        self,
        data,
    ):
        query = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(
                id=123,
                type=ChatType.PRIVATE,
            ),
            effective_user=SimpleNamespace(
                id=123,
                full_name="Mike",
                username=None,
            ),
        )

        return update, query

    async def test_wrong_token_never_reaches_writer(self):
        pending = PendingImport(
            token="deadbeef",
            user_id=123,
            user_name="Mike",
            chat_id=123,
            state="ready",
            preview=preview(),
        )

        context = SimpleNamespace(
            user_data={
                IMPORT_SESSION_KEY: pending
            }
        )

        update, query = (
            self.callback_update(
                "imp|apply|badtoken"
            )
        )

        writer = MagicMock()

        with (
            patch(
                "handlers.imports.allowed",
                new=AsyncMock(
                    return_value=True
                ),
            ),
            patch(
                "handlers.imports.apply_database_import",
                new=writer,
            ),
        ):
            await import_callback_router(
                update,
                context,
            )

        writer.assert_not_called()

        self.assertIs(
            context.user_data[
                IMPORT_SESSION_KEY
            ],
            pending,
        )

        self.assertTrue(
            query.answer.await_args.kwargs[
                "show_alert"
            ]
        )

    async def test_review_action_never_reaches_writer(self):
        pending = PendingImport(
            token="deadbeef",
            user_id=123,
            user_name="Mike",
            chat_id=123,
            state="ready",
            preview=preview(
                review_count=1
            ),
        )

        context = SimpleNamespace(
            user_data={
                IMPORT_SESSION_KEY: pending
            }
        )

        update, query = (
            self.callback_update(
                "imp|review|deadbeef"
            )
        )

        writer = MagicMock()

        with (
            patch(
                "handlers.imports.allowed",
                new=AsyncMock(
                    return_value=True
                ),
            ),
            patch(
                "handlers.imports.apply_database_import",
                new=writer,
            ),
        ):
            await import_callback_router(
                update,
                context,
            )

        writer.assert_not_called()

        self.assertIn(
            IMPORT_SESSION_KEY,
            context.user_data,
        )

        self.assertTrue(
            query.answer.await_args.kwargs[
                "show_alert"
            ]
        )

    async def test_cancel_deletes_pending_upload_and_session(self):
        with tempfile.TemporaryDirectory() as temp:
            upload = (
                Path(temp)
                / "raw.csv"
            )

            upload.write_text(
                "sensitive"
            )

            pending = PendingImport(
                token="deadbeef",
                user_id=123,
                user_name="Mike",
                chat_id=123,
                state="ready",
                path=upload,
                preview=preview(),
            )

            context = SimpleNamespace(
                user_data={
                    IMPORT_SESSION_KEY: pending
                }
            )

            update, query = (
                self.callback_update(
                    "imp|cancel|deadbeef"
                )
            )

            with patch(
                "handlers.imports.allowed",
                new=AsyncMock(
                    return_value=True
                ),
            ):
                await import_callback_router(
                    update,
                    context,
                )

            self.assertFalse(
                upload.exists()
            )

            self.assertNotIn(
                IMPORT_SESSION_KEY,
                context.user_data,
            )

            query.edit_message_text.assert_awaited_once()

    async def test_apply_calls_transactional_writer_once(self):
        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "kokabiel.db"
            )

            pending = PendingImport(
                token="deadbeef",
                user_id=123,
                user_name="Mike",
                chat_id=123,
                state="ready",
                preview=preview(),
            )

            context = SimpleNamespace(
                user_data={
                    IMPORT_SESSION_KEY: pending
                }
            )

            update, query = (
                self.callback_update(
                    "imp|apply|deadbeef"
                )
            )

            result = SimpleNamespace(
                already_applied=False,
                media_added=1,
                states_added=1,
                states_updated=0,
                episodes_added=10,
                ratings_added=0,
            )

            writer = MagicMock(
                return_value=result
            )

            with (
                patch(
                    "handlers.imports.allowed",
                    new=AsyncMock(
                        return_value=True
                    ),
                ),
                patch(
                    "handlers.imports.settings",
                    return_value=SimpleNamespace(
                        database_path=database
                    ),
                ),
                patch(
                    "handlers.imports.apply_database_import",
                    new=writer,
                ),
            ):
                await import_callback_router(
                    update,
                    context,
                )

            writer.assert_called_once()

            call = writer.call_args

            self.assertIs(
                call.args[0],
                pending.preview,
            )

            self.assertEqual(
                call.kwargs[
                    "database_path"
                ],
                database,
            )

            self.assertEqual(
                call.kwargs[
                    "user_name"
                ],
                "Mike",
            )

            self.assertEqual(
                call.kwargs[
                    "chat_id"
                ],
                123,
            )

            backup = call.kwargs[
                "backup_path"
            ]

            self.assertEqual(
                backup.parent.stat().st_mode
                & 0o777,
                0o700,
            )

            self.assertNotIn(
                IMPORT_SESSION_KEY,
                context.user_data,
            )

            self.assertIn(
                "Import complete",
                query.edit_message_text
                .await_args.args[0],
            )

    async def test_stale_apply_discards_old_preview(self):
        from imports.db_apply import (
            ImportPreviewStaleError,
        )

        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "kokabiel.db"
            )

            pending = PendingImport(
                token="deadbeef",
                user_id=123,
                user_name="Mike",
                chat_id=123,
                state="ready",
                preview=preview(),
            )

            context = SimpleNamespace(
                user_data={
                    IMPORT_SESSION_KEY: pending
                }
            )

            update, query = (
                self.callback_update(
                    "imp|apply|deadbeef"
                )
            )

            with (
                patch(
                    "handlers.imports.allowed",
                    new=AsyncMock(
                        return_value=True
                    ),
                ),
                patch(
                    "handlers.imports.settings",
                    return_value=SimpleNamespace(
                        database_path=database
                    ),
                ),
                patch(
                    "handlers.imports.apply_database_import",
                    side_effect=(
                        ImportPreviewStaleError(
                            "stale"
                        )
                    ),
                ),
            ):
                await import_callback_router(
                    update,
                    context,
                )

            self.assertNotIn(
                IMPORT_SESSION_KEY,
                context.user_data,
            )

            self.assertIn(
                "changed after this preview",
                query.edit_message_text
                .await_args.args[0],
            )

    async def test_successful_preview_deletes_raw_upload_immediately(self):
        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "kokabiel.db"
            )

            pending = PendingImport(
                token="deadbeef",
                user_id=123,
                user_name="Mike",
                chat_id=123,
                state="awaiting_upload",
            )

            fake_file = SimpleNamespace(
                download_to_drive=AsyncMock()
            )

            async def download_to_drive(
                *,
                custom_path,
            ):
                Path(
                    custom_path
                ).write_text(
                    "temporary tracker data"
                )

            fake_file.download_to_drive.side_effect = (
                download_to_drive
            )

            context = SimpleNamespace(
                user_data={
                    IMPORT_SESSION_KEY: pending
                },
                bot=SimpleNamespace(
                    get_file=AsyncMock(
                        return_value=fake_file
                    )
                ),
            )

            update = SimpleNamespace(
                effective_chat=SimpleNamespace(
                    id=123,
                    type=ChatType.PRIVATE,
                ),
                effective_user=SimpleNamespace(
                    id=123,
                    full_name="Mike",
                    username=None,
                ),
                effective_message=SimpleNamespace(
                    document=SimpleNamespace(
                        file_name="ratings.csv",
                        file_size=100,
                        file_id="telegram-file",
                    )
                ),
            )

            expected_preview = preview()
            analyzer = MagicMock(
                return_value=object()
            )

            with (
                patch(
                    "handlers.imports.allowed",
                    new=AsyncMock(
                        return_value=True
                    ),
                ),
                patch(
                    "handlers.imports.settings",
                    return_value=SimpleNamespace(
                        database_path=database
                    ),
                ),
                patch(
                    "handlers.imports.tmdb",
                    return_value=object(),
                ),
                patch(
                    "handlers.imports.analyze_import",
                    new=analyzer,
                ),
                patch(
                    "handlers.imports.resolve_import_plan",
                    new=AsyncMock(
                        return_value=object()
                    ),
                ),
                patch(
                    "handlers.imports.preview_selective_database_import",
                    return_value=SelectiveImportPreview(
                        full=expected_preview,
                        eligible=expected_preview,
                        excluded=(),
                    ),
                ),
                patch(
                    "handlers.imports.reply_personal_text",
                    new=AsyncMock(),
                ),
            ):
                await receive_import_document(
                    update,
                    context,
                )

            analyzer.assert_called_once()

            self.assertEqual(
                analyzer.call_args.kwargs[
                    "source_name"
                ],
                "ratings.csv",
            )

            ready = context.user_data[
                IMPORT_SESSION_KEY
            ]

            self.assertEqual(
                ready.state,
                "ready",
            )
            self.assertIsNone(
                ready.path
            )
            self.assertIs(
                ready.preview,
                expected_preview,
            )

            quarantine = (
                _quarantine_path(
                    database,
                    123,
                    "deadbeef",
                    "ratings.csv",
                )
            )

            self.assertFalse(
                quarantine.exists()
            )


if __name__ == "__main__":
    unittest.main()
