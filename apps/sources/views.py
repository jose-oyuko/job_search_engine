"""Staff status page (spec §17.2): source health, recent runs and LLM spend."""

import datetime

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.jobs.models import Job
from apps.llm.services import spend_summary

from .models import Source, SourceRun


@login_required
@require_GET
def status(request: HttpRequest) -> HttpResponse:
    if not request.user.is_staff:
        raise PermissionDenied
    day_ago = timezone.now() - datetime.timedelta(hours=24)
    sources = Source.objects.annotate(
        jobs_total=Count("jobs", filter=Q(jobs__status=Job.Status.ACTIVE), distinct=True),
        jobs_24h=Count("jobs", filter=Q(jobs__first_seen_at__gte=day_ago), distinct=True),
    )
    context = {
        "sources": sources,
        "runs": SourceRun.objects.select_related("source")[:25],
        "spend": spend_summary(),
        "pending_extraction": Job.objects.active()
        .canonical()
        .filter(extracted_at__isnull=True)
        .count(),
    }
    return render(request, "sources/status.html", context)
