"""Domain exceptions for profiles. `message` is safe to show to the user."""


class ProfileError(Exception):
    message = "Something went wrong with your profile. Please try again."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message

    def __str__(self) -> str:
        return self.message


class InvalidCVFile(ProfileError):
    """The upload is not an acceptable PDF or DOCX (type, size or content)."""

    message = "Please upload a PDF or DOCX file."


class UnreadableCV(ProfileError):
    """The file is valid but has no text we can use (for example a scanned PDF)."""

    message = "We could not read any text from this file."


class NotOwner(ProfileError):
    """The object belongs to someone else. Boundaries turn this into a 404."""

    message = "Not found."


class ImportStateError(ProfileError):
    message = "This import cannot be applied right now."
