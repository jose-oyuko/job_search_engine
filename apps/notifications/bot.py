"""The python-telegram-bot glue for `manage.py run_bot` (long polling). Handlers only pass the
update to `bot_services`; all behaviour lives there."""

import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections

from apps.core.logging import log_event

from . import bot_services

logger = logging.getLogger(__name__)


def _run_sync(function: Any, *args: Any) -> Any:
    close_old_connections()
    try:
        return function(*args)
    except Exception:
        log_event(logger, logging.ERROR, "bot_handler_failed", "Bot handler failed", exc_info=True)
    finally:
        close_old_connections()


def build_application(token: str) -> Any:
    from telegram import Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat, sender = update.message, update.effective_chat, update.effective_user
        if message is None or chat is None or not message.text:
            return
        await sync_to_async(_run_sync)(
            bot_services.handle_text,
            str(chat.id),
            (sender.username or "") if sender else "",
            message.text,
        )

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        chat_id = query.message.chat.id if query.message else query.from_user.id
        await sync_to_async(_run_sync)(
            bot_services.handle_callback, str(chat_id), query.id, query.data
        )

    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_message))
    application.add_handler(CallbackQueryHandler(on_callback))
    return application
