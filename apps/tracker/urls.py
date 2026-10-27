from django.urls import path

from . import views

app_name = "tracker"

urlpatterns = [
    path("app/tracker/", views.board, name="board"),
    path("app/tracker/stats/", views.stats, name="stats"),
    path("app/tracker/add/", views.add, name="add"),
    path("app/tracker/apply/job/<int:job_id>/", views.apply_job, name="apply_job"),
    path("app/tracker/<int:pk>/", views.detail, name="detail"),
    path("app/tracker/<int:pk>/stage/", views.stage, name="stage"),
    path("app/tracker/<int:pk>/details/", views.details, name="details"),
    path("app/tracker/<int:pk>/kit/", views.choose_kit, name="kit"),
    path("app/tracker/<int:pk>/interviews/add/", views.interview_add, name="interview_add"),
    path("app/tracker/interviews/<int:pk>/edit/", views.interview_edit, name="interview_edit"),
    path(
        "app/tracker/interviews/<int:pk>/delete/", views.interview_delete, name="interview_delete"
    ),
]
