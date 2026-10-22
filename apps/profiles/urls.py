from django.urls import path

from . import onboarding, views

app_name = "profiles"

# Literal routes come before the generic `<section>` ones so "tracks" and "import" are not
# mistaken for section names.
urlpatterns = [
    path("app/profile/", views.profile_page, name="profile"),
    path("app/profile/basics/", views.basics, name="basics"),
    path("app/profile/tracks/", views.tracks, name="tracks"),
    path("app/profile/tracks/new/", views.track_edit, name="track_new"),
    path("app/profile/tracks/<int:pk>/", views.track_edit, name="track_edit"),
    path("app/profile/tracks/<int:pk>/delete/", views.track_delete, name="track_delete"),
    path("app/profile/tracks/<int:pk>/default/", views.track_default, name="track_default"),
    path("app/profile/import/", views.import_upload, name="import_upload"),
    path("app/profile/import/<int:pk>/", views.import_review, name="import_review"),
    path("app/profile/import/<int:pk>/status/", views.import_status, name="import_status"),
    path("app/profile/import/<int:pk>/apply/", views.import_apply, name="import_apply"),
    path("app/profile/import/<int:pk>/file/", views.import_file, name="import_file"),
    path("app/profile/<slug:section>/new/", views.item_new, name="item_new"),
    path("app/profile/<slug:section>/", views.item_create, name="item_create"),
    path("app/profile/<slug:section>/<int:pk>/", views.item_show, name="item_show"),
    path("app/profile/<slug:section>/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("app/profile/<slug:section>/<int:pk>/save/", views.item_save, name="item_save"),
    path("app/profile/<slug:section>/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("app/profile/<slug:section>/<int:pk>/move/", views.item_move, name="item_move"),
    # Onboarding wizard
    path("welcome/", onboarding.start, name="onboarding_start"),
    path("welcome/<slug:step>/", onboarding.step, name="onboarding_step"),
]
