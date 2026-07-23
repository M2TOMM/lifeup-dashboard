import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


SCHEMA_SQL = """
CREATE TABLE categorymodel (
    id INTEGER PRIMARY KEY,
    categoryname TEXT,
    isdelete INTEGER,
    orderincategory INTEGER
);
CREATE TABLE taskmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    categoryid INTEGER,
    taskstatus INTEGER,
    isfrozen INTEGER,
    isdeleterecord INTEGER,
    createdtime INTEGER,
    updatedtime INTEGER
);
CREATE TABLE userachcategorymodel (
    id INTEGER PRIMARY KEY,
    categoryname TEXT,
    isdelete INTEGER,
    orderincategory INTEGER
);
CREATE TABLE userachievementmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    categoryid INTEGER,
    achievementstatus INTEGER,
    isdelete INTEGER,
    createtime INTEGER,
    finishtime INTEGER,
    updatetime INTEGER
);
"""


class GoalMappingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lifeup-goals-")
        self.db_path = os.path.join(self.root, "LifeUpDB.db")
        self.config_path = os.path.join(self.root, "lifeup_goal_mappings.json")
        self._old_state = dict(server.STATE)
        self._config_patch = patch.object(
            server, "GOAL_CONFIG_PATH", self.config_path, create=True
        )
        self._config_patch.start()

        now = server.now_ms()
        recent = now - 2 * 24 * 60 * 60 * 1000
        old = now - 60 * 24 * 60 * 60 * 1000
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executemany(
                "INSERT INTO categorymodel VALUES (?, ?, ?, ?)",
                [(5, "修炼", 0, 0), (6, "生活", 0, 1)],
            )
            connection.executemany(
                "INSERT INTO userachcategorymodel VALUES (?, ?, ?, ?)",
                [(7, "里程碑", 0, 0), (8, "挑战", 0, 1)],
            )
            connection.executemany(
                "INSERT INTO taskmodel VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "完成修炼", 5, 1, 0, 0, old, recent),
                    (2, "继续修炼", 5, 0, 0, 0, old, recent),
                    (3, "已删除修炼", 5, 1, 0, 1, old, recent),
                    (4, "生活任务", 6, 1, 0, 0, old, recent),
                    (5, "放弃修炼", 5, 2, 0, 0, old, recent),
                ],
            )
            connection.executemany(
                "INSERT INTO userachievementmodel VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (11, "达成里程碑", 7, 1, 0, old, recent, recent),
                    (12, "冲击里程碑", 7, 0, 0, old, None, recent),
                    (13, "已删除成就", 7, 1, 1, old, recent, recent),
                    (14, "挑战完成", 8, 1, 0, old, recent, recent),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self.db_path,
                "tmpdir": self.root,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        self._config_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时宏愿测试目录没有清理")

    def valid_config(self):
        return {
            "version": 1,
            "goals": [
                {
                    "id": "cultivation",
                    "title": "修炼有成",
                    "description": "完成修炼任务与里程碑成就",
                    "target_count": 5,
                    "deadline": "2026-12-31",
                    "task_category_ids": [5],
                    "achievement_category_ids": [7],
                }
            ],
        }

    def save_config(self, config=None, expected_status=200):
        response = self.client.post(
            "/api/goals/config",
            json={"source": "local", **(config or self.valid_config())},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def test_empty_config_returns_real_categories_and_no_goals(self):
        response = self.client.get("/api/goals?source=local")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["goals"], [])
        self.assertEqual(payload["config"], {"version": 1, "goals": []})
        self.assertEqual(payload["meta"]["source"], "local")
        self.assertEqual(payload["meta"]["config_source"], "lifeup_goal_mappings.json")
        self.assertEqual(
            payload["category_options"]["tasks"],
            [{"id": 5, "name": "修炼"}, {"id": 6, "name": "生活"}],
        )
        self.assertEqual(
            payload["category_options"]["achievements"],
            [{"id": 7, "name": "里程碑"}, {"id": 8, "name": "挑战"}],
        )

    def test_save_config_calculates_traceable_progress_without_writing_database(self):
        before = self._entity_counts()

        saved = self.save_config()

        self.assertTrue(saved["ok"])
        self.assertTrue(os.path.isfile(self.config_path))
        with open(self.config_path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["version"], 1)
        self.assertEqual(on_disk["goals"][0]["id"], "cultivation")

        response = self.client.get("/api/goals?source=local")
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["configured"])
        goal = payload["goals"][0]
        self.assertEqual(goal["current_count"], 2)
        self.assertEqual(goal["target_count"], 5)
        self.assertEqual(goal["progress"], 40.0)
        self.assertEqual(goal["recent_count"], 2)
        self.assertEqual(goal["related_count"], 5)
        self.assertEqual(goal["completed_count"], 2)
        self.assertEqual(goal["missing_mappings"], [])
        self.assertEqual(
            {(row["kind"], row["id"]) for row in goal["related_records"]},
            {
                ("task", 1),
                ("task", 2),
                ("task", 5),
                ("achievement", 11),
                ("achievement", 12),
            },
        )
        self.assertEqual(self._entity_counts(), before)

    def test_deleted_categories_and_entities_are_excluded_and_reported(self):
        self.save_config()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("UPDATE categorymodel SET isdelete=1 WHERE id=5")
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/goals?source=local")

        self.assertEqual(response.status_code, 200, response.get_json())
        goal = response.get_json()["goals"][0]
        self.assertEqual(goal["current_count"], 1)
        self.assertEqual(goal["progress"], 20.0)
        self.assertEqual(goal["related_count"], 2)
        self.assertEqual(
            goal["missing_mappings"],
            [{"kind": "task_category", "id": 5}],
        )
        self.assertNotIn(3, [row["id"] for row in goal["related_records"]])
        self.assertNotIn(13, [row["id"] for row in goal["related_records"]])

    def test_invalid_config_is_rejected_without_creating_file(self):
        invalid = self.valid_config()
        invalid["goals"][0]["target_count"] = 0

        payload = self.save_config(invalid, expected_status=400)

        self.assertEqual(payload["code"], "GOAL_CONFIG_INVALID")
        self.assertFalse(os.path.exists(self.config_path))

    def test_corrupt_config_is_reported_without_fake_goals(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{not-json")

        response = self.client.get("/api/goals?source=local")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["goals"], [])
        self.assertTrue(payload["config_error"])

    def test_cloud_source_is_rejected_without_opening_local_database(self):
        with patch.object(server, "get_db", side_effect=AssertionError("local DB opened")):
            read_response = self.client.get("/api/goals?source=cloud")
            write_response = self.client.post(
                "/api/goals/config", json={"source": "cloud", **self.valid_config()}
            )

        self.assertEqual(read_response.status_code, 403, read_response.get_json())
        self.assertEqual(write_response.status_code, 403, write_response.get_json())
        self.assertEqual(read_response.get_json()["code"], "GOAL_MAPPING_LOCAL_ONLY")
        self.assertEqual(write_response.get_json()["code"], "GOAL_MAPPING_LOCAL_ONLY")

    def _entity_counts(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return {
                "tasks": connection.execute("SELECT COUNT(*) FROM taskmodel").fetchone()[0],
                "achievements": connection.execute(
                    "SELECT COUNT(*) FROM userachievementmodel"
                ).fetchone()[0],
            }
        finally:
            connection.close()


class GoalPageTests(unittest.TestCase):
    def test_goal_page_uses_selected_local_source_and_escapes_real_records(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js runtime is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
let source = '';
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) source += match[1] + '\n';
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
const content = { classList, style: {}, textContent: '', innerHTML: '', addEventListener: noop, querySelector: () => null, querySelectorAll: () => [] };
const generic = { classList, style: {}, textContent: '', innerHTML: '', addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], setAttribute: noop };
const document = {
  body: { classList }, getElementById: (id) => id === 'content' ? content : generic,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: (key) => key === 'lifeup_data_source' ? 'local' : null, setItem: noop, removeItem: noop };
const requests = [];
const payload = {
  meta: { source: 'local', config_source: 'lifeup_goal_mappings.json', refreshed_at: '2026-07-18 10:00:00' },
  configured: true, config: { version: 1, goals: [] }, config_error: '',
  category_options: { tasks: [{ id: 5, name: '修炼' }], achievements: [{ id: 7, name: '里程碑' }] },
  goals: [{
    id: 'cultivation', title: '<img src=x onerror=alert(1)>', description: '<script>bad()</script>',
    target_count: 5, current_count: 0, completed_count: 0, progress: 0, deadline: '2026-12-31',
    recent_count: 0, recent_days: 30, related_count: 2, missing_mappings: [],
    mapped_categories: { tasks: [{ id: 5, name: '修炼' }], achievements: [{ id: 7, name: '里程碑' }] },
    related_records: [
      { kind: 'task', id: 1, name: '<b>真实任务</b>', category_name: '修炼', completed: true, status_label: '已完成', updated_date: '2026-07-17' },
      { kind: 'achievement', id: 11, name: '达成里程碑', category_name: '里程碑', completed: true, status_label: '已完成', updated_date: '2026-07-16' }
    ]
  }]
};
const fetch = async (url) => { requests.push(url); return { ok: true, json: async () => payload }; };
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  setTimeout, clearTimeout, URLSearchParams, AbortController,
  confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  await sandbox.loadGoals();
  if (JSON.stringify(requests) !== JSON.stringify(['/api/goals?source=local'])) {
    throw new Error('goals page requested the wrong source: ' + JSON.stringify(requests));
  }
  if (!content.innerHTML.includes('0 / 5') || !content.innerHTML.includes('近 30 天 +0') || !content.innerHTML.includes('进度 0%') || !content.innerHTML.includes('达成里程碑')) {
    throw new Error('real goal summary or traceable records are missing');
  }
  if (content.innerHTML.includes('<img src=x') || content.innerHTML.includes('<script>bad()')) {
    throw new Error('goal database content was rendered without escaping');
  }
  if (content.innerHTML.includes('演示数据')) throw new Error('goals page still uses demo label');
  const loaderSource = sandbox.loadGoals.toString();
  if (loaderSource.includes('getMockCultivationData') || loaderSource.includes('Math.random')) {
    throw new Error('goals loader still depends on random mock data');
  }

  requests.length = 0;
  sandbox.dataSource = 'cloud';
  await sandbox.loadGoals();
  if (requests.length !== 0 || !content.innerHTML.includes('只支持本地备份')) {
    throw new Error('cloud mode did not stay isolated from local goal data');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
