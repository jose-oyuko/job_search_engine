"""Run the Telegram bot with long polling (`python manage.py run_bot`)."""

import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.logging import log_event
from apps.notifications.bot import build_application

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the Telegram bot (long polling). Needs TELEGRAM_BOT_TOKEN."

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not set, so there is no bot to run.")
        log_event(logger, logging.INFO, "bot_started", "Telegram bot started")
        application = build_application(settings.TELEGRAM_BOT_TOKEN)
        application.run_polling(allowed_updates=["message", "callback_query"])
