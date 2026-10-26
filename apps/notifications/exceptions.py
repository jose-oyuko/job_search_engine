"""Domain errors for notifications. `message` is always safe to show to the user."""


class NotificationError(Exception):
    def __init__(self, message: str = "Something went wrong.") -> None:
        super().__init__(message)
        self.message = message


class LinkTokenInvalid(NotificationError):
    """The Telegram link token is unknown, used or expired."""


class ChatAlreadyLinked(NotificationError):
    """That Telegram chat is already linked to a different account."""


class TelegramUnavailable(NotificationError):
    """Telegram is not configured on this server."""


class GatewayError(Exception):
    """Telegram refused or could not be reached. Never carries message content."""

    def __init__(self, reason: str, *, blocked: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        # The user blocked the bot or deleted the chat: sending will never work again.
        self.blocked = blocked
