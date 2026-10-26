"""Application kits: a tailored CV, cover letter, interview prep and screening answers (§5.7)."""

import uuid
from pathlib import PurePath

from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedModel
from apps.core.storage import private_storage
from apps.jobs.models import Job
from apps.matching.models import Match
from apps.profiles.models import Track


def kit_file_path(instance: "Kit", filename: str) -> str:
    """A random name under `kits/`, keeping only the extension. Names the user sees come from
    the download view, never from the file system."""
    return f"kits/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


class Kit(UserOwnedModel, TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="kits")
    match = models.ForeignKey(
        Match, null=True, blank=True, on_delete=models.SET_NULL, related_name="kits"
    )
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Per user and job: 1, 2, 3... A regeneration is a new version; old ones stay downloadable.
    version = models.PositiveIntegerField(default=1)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    # What the progress line shows while generating ("Writing cover letter...").
    progress = models.CharField(max_length=80, blank=True)
    # A short, user-safe message; never profile or job content.
    error = models.CharField(max_length=300, blank=True)
    instruction = models.CharField(max_length=300, blank=True)
    auto_generated = models.BooleanField(default=False)

    cv_data = models.JSONField(default=dict, blank=True)
    cover_letter_text = models.TextField(blank=True)
    interview_qa = models.JSONField(default=list, blank=True)
    screening_answers = models.JSONField(default=list, blank=True)
    validation_report = models.JSONField(default=dict, blank=True)

    cv_pdf = models.FileField(upload_to=kit_file_path, storage=private_storage, blank=True)
    cv_docx = models.FileField(upload_to=kit_file_path, storage=private_storage, blank=True)
    cover_letter_pdf = models.FileField(
        upload_to=kit_file_path, storage=private_storage, blank=True
    )
    cover_letter_docx = models.FileField(
        upload_to=kit_file_path, storage=private_storage, blank=True
    )

    template = models.CharField(max_length=30, default="classic")
    profile_version = models.PositiveIntegerField(default=1)
    user_edited = models.BooleanField(default=False)

    class Meta:
        ordering = ["-version", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "job", "version"], name="kit_unique_version"),
        ]
        indexes = [models.Index(fields=["user", "job", "status"])]

    @property
    def in_progress(self) -> bool:
        return self.status in (self.Status.QUEUED, self.Status.GENERATING)

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.READY

    def is_stale(self, current_profile_version: int) -> bool:
        """Made from an older version of the profile."""
        return self.is_ready and self.profile_version != current_profile_version

    def __str__(self) -> str:
        return f"Kit #{self.pk} v{self.version}"
