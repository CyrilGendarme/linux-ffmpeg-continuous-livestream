import json

from django.test import TestCase


class ControlApiTests(TestCase):
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
            HTTP_X_INSTANCE_KEY="test-secret-key",
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
            HTTP_X_INSTANCE_KEY="test-secret-key",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"], payload)
