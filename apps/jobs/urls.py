from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    path("app/jobs/add/", views.add, name="add"),
    path("app/jobs/add/url/", views.add_url, name="add_url"),
    path("app/jobs/add/text/", views.add_text, name="add_text"),
    path("app/jobs/<int:pk>/", views.detail, name="detail"),
    path("app/jobs/<int:pk>/report/", views.report, name="report"),
]
