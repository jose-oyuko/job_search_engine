"""Domain exceptions for matching."""

import datetime


class MatchingError(Exception):
    message = "Something went wrong with your matches."

    def __str__(self) -> str:
        return self.message


class NotOwner(MatchingError):
    """The match belongs to someone else. Boundaries turn this into a 404."""

    message = "Not found."


class SearchTooSoon(MatchingError):
    """ "Search now" was used less than 10 minutes ago."""

    def __init__(self, wait: datetime.timedelta) -> None:
        super().__init__()
        self.wait = wait
        minutes = max(1, int(wait.total_seconds() // 60) + 1)
        self.message = (
            f"You searched a moment ago. You can search again in about {minutes} minute(s)."
        )
