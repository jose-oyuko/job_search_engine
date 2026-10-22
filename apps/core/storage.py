"""Private file storage (spec §16). Files live under PRIVATE_MEDIA_ROOT, never under MEDIA_URL,
and are served only by views that check ownership."""

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def private_storage() -> FileSystemStorage:
    # No base_url: there is deliberately no public URL for these files.
    return FileSystemStorage(location=str(settings.PRIVATE_MEDIA_ROOT), base_url=None)
