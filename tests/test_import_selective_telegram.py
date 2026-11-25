from __future__ import annotations

import unittest
from dataclasses import replace

import handlers.imports as workflow

from imports.db_preview import ExistingPersonalState
from imports.selective import build_selective_import_preview
from tests.test_import_selective import (
    empty_snapshot,
    resolution,
    resolved,
    unresolved,
)


def selection(snapshot=None):
    return build_selective_import_preview(
        resolution(
            resolved(
                "movie", 100, "Verified Film",
                record_count=4,
            ),
            resolved(
                "tv", 200, "Excluded Series",
                status="dropped",
                record_count=2,
            ),
            resolved(
                "episode", 200, "Excluded Series",
                season=1,
                episode=1,
            ),
        ),
        snapshot or empty_snapshot(),
    )


def callbacks(keyboard):
    return tuple(
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    )


class SelectiveTelegramInterfaceTests(unittest.TestCase):
    def test_initial_preview_offers_prepare_not_apply(self):
        preview = selection()

        keyboard = workflow.import_preview_keyboard(
            preview.full,
            "original",
            selective=preview,
        )

        actions = callbacks(keyboard)

        self.assertIn(
            "imp|prepare_verified|original",
            actions,
        )
        self.assertIn("imp|review|original", actions)
        self.assertIn("imp|cancel|original", actions)

        self.assertNotIn("imp|apply|original", actions)
        self.assertNotIn("imp|apply_verified|original", actions)

    def test_confirmation_has_separate_apply_action(self):
        keyboard = workflow.selective_import_keyboard(
            "confirmation"
        )

        actions = callbacks(keyboard)

        self.assertIn(
            "imp|apply_verified|confirmation",
            actions,
        )
        self.assertIn("imp|cancel|confirmation", actions)

        self.assertNotIn(
            "imp|apply|confirmation",
            actions,
        )
        self.assertNotIn(
            "imp|prepare_verified|confirmation",
            actions,
        )

    def test_all_excluded_never_offers_prepare(self):
        preview = build_selective_import_preview(
            resolution(
                unresolved(
                    media_type="movie",
                    title="Missing Film",
                )
            ),
            empty_snapshot(),
        )

        actions = callbacks(
            workflow.import_preview_keyboard(
                preview.full,
                "original",
                selective=preview,
            )
        )

        self.assertNotIn(
            "imp|prepare_verified|original",
            actions,
        )
        self.assertNotIn("imp|apply|original", actions)
        self.assertIn("imp|cancel|original", actions)

    def test_unchanged_eligible_subset_never_offers_prepare(self):
        snapshot = replace(
            empty_snapshot(),
            states={
                ("movie", 100): ExistingPersonalState(
                    watchlist=False,
                    viewing_status="watched",
                    updated_at="2020-01-01",
                ),
            },
            media=frozenset({("movie", 100)}),
        )

        preview = selection(snapshot)

        self.assertFalse(
            any(i.has_changes for i in preview.eligible.items)
        )

        actions = callbacks(
            workflow.import_preview_keyboard(
                preview.full,
                "original",
                selective=preview,
            )
        )

        self.assertNotIn(
            "imp|prepare_verified|original",
            actions,
        )
        self.assertNotIn("imp|apply|original", actions)

    def test_confirmation_reports_exact_subset_counts(self):
        preview = selection()

        text = workflow.format_selective_import_preview(
            preview
        )

        self.assertIn("Eligible source records:", text)
        self.assertIn("Excluded source records:", text)
        self.assertIn("Dependency-excluded items:", text)
        self.assertIn("New catalog rows: 1", text)
        self.assertIn("Personal states added: 1", text)
        self.assertIn("Episodes added: 0", text)
        self.assertIn("No changes have been written", text)

    def test_clean_import_retains_original_approval(self):
        preview = build_selective_import_preview(
            resolution(
                resolved("movie", 100, "Verified Film")
            ),
            empty_snapshot(),
        )

        actions = callbacks(
            workflow.import_preview_keyboard(
                preview.full,
                "original",
                selective=preview,
            )
        )

        self.assertIn("imp|apply|original", actions)
        self.assertNotIn(
            "imp|prepare_verified|original",
            actions,
        )


if __name__ == "__main__":
    unittest.main()
