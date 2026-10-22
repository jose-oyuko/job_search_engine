"""URLconf for the 500-page test: the real routes plus one view that always crashes."""

from django.http import HttpRequest, HttpResponse
from django.urls import path

from jobpilot.urls import handler403, handler404, handler500, urlpatterns  # noqa: F401


def boom(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("boom")


urlpatterns = [path("boom/", boom), *urlpatterns]
