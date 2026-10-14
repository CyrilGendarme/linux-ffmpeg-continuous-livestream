from django.urls import path

from . import views

urlpatterns = [
    path("api/target-time/", views.target_time, name="target_time"),
    path("api/push-json/", views.push_json, name="push_json"),
]
