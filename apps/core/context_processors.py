from django.conf import settings
from django.http import HttpRequest


def site(request: HttpRequest) -> dict[str, str]:
    return {"SITE_NAME": settings.SITE_NAME, "SITE_URL": settings.SITE_URL}
