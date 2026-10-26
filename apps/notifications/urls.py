from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("app/settings/notifications/", views.settings_notifications, name="settings"),
    path("app/settings/notifications/telegram/connect/", views.connect_telegram, name="connect"),
    path(
        "app/settings/notifications/telegram/disconnect/",
        views.disconnect_telegram,
        name="disconnect",
    ),
]
