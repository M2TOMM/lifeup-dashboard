import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_NOW = datetime(2026, 7, 18, 12, 0, 0)


def ms(year, month, day, hour=12):
    return int(datetime(year, month, day, hour).timestamp() * 1000)


SCHEMA_SQL = """
CREATE TABLE categorymodel (id INTEGER PRIMARY KEY, categoryname TEXT);
CREATE TABLE taskmodel (
    id INTEGER PRIMARY KEY, content TEXT, categoryid INTEGER, taskstatus INTEGER,
    isdeleterecord INTEGER, updatedtime INTEGER, endtime INTEGER, createdtime INTEGER,
    rewardcoin INTEGER, expreward INTEGER
);
CREATE TABLE tomatomodel (
    id INTEGER PRIMARY KEY, createtime INTEGER, lasttime INTEGER, endtime INTEGER,
    isabandoned INTEGER, starttime INTEGER, isdel INTEGER, taskmodelid INTEGER
);
CREATE TABLE coinmodel (
    id INTEGER PRIMARY KEY, createtime INTEGER, isdecrease INTEGER,
    changedvalue INTEGER, isdel INTEGER, content TEXT, rescode INTEGER, relatedid INTEGER
);
CREATE TABLE expmodel (
    id INTEGER PRIMARY KEY, createtime INTEGER, isdecrease INTEGER,
    value INTEGER, isdel INTEGER, content TEXT, rescode INTEGER, relatedid INTEGER
);
CREATE TABLE userachcategorymodel (id INTEGER PRIMARY KEY, categoryname TEXT);
CREATE TABLE userachievementmodel (
    id INTEGER PRIMARY KEY, content TEXT, categoryid INTEGER, achievementstatus INTEGER,
    isdelete INTEGER, createtime INTEGER, finishtime INTEGER, updatetime INTEGER
);
"""


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lifeup-review-")
        self.db_path = os.path.join(self.root, "LifeUpDB.db")
        self._old_state = dict(server.STATE)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute("INSERT INTO categorymodel VALUES (5, '修炼')")
            connection.execute("INSERT INTO userachcategorymodel VALUES (7, '里程碑')")
            connection.executemany(
                "INSERT INTO taskmodel VALUES (?, ?, 5, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "本周修炼一", 1, 0, ms(2026, 7, 14), 0, 0, 10, 20),
                    (2, "本周修炼二", 1, 0, ms(2026, 7, 17), 0, 0, 15, 25),
                    (3, "上周修炼", 1, 0, ms(2026, 7, 8), 0, 0, 5, 8),
                    (4, "进行中", 0, 0, ms(2026, 7, 16), 0, 0, 99, 99),
                    (5, "已删除", 1, 1, ms(2026, 7, 15), 0, 0, 99, 99),
                    (6, "本周已放弃", 2, 0, ms(2026, 7, 18), 0, 0, 50, 80),
                ],
            )
            connection.executemany(
                "INSERT INTO tomatomodel VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, ms(2026, 7, 14), 25 * 60000, ms(2026, 7, 14, 13), 0, ms(2026, 7, 14), 0, 1),
                    (2, ms(2026, 7, 17), 30 * 60000, ms(2026, 7, 17, 13), 0, ms(2026, 7, 17), 0, 2),
                    (3, ms(2026, 7, 9), 20 * 60000, ms(2026, 7, 9, 13), 0, ms(2026, 7, 9), 0, 3),
                    (4, ms(2026, 7, 16), 60 * 60000, ms(2026, 7, 16, 13), 1, ms(2026, 7, 16), 0, 4),
                ],
            )
            connection.executemany(
                "INSERT INTO coinmodel VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                [
                    (1, ms(2026, 7, 14), 0, 100, "任务奖励", 2, 1),
                    (2, ms(2026, 7, 17), 1, 30, "购买商品", 0, 9),
                    (3, ms(2026, 7, 8), 0, 20, "上周奖励", 2, 3),
                ],
            )
            connection.executemany(
                "INSERT INTO expmodel VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                [
                    (1, ms(2026, 7, 15), 0, 50, "获得经验", 2, 1),
                    (2, ms(2026, 7, 17), 1, 10, "扣除经验", 3, 2),
                    (3, ms(2026, 7, 10), 0, 5, "上周经验", 2, 3),
                ],
            )
            connection.executemany(
                "INSERT INTO userachievementmodel VALUES (?, ?, 7, ?, ?, ?, ?, ?)",
                [
                    (1, "本周成就", 1, 0, ms(2026, 6, 1), ms(2026, 7, 16), ms(2026, 7, 16)),
                    (2, "上周成就", 2, 0, ms(2026, 6, 1), ms(2026, 7, 9), ms(2026, 7, 9)),
                    (3, "未完成成就", 0, 0, ms(2026, 7, 15), 0, ms(2026, 7, 15)),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        server.STATE.update({
            "backup_path": None,
            "db_path": self.db_path,
            "tmpdir": self.root,
            "loaded": True,
        })
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时周复盘数据库目录没有清理")

    def get_review(self, source="local", period="week", expected_status=200):
        with patch.object(server, "review_reference_now", return_value=REFERENCE_NOW):
            response = self.client.get(f"/api/review?source={source}&period={period}")
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def test_local_week_report_uses_real_traceable_records(self):
        payload = self.get_review()

        self.assertEqual(payload["meta"]["source"], "local")
        self.assertEqual(payload["window"]["start"], "2026-07-13")
        self.assertEqual(payload["window"]["end"], "2026-07-19")
        expected = {
            "focus_minutes": (55, 20),
            "tasks_completed": (2, 1),
            "coin_change": (70, 20),
            "exp_change": (40, 5),
            "achievements_completed": (1, 1),
        }
        for key, values in expected.items():
            metric = payload["metrics"][key]
            self.assertTrue(metric["available"], key)
            self.assertEqual((metric["value"], metric["previous_value"]), values, key)
            self.assertEqual(len(metric["current_records"]), metric["record_count"], key)
            self.assertEqual(len(metric["previous_records"]), metric["previous_record_count"], key)

        task_names = {row["name"] for row in payload["metrics"]["tasks_completed"]["current_records"]}
        self.assertEqual(task_names, {"本周修炼一", "本周修炼二"})
        self.assertEqual(len(payload["series"]), 7)
        self.assertEqual(sum(row["focus_minutes"] for row in payload["series"]), 55)
        self.assertNotEqual(payload["insights"], [])

    def test_natural_month_and_year_windows_are_stable(self):
        month = self.get_review(period="month")
        year = self.get_review(period="year")

        self.assertEqual((month["window"]["start"], month["window"]["end"]), ("2026-07-01", "2026-07-31"))
        self.assertEqual(len(month["series"]), 31)
        self.assertEqual((year["window"]["start"], year["window"]["end"]), ("2026-01-01", "2026-12-31"))
        self.assertEqual(len(year["series"]), 12)

    def test_invalid_period_is_rejected(self):
        payload = self.get_review(period="rolling", expected_status=400)
        self.assertEqual(payload["code"], "REVIEW_PERIOD_INVALID")

    def test_cloud_report_never_opens_local_database_and_marks_missing_ledger_data(self):
        def fake_cloud_request(_config, route, timeout=12):
            data = {
                "/tasks": [
                    {"id": 10, "name": "手机任务", "status": 1, "finishedTime": ms(2026, 7, 15)},
                    {"id": 11, "name": "手机已放弃", "status": 2, "finishedTime": ms(2026, 7, 16)},
                ],
                "/pomodoro_records": [{"id": 20, "taskName": "手机番茄", "startTime": ms(2026, 7, 16), "durationMinutes": 35}],
                "/achievement_categories": [{"id": 7, "name": "手机成就"}],
                "/achievements/7": [{"id": 30, "name": "云端里程碑", "status": 1, "finishTime": ms(2026, 7, 17)}],
            }.get(route, [])
            return {"route": route, "base_url": "http://phone", "data": data}

        with patch.object(server, "review_reference_now", return_value=REFERENCE_NOW), patch.object(
            server, "get_db", side_effect=AssertionError("cloud report opened local DB")
        ), patch.object(server, "cloud_request", side_effect=fake_cloud_request):
            response = self.client.get("/api/review?source=cloud&period=week")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["meta"]["source"], "cloud")
        self.assertEqual(payload["metrics"]["tasks_completed"]["value"], 1)
        self.assertEqual(payload["metrics"]["focus_minutes"]["value"], 35)
        self.assertEqual(payload["metrics"]["achievements_completed"]["value"], 1)
        self.assertFalse(payload["metrics"]["coin_change"]["available"])
        self.assertFalse(payload["metrics"]["exp_change"]["available"])
        self.assertTrue(any("流水" in gap for gap in payload["gaps"]))


class ReviewPageTests(unittest.TestCase):
    def test_review_page_uses_selected_source_and_escapes_trace_records(self):
        node = os.environ.get("NODE_BINARY") or shutil.which("node") or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
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
const document = { body: { classList }, getElementById: (id) => id === 'content' ? content : generic, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, createElement: () => generic };
const localStorage = { getItem: () => 'local', setItem: noop, removeItem: noop };
const requests = [];
const metric = (label, value, previous, records) => ({ label, value, previous_value: previous, delta: value - previous, unit: '条', available: true, record_count: records.length, previous_record_count: 0, current_records: records, previous_records: [], source_label: '真实记录', missing_reason: '' });
const payload = {
  meta: { source: 'local', source_label: '本地备份数据', refreshed_at: '2026-07-18 12:00:00' },
  window: { period: 'week', label: '本周', start: '2026-07-13', end: '2026-07-19', previous_start: '2026-07-06', previous_end: '2026-07-12', comparison_label: '上周' },
  metrics: {
    focus_minutes: { ...metric('番茄专注时长', 35, 20, [{ id: 1, name: '<img src=x onerror=alert(1)>', date: '2026-07-16', time: '2026-07-16 12:00:00', value: 35, detail: '完成' }]), unit: '分钟' },
    tasks_completed: metric('完成任务数', 1, 0, [{ id: 2, name: '<b>真实任务</b>', date: '2026-07-17', time: '2026-07-17 12:00:00', value: 1, detail: '修炼' }]),
    coin_change: { ...metric('金币净变化', 5, 0, [],), unit: '金币' },
    exp_change: { ...metric('经验净变化', 0, 0, []), available: false, missing_reason: '没有经验流水' },
    achievements_completed: metric('完成成就数', 1, 0, [{ id: 3, name: '云端里程碑', date: '2026-07-18', time: '2026-07-18 12:00:00', value: 1, detail: '里程碑' }])
  },
  series: [{ label: '7/16', date: '2026-07-16', focus_minutes: 35, tasks_completed: 1, coin_change: 5, exp_change: 0, achievements_completed: 0 }],
  insights: [{ icon: '✅', text: '番茄专注时长比上周增加 15 分钟。', metric: 'focus_minutes' }],
  gaps: ['没有经验流水']
};
const fetch = async (url) => { requests.push(url); return { ok: true, json: async () => payload }; };
const sandbox = { console, document, localStorage, fetch, window: { addEventListener: noop }, setTimeout, clearTimeout, URLSearchParams, AbortController, confirm: () => false, alert: noop, Blob, URL };
sandbox.window.window = sandbox.window; sandbox.window.document = document; sandbox.window.localStorage = localStorage;
vm.createContext(sandbox); vm.runInContext(source, sandbox);
(async () => {
  await sandbox.loadReview();
  if (JSON.stringify(requests) !== JSON.stringify(['/api/review?source=local&period=week'])) throw new Error('wrong review request: ' + JSON.stringify(requests));
  for (const expected of ['本地备份数据', '35m', '完成任务数', '来源明细', '没有经验流水', '云端里程碑']) if (!content.innerHTML.includes(expected)) throw new Error('missing real review content: ' + expected);
  if (content.innerHTML.includes('<img src=x') || content.innerHTML.includes('<b>真实任务</b>')) throw new Error('review record was not escaped');
  if (content.innerHTML.includes('演示数据')) throw new Error('review still shows demo label');
  const loader = sandbox.loadReview.toString();
  if (loader.includes('getMockCultivationData') || loader.includes('Math.random')) throw new Error('review loader still depends on mock data');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
