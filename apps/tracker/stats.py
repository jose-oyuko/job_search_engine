"""Personal application statistics (spec §14). Plain Python over the user's own rows."""

import datetime
import statistics
from dataclasses import dataclass, field

from apps.accounts.models import User

from .models import RESPONSE_STAGES, Application, ApplicationEvent, Stage

NO_SOURCE = "Added by you"
NO_TRACK = "No track"


@dataclass
class Row:
    label: str
    applied: int = 0
    responded: int = 0

    @property
    def rate(self) -> int:
        """Percent of applications that got a response."""
        return round(100 * self.responded / self.applied) if self.applied else 0


@dataclass
class Stats:
    counts: dict[str, int]
    applied: int
    responded: int
    median_days_to_response: float | None
    by_source: list[Row] = field(default_factory=list)
    by_track: list[Row] = field(default_factory=list)
    by_kit: list[Row] = field(default_factory=list)

    @property
    def response_rate(self) -> int:
        return round(100 * self.responded / self.applied) if self.applied else 0


def _rows(groups: dict[str, Row]) -> list[Row]:
    return sorted(groups.values(), key=lambda r: (-r.applied, r.label.lower()))


def compute(user: User) -> Stats:
    """Counts by stage and response rates.

    "Applied" means every application that is not just `saved`. "Responded" means the employer
    answered at some point: the application ever reached screening, interview, offer, hired or
    rejected (ghosted and withdrawn are not responses). Median days is from `applied_at` to the
    first such stage.
    """
    applications = list(
        Application.objects.for_user(user).select_related("job", "job__source", "track", "kit")
    )
    counts = {s.value: 0 for s in Stage}
    for application in applications:
        counts[application.stage] += 1

    first_response: dict[int, datetime.datetime] = {}
    response_values = [s.value for s in RESPONSE_STAGES]
    for event in (
        ApplicationEvent.objects.for_user(user)
        .filter(to_stage__in=response_values)
        .order_by("at", "id")
    ):
        first_response.setdefault(event.application_id, event.at)

    sources: dict[str, Row] = {}
    tracks: dict[str, Row] = {}
    kits: dict[str, Row] = {"With a kit": Row("With a kit"), "Without a kit": Row("Without a kit")}
    days: list[float] = []
    applied = responded = 0
    for application in applications:
        if application.stage == Stage.SAVED:
            continue
        got_response = application.pk in first_response
        job = application.job
        source_label = (
            job.company_platform or (job.source.display_name if job.source else "") or NO_SOURCE
        )
        track_label = application.track.name if application.track else NO_TRACK
        kit_label = "With a kit" if application.kit_id else "Without a kit"
        for group, label in ((sources, source_label), (tracks, track_label), (kits, kit_label)):
            row = group.setdefault(label, Row(label))
            row.applied += 1
            row.responded += int(got_response)
        applied += 1
        if got_response:
            responded += 1
            if application.applied_at:
                delta = first_response[application.pk] - application.applied_at
                days.append(max(0.0, delta.total_seconds() / 86400))
    return Stats(
        counts=counts,
        applied=applied,
        responded=responded,
        median_days_to_response=round(statistics.median(days), 1) if days else None,
        by_source=_rows(sources),
        by_track=_rows(tracks),
        by_kit=[r for r in kits.values() if r.applied],
    )
