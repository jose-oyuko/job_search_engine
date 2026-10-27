"""Domain errors for the tracker. `message` is safe to show to the user."""


class TrackerError(Exception):
    def __init__(self, message: str = "Something went wrong.") -> None:
        super().__init__(message)
        self.message = message


class NotOwner(TrackerError):
    """The object belongs to someone else (or does not exist). Boundaries turn this into a 404."""


class InvalidStage(TrackerError):
    """That is not a stage."""


class InvalidKit(TrackerError):
    """The kit is not the user's, or is for a different job."""
