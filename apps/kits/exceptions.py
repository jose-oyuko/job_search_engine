"""Domain errors for kits. Services raise these; views and tasks catch them (spec §21.2)."""


class KitError(Exception):
    """Base class. `message` is safe to show to the user; it never contains profile content."""

    def __init__(
        self, message: str = "Something went wrong while preparing your documents."
    ) -> None:
        super().__init__(message)
        self.message = message


class NotOwner(KitError):
    """The user does not own the kit or job they asked about."""


class KitInProgress(KitError):
    """A kit for this job is already being prepared."""


class KitTooOld(KitError):
    """Editing is only allowed on a kit that is ready."""


class RenderError(KitError):
    """A document could not be rendered."""


class EmptyProfile(KitError):
    """There is nothing in the profile to build a CV from."""
