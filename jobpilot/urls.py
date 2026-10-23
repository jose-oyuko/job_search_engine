from allauth.account import views as allauth_views
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django_ratelimit.decorators import ratelimit

# Login and signup: 10 attempts/minute/IP (§20), applied in front of allauth's own views.
# The names match allauth's so `{% url 'account_login' %}` resolves either way.
_login_limit = ratelimit(key="ip", rate="10/m", method="POST", block=True)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", _login_limit(allauth_views.login), name="account_login"),
    path("accounts/signup/", _login_limit(allauth_views.signup), name="account_signup"),
    path("accounts/", include("allauth.urls")),
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.llm.urls")),
    path("", include("apps.profiles.urls")),
    path("", include("apps.sources.urls")),
    path("", include("apps.jobs.urls")),
    path("", include("apps.matching.urls")),
    path("", include("apps.pages.urls")),
]

if settings.DEBUG and "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns = [path("__debug__/", include("debug_toolbar.urls")), *urlpatterns]

handler403 = "apps.pages.views.permission_denied"
handler404 = "apps.pages.views.not_found"
handler500 = "apps.pages.views.server_error"
