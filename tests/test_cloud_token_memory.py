import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server


class CloudTokenMemoryTests(unittest.TestCase):
    def setUp(self):
        self._old_runtime = dict(getattr(server, "CLOUD_RUNTIME_CONFIG", {}))
        if hasattr(server, "CLOUD_RUNTIME_CONFIG"):
            server.CLOUD_RUNTIME_CONFIG.clear()
            server.CLOUD_RUNTIME_CONFIG.update({"api_token": ""})
        self.client = server.app.test_client()

    def tearDown(self):
        if hasattr(server, "CLOUD_RUNTIME_CONFIG"):
            server.CLOUD_RUNTIME_CONFIG.clear()
            server.CLOUD_RUNTIME_CONFIG.update(self._old_runtime)

    def test_successful_connection_keeps_token_in_memory_but_not_on_disk(self):
        with tempfile.TemporaryDirectory(prefix="lifeup-token-test-") as tmpdir:
            config_path = os.path.join(tmpdir, "cloud.json")
            fake_result = {
                "route": "/info",
                "base_url": "http://127.0.0.1:13276",
                "data": {"apiVersion": 1},
            }
            with patch.object(server, "CLOUD_CONFIG_PATH", config_path), patch.object(
                server, "cloud_request", return_value=fake_result
            ):
                response = self.client.post(
                    "/api/cloud/test",
                    json={
                        "host": "127.0.0.1",
                        "port": 13276,
                        "api_token": "memory-only-secret",
                        "save": True,
                    },
                )

            self.assertEqual(response.status_code, 200, response.get_json())
            normalized = server.normalize_cloud_config(
                {"host": "127.0.0.1", "port": 13276}
            )
            self.assertEqual(normalized["api_token"], "memory-only-secret")
            with open(config_path, "r", encoding="utf-8") as config_file:
                saved = json.load(config_file)
            self.assertNotIn("api_token", saved)
            self.assertNotIn("memory-only-secret", json.dumps(saved))

    def test_token_can_be_cleared_from_process_memory(self):
        if hasattr(server, "CLOUD_RUNTIME_CONFIG"):
            server.CLOUD_RUNTIME_CONFIG["api_token"] = "secret"

        response = self.client.post(
            "/api/cloud/config",
            json={"host": "127.0.0.1", "port": 13276, "clear_token": True},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(response.get_json()["api_token_in_memory"])
        self.assertEqual(server.normalize_cloud_config({"host": "127.0.0.1"})["api_token"], "")


if __name__ == "__main__":
    unittest.main()
