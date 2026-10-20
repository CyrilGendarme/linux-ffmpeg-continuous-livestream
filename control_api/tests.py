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

    def test_target_time_updates_and_returns_state(self):
        response = self.client.post(
            "/api/target-time/",
            data=json.dumps({"hours": 2, "minutes": 45}),
            content_type="application/json",
            HTTP_X_INSTANCE_KEY=STRONG_SECRET,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hours"], 2)
        self.assertEqual(response.json()["minutes"], 45)

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
        self.settings = override_settings(STREAM_CONFIG_PATH=self.config_path, STREAM_WORKDIR=base)
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
