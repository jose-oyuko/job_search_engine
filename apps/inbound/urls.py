from django.urls import path

from . import views

app_name = "inbound"

urlpatterns = [
    path("app/settings/job-alerts/", views.job_alerts, name="settings"),
    path(
        "app/settings/job-alerts/confirmation/<int:pk>/dismiss/",
        views.dismiss_confirmation,
        name="dismiss_confirmation",
    ),
]
