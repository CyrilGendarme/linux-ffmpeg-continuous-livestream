from django.urls import path

from . import config_views, restream_views, views

urlpatterns = [
    path("", config_views.config_page, name="home"),
    path("config/", config_views.config_page, name="config_page"),
    path("api/config/schema/", config_views.config_schema, name="config_schema"),
    path("api/config/", config_views.config_api, name="config_api"),
    path("show/", config_views.show_page, name="show_page"),
    path("api/show/", config_views.show_api, name="show_api"),
    path("api/restream/", restream_views.restream_api, name="restream_api"),
    path("api/restream/connect/", restream_views.restream_connect, name="restream_connect"),
    path("restream/callback/", restream_views.restream_callback, name="restream_callback"),
    path("api/target-time/", views.target_time, name="target_time"),
    path("api/push-json/", views.push_json, name="push_json"),
]
