from django.urls import path

from . import views

app_name = "sources"

urlpatterns = [
    path("app/staff/status/", views.status, name="status"),
]
