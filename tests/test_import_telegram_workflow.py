from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace

from handlers.imports import (
    _prepare_quarantine_directory,
    _quarantine_path,
    _safe_upload_suffix,
    format_import_preview,
    import_preview_keyboard,
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


if __name__ == "__main__":
    unittest.main()
