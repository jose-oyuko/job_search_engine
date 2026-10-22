"""Domain exceptions for accounts. Raised by services, caught at boundaries (views, adapters)."""


class InviteError(Exception):
    """An invite cannot be used. `reason` is a stable code; `message` is safe to show a user."""

    reason = "invalid"
    message = "This invite link is not valid."

    def __str__(self) -> str:
        return self.message


class InviteNotFound(InviteError):
    reason = "not_found"


class InviteAlreadyUsed(InviteError):
    reason = "used"
    message = "This invite has already been used."


class InviteExpired(InviteError):
    reason = "expired"
    message = "This invite has expired. Ask for a new one."


class InviteEmailMismatch(InviteError):
    reason = "email_mismatch"
    message = "This invite was issued for a different email address."
