from django.urls import path

from . import views

app_name = "matching"

urlpatterns = [
    path("app/matches/<int:pk>/save/", views.save, name="save"),
    path("app/matches/<int:pk>/dismiss/", views.dismiss, name="dismiss"),
    path("app/matches/<int:pk>/applied/", views.applied, name="applied"),
    path("app/matches/<int:pk>/restore/", views.restore, name="restore"),
    path("app/matches/<int:pk>/rescore/", views.rescore, name="rescore"),
    path("app/matches/<int:pk>/score-status/", views.score_status, name="score_status"),
    path("app/search/", views.search_now, name="search_now"),
    path("app/search/status/", views.search_status, name="search_status"),
]
