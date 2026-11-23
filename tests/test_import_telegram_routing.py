from __future__ import annotations

import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.callbacks import callback_router


class ImportTelegramRoutingTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_import_callback_routes_to_import_router(self):
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                data="imp|apply|deadbeef"
            )
        )

        context = SimpleNamespace()

        import_router = AsyncMock()

        with (
            patch(
                "handlers.callbacks.import_callback_router",
                new=import_router,
            ),
            patch(
                "handlers.callbacks.allowed",
                new=AsyncMock(
                    side_effect=AssertionError(
                        "generic callback path must not run"
                    )
                ),
            ),
        ):
            await callback_router(
                update,
                context,
            )

        import_router.assert_awaited_once_with(
            update,
            context,
        )


if __name__ == "__main__":
    unittest.main()
