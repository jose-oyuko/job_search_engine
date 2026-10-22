from django.urls import path

from . import views

app_name = "pages"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("waitlist/", views.waitlist, name="waitlist"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
    path("app/", views.dashboard, name="dashboard"),
]
