"""Settings → Usage: this month's LLM usage against the user's quota."""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.models import User

from .services import usage_summary


@require_GET
@login_required
def usage(request: HttpRequest) -> HttpResponse:
    user: User = request.user  # type: ignore[assignment]
    return render(request, "llm/usage.html", {"summary": usage_summary(user)})
