from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("join/<str:code>/", views.join, name="join"),
    path("app/settings/account/", views.account_settings, name="settings_account"),
]
