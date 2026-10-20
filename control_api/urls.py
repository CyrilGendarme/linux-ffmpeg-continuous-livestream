from django.urls import path

from . import config_views, views

urlpatterns = [
    path("", config_views.config_page, name="home"),
    path("config/", config_views.config_page, name="config_page"),
    path("api/config/schema/", config_views.config_schema, name="config_schema"),
    path("api/config/", config_views.config_api, name="config_api"),
    path("api/target-time/", views.target_time, name="target_time"),
    path("api/push-json/", views.push_json, name="push_json"),
]
