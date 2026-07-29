from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer(
                "Something went wrong while processing that button.", show_alert=True
            )
        except Exception:
            LOGGER.debug("Unable to answer failed callback", exc_info=True)
