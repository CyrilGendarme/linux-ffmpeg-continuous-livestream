import json

from django.test import TestCase, override_settings

STRONG_SECRET = "a-strong-test-secret-0123456789"


@override_settings(INSTANCE_API_KEY=STRONG_SECRET)
class ControlApiTests(TestCase):
    def setUp(self):
        from .auth import reset_failures

        reset_failures()

    def test_target_time_requires_api_key(self):
        response = self.client.post(
            "/api/target-time/",
            data=json.dumps({"hours": 3, "minutes": 15}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_push_json_parses_and_returns_payload(self):
        payload = {"name": "demo", "enabled": True, "count": 4}
        response = self.client.post(
            "/api/push-json/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_INSTANCE_KEY=STRONG_SECRET,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"], payload)


import os
import tempfile
from pathlib import Path

from .auth import MAX_FAILURES, reset_failures

SECRET = STRONG_SECRET


@override_settings(INSTANCE_API_KEY=STRONG_SECRET)
class ConfigEditorTests(TestCase):
    def setUp(self):
        reset_failures()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "music").mkdir()
        (base / "bg.mp4").write_bytes(b"x")
        self.config_path = base / "config.json"
        self.valid = {
            "music_dir": str(base / "music"),
            "background_video": str(base / "bg.mp4"),
            "rtmp_url": "rtmp://live.example/app/STREAMKEY123",
            "video_fps": 25,
            "profiles": {"mixcloud": {"video_size": "1280x720", "audio_bitrate": "320k"}},
        }
        self.config_path.write_text(json.dumps(self.valid))
        self.schedule_path = base / "startingSoonSchedule.js"
        self.schedule_path.write_text("export const TARGET_HOUR = 19;\nexport const TARGET_MINUTE = 45;\n")
        (base / "loops").mkdir()
        (base / "loops" / "a.mp4").write_bytes(b"x")
        (base / "loops" / "notes.txt").write_text("not a video")
        self.settings = override_settings(
            STREAM_CONFIG_PATH=self.config_path, STREAM_WORKDIR=base,
            STARTING_SOON_SCHEDULE_PATH=self.schedule_path, BACKGROUND_VIDEO_DIRS=[base / "loops"],
        )
        self.settings.enable()

    def tearDown(self):
        self.settings.disable()
        self.tmp.cleanup()

    def post(self, config, secret=SECRET, restart=False):
        headers = {"HTTP_X_INSTANCE_KEY": secret} if secret is not None else {}
        return self.client.post(
            "/api/config/", data=json.dumps({"config": config, "restart": restart}),
            content_type="application/json", **headers,
        )

    def test_page_and_schema_are_public_but_contain_no_config(self):
        page = self.client.get("/config/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"STREAMKEY123", page.content)
        schema = self.client.get("/api/config/schema/").json()
        self.assertIn("rtmp_url", [option["name"] for option in schema["options"]])
        self.assertNotIn(b"STREAMKEY123", self.client.get("/api/config/schema/").content)

    def test_loading_requires_the_secret(self):
        self.assertEqual(self.client.get("/api/config/").status_code, 401)
        self.assertEqual(self.client.get("/api/config/", HTTP_X_INSTANCE_KEY="wrong").status_code, 401)
        response = self.client.get("/api/config/", HTTP_X_INSTANCE_KEY=SECRET)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["rtmp_url"], self.valid["rtmp_url"])
        self.assertEqual(response["Cache-Control"].split(",")[0].strip(), "max-age=0")

    def test_saving_requires_the_secret_and_leaves_the_file_alone(self):
        changed = dict(self.valid, video_fps=30)
        for secret in (None, "", "wrong"):
            self.assertEqual(self.post(changed, secret=secret).status_code, 401)
        self.assertEqual(json.loads(self.config_path.read_text())["video_fps"], 25)

    def test_repeated_wrong_secrets_lock_the_client_out(self):
        for _ in range(MAX_FAILURES):
            self.assertEqual(self.client.get("/api/config/", HTTP_X_INSTANCE_KEY="wrong").status_code, 401)
        locked = self.client.get("/api/config/", HTTP_X_INSTANCE_KEY=SECRET)
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked)

    def test_invalid_values_are_rejected_per_field(self):
        bad = dict(self.valid, video_fps=500, video_size="1921x1080", video_preset="turbo",
                   music_dir="/does/not/exist", rtmp_url="http://nope", surprise=1,
                   profiles={"bad name!": {}, "ok": {"music_dir": "/x", "video_bitrate": "fast"}},
                   profile="missing")
        response = self.post(bad)
        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        for key in ("video_fps", "video_size", "video_preset", "music_dir", "rtmp_url", "surprise",
                    "profiles.bad name!", "profiles.ok.music_dir", "profiles.ok.video_bitrate", "profile"):
            self.assertIn(key, errors)
        self.assertEqual(json.loads(self.config_path.read_text()), self.valid)

    def test_valid_save_writes_atomically_with_backup(self):
        changed = dict(self.valid, video_fps=30, profile="mixcloud")
        response = self.post(changed)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(sorted(body["changed"]), ["profile", "video_fps"])
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["video_fps"], 30.0)
        self.assertEqual(saved["profile"], "mixcloud")
        backup = json.loads(self.config_path.with_name("config.json.bak").read_text())
        self.assertEqual(backup["video_fps"], 25)
        self.assertEqual(os.stat(self.config_path).st_mode & 0o777, 0o600)

    @override_settings(DEBUG=False, INSTANCE_API_KEY="short")
    def test_weak_server_secret_disables_the_api_in_production(self):
        response = self.client.get("/api/config/", HTTP_X_INSTANCE_KEY="short")
        self.assertEqual(response.status_code, 503)

    # -- show controls -------------------------------------------------------
    def show(self, changes, secret=SECRET):
        headers = {"HTTP_X_INSTANCE_KEY": secret} if secret is not None else {}
        return self.client.post("/api/show/", data=json.dumps(changes), content_type="application/json", **headers)

    def test_show_requires_the_secret(self):
        self.assertEqual(self.client.get("/api/show/").status_code, 401)
        self.assertEqual(self.show({"countdown_enabled": True}, secret="wrong").status_code, 401)
        self.assertNotIn("countdown_enabled", json.loads(self.config_path.read_text()))

    def test_show_lists_backgrounds_and_defaults(self):
        body = self.client.get("/api/show/", HTTP_X_INSTANCE_KEY=SECRET).json()
        self.assertEqual(sorted(body["backgrounds"]), ["bg.mp4", "loops/a.mp4"])
        self.assertEqual(body["show"]["background_video"], self.valid["background_video"])
        self.assertFalse(body["show"]["countdown_enabled"])
        self.assertEqual(body["show"]["menu_text"], "45 to 33 rpm\npopuri music")

    def test_show_update_saves_config_and_schedule(self):
        response = self.show({"background_video": "loops/a.mp4", "menu_text": " Disco night \n\n  vinyl only ",
                              "countdown_enabled": True, "countdown_hour": 21, "countdown_minute": 5})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["show"]["menu_text"], "Disco night\nvinyl only")
        self.assertIn("background_video", body["changed"])
        saved = json.loads(self.config_path.read_text())
        self.assertEqual((saved["countdown_enabled"], saved["countdown_hour"], saved["countdown_minute"]), (True, 21, 5))
        self.assertEqual(saved["profiles"], self.valid["profiles"])  # untouched
        self.assertEqual(self.schedule_path.read_text(),
                         "export const TARGET_HOUR = 21;\nexport const TARGET_MINUTE = 5;\n")

    def test_show_update_writes_where_the_active_profile_overrides(self):
        config = dict(self.valid, profile="mixcloud",
                      profiles={"mixcloud": {"background_video": self.valid["background_video"]}})
        self.config_path.write_text(json.dumps(config))
        self.assertEqual(self.show({"background_video": "loops/a.mp4"}).status_code, 200)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["profiles"]["mixcloud"]["background_video"], "loops/a.mp4")
        self.assertEqual(saved["background_video"], self.valid["background_video"])

    def test_show_rejects_invalid_values(self):
        errors = self.show({"countdown_hour": 24, "background_video": "loops/missing.mp4",
                            "menu_text": "1\n2\n3\n4\n5", "rtmp_url": "rtmp://x/y"}).json()["errors"]
        self.assertEqual(sorted(errors), ["rtmp_url"])  # unknown keys are refused first
        errors = self.show({"countdown_hour": 24, "background_video": "loops/missing.mp4",
                            "menu_text": "1\n2\n3\n4\n5"}).json()["errors"]
        self.assertEqual(sorted(errors), ["background_video", "countdown_hour", "menu_text"])
        self.assertEqual(json.loads(self.config_path.read_text()), self.valid)

    def test_target_time_is_the_countdown_time(self):
        response = self.client.post(
            "/api/target-time/",
            data=json.dumps({"hours": 2, "minutes": 45}),
            content_type="application/json",
            HTTP_X_INSTANCE_KEY=STRONG_SECRET,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"hours": 2, "minutes": 45})
        saved = json.loads(self.config_path.read_text())
        self.assertEqual((saved["countdown_hour"], saved["countdown_minute"]), (2, 45))
        self.assertIn("TARGET_HOUR = 2;", self.schedule_path.read_text())


from unittest import mock
from urllib.parse import parse_qs, urlsplit

from . import restream


@override_settings(INSTANCE_API_KEY=STRONG_SECRET)
class RestreamTests(TestCase):
    def setUp(self):
        reset_failures()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config_path = base / "config.json"
        self.config_path.write_text(json.dumps({"restream_client_id": "cid", "restream_client_secret": "csecret"}))
        self.state_path = base / "restream.json"
        self.settings = override_settings(STREAM_CONFIG_PATH=self.config_path, RESTREAM_STATE_PATH=self.state_path,
                                          RESTREAM_REDIRECT_URI="https://radio.example/restream/callback/")
        self.settings.enable()
        self.calls = []

    def tearDown(self):
        self.settings.disable()
        self.tmp.cleanup()

    def fake_restream(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        if url == restream.TOKEN_URL:
            fields = parse_qs(body.decode())
            return {"access_token": f"access-{len(self.calls)}", "refresh_token": f"refresh-{len(self.calls)}",
                    "expires_in": 3600, "refreshTokenExpiresIn": 31536000, "_fields": fields}
        if url.endswith("/v2/user/channels"):
            return {"channels": [{"id": 1, "displayName": "YouTube"}, {"id": 2, "displayName": "Custom RTMP"}]}
        if "/channel-meta/2" in url:
            raise restream.RestreamError("Restream refused the request (400): not supported")
        return {}

    def connect(self):
        url = self.client.post("/api/restream/connect/", HTTP_X_INSTANCE_KEY=SECRET).json()["url"]
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["client_id"], ["cid"])
        self.assertEqual(query["redirect_uri"], ["https://radio.example/restream/callback/"])
        with mock.patch.object(restream, "_request", side_effect=self.fake_restream):
            return self.client.get("/restream/callback/", {"code": "the-code", "state": query["state"][0]})

    def test_endpoints_require_the_secret(self):
        self.assertEqual(self.client.get("/api/restream/").status_code, 401)
        self.assertEqual(self.client.post("/api/restream/connect/").status_code, 401)

    def test_connect_stores_tokens_privately(self):
        response = self.connect()
        self.assertEqual(response.status_code, 302)
        method, url, body, headers = self.calls[0]
        self.assertEqual(parse_qs(body.decode())["grant_type"], ["authorization_code"])
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)
        self.assertTrue(restream.is_connected())

    def test_callback_rejects_unknown_state(self):
        with mock.patch.object(restream, "_request", side_effect=self.fake_restream):
            response = self.client.get("/restream/callback/", {"code": "x", "state": "forged"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_set_title_on_every_channel_reports_failures(self):
        self.connect()
        with mock.patch.object(restream, "_request", side_effect=self.fake_restream):
            response = self.client.post("/api/restream/", data=json.dumps({"title": "  Disco   night  "}),
                                        content_type="application/json", HTTP_X_INSTANCE_KEY=SECRET)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["title"], "Disco night")
        self.assertEqual([(item["name"], item["ok"]) for item in body["results"]], [("YouTube", True), ("Custom RTMP", False)])
        patch = [call for call in self.calls if call[0] == "PATCH"][0]
        self.assertTrue(patch[1].endswith("/v2/user/channel-meta/1"))
        self.assertEqual(json.loads(patch[2]), {"title": "Disco night"})
        self.assertEqual(patch[3]["Authorization"], "Bearer access-1")
        self.assertEqual(restream.last_title(), "Disco night")

    def test_expired_access_token_is_refreshed_and_rotated(self):
        self.connect()
        state = json.loads(self.state_path.read_text())
        state["expires_at"] = 0
        self.state_path.write_text(json.dumps(state))
        with mock.patch.object(restream, "_request", side_effect=self.fake_restream):
            self.client.get("/api/restream/", HTTP_X_INSTANCE_KEY=SECRET)
        refresh = self.calls[1]
        self.assertEqual(parse_qs(refresh[2].decode())["grant_type"], ["refresh_token"])
        self.assertEqual(parse_qs(refresh[2].decode())["refresh_token"], ["refresh-1"])
        self.assertEqual(json.loads(self.state_path.read_text())["refresh_token"], "refresh-2")

    def test_title_is_validated(self):
        for title in ("", "   ", "x" * 141, 5):
            response = self.client.post("/api/restream/", data=json.dumps({"title": title}),
                                        content_type="application/json", HTTP_X_INSTANCE_KEY=SECRET)
            self.assertEqual(response.status_code, 400, title)
