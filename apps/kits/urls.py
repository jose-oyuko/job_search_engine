from django.urls import path

from . import views

app_name = "kits"

urlpatterns = [
    path("app/jobs/<int:job_id>/kit/", views.panel, name="panel"),
    path("app/jobs/<int:job_id>/kit/generate/", views.generate, name="generate"),
    path("app/kits/<int:pk>/edit/", views.edit, name="edit"),
    path("app/kits/<int:pk>/rerender/", views.rerender, name="rerender"),
    path("app/kits/<int:pk>/download/<str:key>/", views.download, name="download"),
]
