import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

import server


class CloudPreviewExecutionTests(unittest.TestCase):
    def setUp(self):
        getattr(server, "CLOUD_PREVIEWS", {}).clear()
        getattr(server, "CLOUD_EXECUTIONS", {}).clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_patch = patch.object(
            server,
            "CLOUD_OPERATION_LOG_PATH",
            str(Path(self.tempdir.name) / "cloud-operation-log.jsonl"),
        )
        self.log_patch.start()
        self.client = server.app.test_client()
        self.connection = {
            "host": "127.0.0.1",
            "port": 13276,
            "api_token": "temporary-token",
        }
        self.url = "lifeup://api/add_task?todo=Previewed"

    def tearDown(self):
        self.log_patch.stop()
        self.tempdir.cleanup()
        getattr(server, "CLOUD_PREVIEWS", {}).clear()
        getattr(server, "CLOUD_EXECUTIONS", {}).clear()

    def preview(self):
        response = self.client.post(
            "/api/cloud/preview",
            json={**self.connection, "urls": [self.url]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["items"][0]["title"], "Previewed")
        return payload["preview_token"]

    def test_execute_rejects_unpreviewed_urls(self):
        with patch.object(server, "cloud_post_json") as cloud_post:
            response = self.client.post(
                "/api/cloud/execute",
                json={**self.connection, "urls": [self.url]},
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("预览", response.get_json()["error"])
        cloud_post.assert_not_called()

    def test_preview_token_is_one_time_and_idempotent(self):
        token = self.preview()
        fake_result = {
            "route": "/api/contentprovider",
            "base_url": "http://127.0.0.1:13276",
            "data": [{"ok": True}],
            "response": {"code": 200, "data": [{"ok": True}]},
        }

        with patch.object(server, "cloud_post_json", return_value=fake_result) as cloud_post:
            first = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "single-task-1"},
            )
            replay = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "single-task-1"},
            )
            second_execution = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "single-task-2"},
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json()["count"], 1)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertEqual(second_execution.status_code, 409, second_execution.get_json())
        cloud_post.assert_called_once()
        config, route, payload = cloud_post.call_args.args[:3]
        self.assertEqual(config["api_token"], "temporary-token")
        self.assertEqual(route, "/api/contentprovider")
        self.assertEqual(payload, {"urls": [self.url]})

    def test_preview_rejects_invalid_task_parameters(self):
        invalid_urls = [
            "lifeup://api/add_task?todo=Bad&coin=abc",
            "lifeup://api/add_task?todo=Bad&coin=-1",
            "lifeup://api/add_task?todo=Bad&importance=999",
            "lifeup://api/add_task?todo=Bad&difficulty=0",
            "lifeup://api/add_task?todo=Bad&skills=abc",
            "lifeup://api/add_task?todo=Bad&frequency=abc",
            "lifeup://api/add_task?todo=Bad&unknown=value",
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                response = self.client.post(
                    "/api/cloud/preview",
                    json={**self.connection, "urls": [url]},
                )
                self.assertEqual(response.status_code, 400, response.get_json())


if __name__ == "__main__":
    unittest.main()
