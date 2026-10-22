from django.conf import settings
from django.http import HttpRequest


def site(request: HttpRequest) -> dict[str, object]:
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_URL": settings.SITE_URL,
        "COMPANY_NAME": settings.COMPANY_NAME,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
        "GOOGLE_ENABLED": settings.GOOGLE_ENABLED,
        "TERMS_VERSION": settings.TERMS_VERSION,
        "PRIVACY_VERSION": settings.PRIVACY_VERSION,
    }
