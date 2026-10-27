"""The only code that talks to Telegram (spec §13.1). Everything else uses `TelegramGateway`, so
tests run against `FakeGateway` and never touch the network."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.conf import settings

from .exceptions import GatewayError


@dataclass(frozen=True)
class Button:
    """An inline button: a link (`url`) or a callback (`data`, at most 64 bytes)."""

    text: str
    url: str | None = None
    data: str | None = None


Buttons = list[list[Button]]


class TelegramGateway(Protocol):
    def send_message(self, chat_id: str, text: str, *, buttons: Buttons | None = None) -> None: ...

    def send_document(
        self, chat_id: str, filename: str, data: bytes, caption: str = ""
    ) -> None: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...


@dataclass
class FakeGateway:
    """Records what would have been sent. `fail_with` makes the next call raise once."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    answers: list[dict[str, Any]] = field(default_factory=list)
    fail_with: GatewayError | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error

    def send_message(self, chat_id: str, text: str, *, buttons: Buttons | None = None) -> None:
        self._maybe_fail()
        self.messages.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str = "") -> None:
        self._maybe_fail()
        self.documents.append(
            {"chat_id": chat_id, "filename": filename, "data": data, "caption": caption}
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append({"callback_id": callback_id, "text": text})

    def reset(self) -> None:
        self.messages.clear()
        self.documents.clear()
        self.answers.clear()
        self.fail_with = None


CONNECT_TIMEOUT = 15.0  # seconds
READ_TIMEOUT = 30.0


class PtbGateway:
    """python-telegram-bot over the Bot API. Each call runs its own short event loop, so it is
    safe from Huey threads and from the bot's worker threads."""

    def __init__(self, token: str) -> None:
        self._token = token

    def _run(self, make_call: Any, *, attempts: int = 1) -> None:
        """One Bot API call. Generous timeouts (the default 5 seconds is too short on a slow link)
        and no `getMe` round trip first. `attempts` above 1 retries a timeout or network error,
        which is only safe for calls that can be repeated (answering a button press)."""
        from telegram.error import Forbidden, NetworkError, TelegramError, TimedOut

        async def go() -> None:
            from telegram import Bot
            from telegram.request import HTTPXRequest

            request = HTTPXRequest(
                connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
                write_timeout=READ_TIMEOUT, pool_timeout=CONNECT_TIMEOUT,
            )  # fmt: skip
            try:
                await make_call(Bot(self._token, request=request))
            finally:
                await request.shutdown()

        for attempt in range(1, attempts + 1):
            try:
                asyncio.run(go())
                return
            except Forbidden as exc:
                raise GatewayError("forbidden", blocked=True) from exc
            except (TimedOut, NetworkError) as exc:
                if attempt == attempts:
                    raise GatewayError(type(exc).__name__) from exc
            except TelegramError as exc:
                raise GatewayError(type(exc).__name__) from exc

    def send_message(self, chat_id: str, text: str, *, buttons: Buttons | None = None) -> None:
        markup = _markup(buttons)
        self._run(
            lambda bot: bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup, disable_web_page_preview=True
            )
        )

    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str = "") -> None:
        self._run(
            lambda bot: bot.send_document(
                chat_id=chat_id, document=data, filename=filename, caption=caption or None
            )
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._run(lambda bot: bot.answer_callback_query(callback_id, text=text or None), attempts=2)


def _markup(buttons: Buttons | None) -> Any:
    if not buttons:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(b.text, url=b.url)
                if b.url
                else InlineKeyboardButton(b.text, callback_data=b.data)
                for b in row
            ]
            for row in buttons
        ]
    )


_fake = FakeGateway()


def telegram_enabled() -> bool:
    return settings.TELEGRAM_GATEWAY == "fake" or bool(settings.TELEGRAM_BOT_TOKEN)


def get_gateway() -> TelegramGateway | None:
    """The configured gateway, or None when Telegram is not set up on this server."""
    if settings.TELEGRAM_GATEWAY == "fake":
        return _fake
    if settings.TELEGRAM_BOT_TOKEN:
        return PtbGateway(settings.TELEGRAM_BOT_TOKEN)
    return None
