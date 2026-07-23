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


def ms(year, month, day, hour=12, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


SCHEMA_SQL = """
CREATE TABLE categorymodel (id INTEGER PRIMARY KEY, categoryname TEXT);
CREATE TABLE taskmodel (
    id INTEGER PRIMARY KEY, content TEXT, categoryid INTEGER, taskstatus INTEGER,
    isdeleterecord INTEGER, updatedtime INTEGER, endtime INTEGER, createdtime INTEGER
);
CREATE TABLE tomatomodel (
    id INTEGER PRIMARY KEY, createtime INTEGER, lasttime INTEGER, endtime INTEGER,
    isabandoned INTEGER, starttime INTEGER, isdel INTEGER, taskmodelid INTEGER
);
"""


class DailyFocusApiTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lifeup-daily-focus-")
        self.db_path = os.path.join(self.root, "LifeUpDB.db")
        self._old_state = dict(server.STATE)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute("INSERT INTO categorymodel VALUES (5, '日常')")
            connection.executemany(
                "INSERT INTO taskmodel VALUES (?, ?, 5, ?, ?, ?, ?, ?)",
                [
                    (1, "午夜功课", 1, 0, ms(2026, 7, 13, 0, 30), 0, 0),
                    (2, "连续功课一", 1, 0, ms(2026, 7, 14), 0, 0),
                    (3, "连续功课二", 1, 0, ms(2026, 7, 15), 0, 0),
                    (4, "上月功课", 1, 0, ms(2026, 6, 30), 0, 0),
                    (5, "未完成", 0, 0, ms(2026, 7, 16), 0, 0),
                    (6, "已删除", 1, 1, ms(2026, 7, 16), 0, 0),
                    (7, "已放弃功课", 2, 0, ms(2026, 7, 17), 0, 0),
                ],
            )
            connection.executemany(
                "INSERT INTO tomatomodel VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, ms(2026, 7, 14), 25 * 60000, ms(2026, 7, 14, 13), 0, ms(2026, 7, 14), 0, 2),
                    (2, ms(2026, 7, 15), 35, ms(2026, 7, 15, 13), 0, ms(2026, 7, 15), 0, 3),
                    (3, ms(2026, 7, 16), 30 * 60000, ms(2026, 7, 16, 13), 0, ms(2026, 7, 16), 0, 5),
                    (4, ms(2026, 7, 17), 60 * 60000, ms(2026, 7, 17, 13), 1, ms(2026, 7, 17), 0, 5),
                    (5, ms(2026, 7, 18), 60 * 60000, ms(2026, 7, 18, 13), 0, ms(2026, 7, 18), 1, 5),
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
        self.assertFalse(os.path.exists(self.root), "临时热力图数据库目录没有清理")

    def get_activity(self, period="month", source="local", expected_status=200):
        with patch.object(server, "review_reference_now", return_value=REFERENCE_NOW):
            response = self.client.get(f"/api/activity/heatmap?source={source}&period={period}")
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def test_local_month_uses_real_daily_task_and_tomato_records(self):
        payload = self.get_activity()

        self.assertEqual(payload["meta"]["source"], "local")
        self.assertEqual((payload["window"]["start"], payload["window"]["end"]), ("2026-07-01", "2026-07-31"))
        self.assertEqual(len(payload["series"]), 31)
        by_date = {row["date"]: row for row in payload["series"]}
        self.assertEqual(by_date["2026-07-13"]["tasks_completed"], 1)
        self.assertEqual(by_date["2026-07-14"]["tasks_completed"], 1)
        self.assertEqual(by_date["2026-07-14"]["focus_minutes"], 25)
        self.assertEqual(by_date["2026-07-15"]["focus_minutes"], 35)
        self.assertEqual(by_date["2026-07-16"]["focus_minutes"], 30)
        self.assertEqual(payload["metrics"]["tasks_completed"]["value"], 3)
        self.assertEqual(payload["metrics"]["focus_minutes"]["value"], 90)
        self.assertEqual(payload["streaks"]["tasks"]["longest"], 3)
        self.assertEqual(payload["streaks"]["focus"]["longest"], 3)
        self.assertEqual(payload["streaks"]["active"]["longest"], 4)
        self.assertEqual(payload["streaks"]["active"]["current"], 0)
        self.assertEqual(len(payload["records"]["tasks"]), 3)
        self.assertEqual(len(payload["records"]["focus"]), 3)

    def test_day_and_natural_week_ranges_use_local_timezone_boundaries(self):
        day = self.get_activity(period="day")
        week = self.get_activity(period="week")

        self.assertEqual((day["window"]["start"], day["window"]["end"]), ("2026-07-18", "2026-07-18"))
        self.assertEqual(len(day["series"]), 1)
        self.assertEqual((week["window"]["start"], week["window"]["end"]), ("2026-07-13", "2026-07-19"))
        self.assertEqual(len(week["series"]), 7)
        self.assertEqual(week["series"][0]["tasks_completed"], 1)

    def test_missing_compatible_fields_are_reported_without_guessed_data(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("ALTER TABLE taskmodel RENAME TO taskmodel_full")
            connection.execute("CREATE TABLE taskmodel (id INTEGER, content TEXT, taskstatus INTEGER, isdeleterecord INTEGER)")
            connection.execute("ALTER TABLE tomatomodel RENAME TO tomatomodel_full")
            connection.execute("CREATE TABLE tomatomodel (id INTEGER, starttime INTEGER, isdel INTEGER, isabandoned INTEGER)")
            connection.commit()
        finally:
            connection.close()

        payload = self.get_activity()
        self.assertFalse(payload["metrics"]["tasks_completed"]["available"])
        self.assertFalse(payload["metrics"]["focus_minutes"]["available"])
        self.assertIsNone(payload["series"][0]["tasks_completed"])
        self.assertIsNone(payload["series"][0]["focus_minutes"])
        self.assertTrue(any("字段" in gap for gap in payload["gaps"]))

    def test_cloud_mapping_never_opens_local_database_and_reports_missing_times(self):
        def fake_cloud_request(_config, route, timeout=12):
            data = {
                "/tasks": [
                    {"id": 10, "name": "手机任务", "status": 1, "finishedTime": "2026-07-17T16:15:00Z"},
                    {"id": 11, "name": "缺时间任务", "status": 1},
                    {"id": 12, "name": "手机已放弃", "status": 2, "finishedTime": ms(2026, 7, 18)},
                ],
                "/pomodoro_records": [
                    {"id": 20, "taskName": "手机番茄", "startTime": ms(2026, 7, 18, 1), "durationMinutes": 45},
                    {"id": 21, "taskName": "毫秒番茄", "createdTime": ms(2026, 7, 17), "lastTime": 30 * 60000},
                ],
            }[route]
            return {"route": route, "base_url": "http://phone", "data": data}

        with patch.object(server, "review_reference_now", return_value=REFERENCE_NOW), patch.object(
            server, "get_db", side_effect=AssertionError("cloud heatmap opened local DB")
        ), patch.object(server, "cloud_request", side_effect=fake_cloud_request):
            response = self.client.get("/api/activity/heatmap?source=cloud&period=week")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["meta"]["source"], "cloud")
        self.assertEqual(payload["metrics"]["tasks_completed"]["value"], 1)
        self.assertEqual(payload["metrics"]["focus_minutes"]["value"], 75)
        task_record = payload["records"]["tasks"][0]
        self.assertEqual(task_record["date"], "2026-07-18")
        self.assertTrue(any("缺少完成时间" in gap for gap in payload["gaps"]))

    def test_invalid_period_is_rejected(self):
        payload = self.get_activity(period="year", expected_status=400)
        self.assertEqual(payload["code"], "ACTIVITY_PERIOD_INVALID")


class DailyFocusPageTests(unittest.TestCase):
    def test_daily_and_focus_pages_render_real_heatmaps_without_mock_series(self):
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
const generic = { classList, style: {}, textContent: '', innerHTML: '', disabled: false, addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], setAttribute: noop };
const document = { body: { classList }, getElementById: (id) => id === 'content' ? content : generic, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, createElement: () => generic };
const localStorage = { getItem: () => 'local', setItem: noop, removeItem: noop };
const requests = [];
const activity = {
  meta: { source: 'local', source_label: '本地备份数据', refreshed_at: '2026-07-18 12:00:00' },
  window: { period: 'month', label: '本月', start: '2026-07-01', end: '2026-07-31', timezone_label: '电脑本地时区' },
  metrics: {
    tasks_completed: { label: '完成任务数', unit: '条', available: true, value: 2, record_count: 2, missing_reason: '' },
    focus_minutes: { label: '番茄专注时长', unit: '分钟', available: true, value: 35, record_count: 1, missing_reason: '' }
  },
  series: [{ label: '7/18', date: '2026-07-18', tasks_completed: 2, focus_minutes: 35, active: true }],
  streaks: { tasks: { current: 1, longest: 2 }, focus: { current: 1, longest: 1 }, active: { current: 1, longest: 2 } },
  records: { tasks: [{ id: 1, name: '<img src=x onerror=alert(1)>', date: '2026-07-18', time: '2026-07-18 08:00:00', value: 1, detail: '日常' }], focus: [] },
  gaps: []
};
const focus = {
  meta: { source: 'local', source_label: '本地备份数据' }, todayFocusMin: 35, weekFocusMin: 35, monthFocusMin: 35,
  weekBarData: [{ label: '7/18', value: 35 }], hourDistribution: [], focusSessions: [], rewardHint: '真实奖励提示', filterHint: '真实筛选提示'
};
const fetch = async (url) => {
  requests.push(url);
  let payload = [];
  if (url.startsWith('/api/activity/heatmap')) payload = activity;
  else if (url.startsWith('/api/focus/overview')) payload = focus;
  return { ok: true, json: async () => payload };
};
const sandbox = { console, document, localStorage, fetch, window: { addEventListener: noop }, location: { origin: 'http://127.0.0.1:5000' }, setTimeout, clearTimeout, URLSearchParams, AbortController, confirm: () => false, alert: noop, Blob, URL };
sandbox.window.window = sandbox.window; sandbox.window.document = document; sandbox.window.localStorage = localStorage;
vm.createContext(sandbox); vm.runInContext(source, sandbox);
(async () => {
  await sandbox.loadDaily();
  for (const expected of ['本地备份数据', '真实任务完成热力图', '本月', '连续 1 天', '尚未找到三大功课分类']) if (!content.innerHTML.includes(expected)) throw new Error('daily missing: ' + expected);
  if (content.innerHTML.includes('<img src=x')) throw new Error('daily trace record was not escaped');
  const dailyLoader = sandbox.loadDaily.toString();
  if (dailyLoader.includes('_mockDailyCheck') || dailyLoader.includes('Math.random')) throw new Error('daily loader still uses mock heatmap data');
  await sandbox.loadFocus();
  for (const expected of ['真实番茄热力图', '本地备份数据', '连续 1 天']) if (!content.innerHTML.includes(expected)) throw new Error('focus missing: ' + expected);
  if (!requests.includes('/api/activity/heatmap?source=local&period=month')) throw new Error('activity request missing: ' + JSON.stringify(requests));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
