import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import server


SCHEMA_SQL = """
CREATE TABLE taskmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    taskfrequency INTEGER,
    rewardcoin INTEGER,
    expreward INTEGER,
    remark TEXT,
    taskstatus INTEGER,
    currenttimes INTEGER,
    categoryid INTEGER,
    createdtime INTEGER,
    updatedtime INTEGER,
    isdeleterecord INTEGER,
    isfrozen INTEGER,
    tagcolor INTEGER,
    taskdifficultydegree INTEGER,
    taskurgencydegree INTEGER,
    groupid INTEGER,
    tasktype INTEGER,
    starttime INTEGER,
    endtime INTEGER,
    rewardcoinvariable INTEGER,
    extrainfo TEXT,
    enableebbinghausmode INTEGER,
    ishandleoverdue INTEGER,
    tasktargetid INTEGER
);
CREATE TABLE categorymodel (id INTEGER PRIMARY KEY, categoryname TEXT);
CREATE TABLE tasktargetmodel (id INTEGER PRIMARY KEY, targettimes INTEGER);
CREATE TABLE taskmodel_skillids (taskmodel_id INTEGER, skillids INTEGER);
CREATE TABLE taskrewardmodel (
    id INTEGER PRIMARY KEY, taskmodelid INTEGER, shopitemmodelid INTEGER, amount INTEGER
);
CREATE TABLE usermodel (id INTEGER PRIMARY KEY, nickname TEXT, userhead TEXT, userid TEXT);
CREATE TABLE coinmodel (id INTEGER PRIMARY KEY, savingbalance INTEGER);
CREATE TABLE recordmodel (
    id INTEGER PRIMARY KEY,
    usingdays INTEGER,
    currentusingdaystreak INTEGER,
    longestusingdaystreak INTEGER
);
CREATE TABLE shopitemmodel (
    id INTEGER PRIMARY KEY, itemname TEXT, price INTEGER, isdel INTEGER
);
CREATE TABLE inventorymodel (id INTEGER PRIMARY KEY, stocknumber INTEGER);
CREATE TABLE inventoryrecordmodel (
    id INTEGER PRIMARY KEY,
    createtime INTEGER,
    isdecrease INTEGER,
    changenumber INTEGER,
    desc_lpcolumn TEXT,
    shopitemmodel_id INTEGER
);
CREATE TABLE userachievementmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    achievementstatus INTEGER,
    currentvalue INTEGER,
    progress INTEGER,
    createtime INTEGER,
    updatetime INTEGER,
    categoryid INTEGER,
    isdelete INTEGER,
    orderincategory INTEGER
);
CREATE TABLE userachcategorymodel (
    id INTEGER PRIMARY KEY, categoryname TEXT, orderincategory INTEGER
);
CREATE TABLE unlockconditionmodel (
    id INTEGER PRIMARY KEY,
    userachievementid INTEGER,
    currentvalue INTEGER,
    targetvalues INTEGER,
    progress INTEGER,
    isdel INTEGER
);
CREATE TABLE skillmodel (
    id INTEGER PRIMARY KEY, content TEXT, experience INTEGER, isdel INTEGER
);
"""


class TaskStatusSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lifeup-task-status-")
        self.db_path = os.path.join(self.root, "LifeUpDB.db")
        self._old_state = dict(server.STATE)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute("INSERT INTO categorymodel VALUES (5, '修炼')")
            connection.execute("INSERT INTO usermodel VALUES (1, '测试用户', '', 'local-test')")
            connection.execute("INSERT INTO coinmodel VALUES (1, 100)")
            connection.execute("INSERT INTO recordmodel VALUES (1, 10, 2, 5)")
            connection.executemany(
                """
                INSERT INTO taskmodel (
                    id, content, taskfrequency, rewardcoin, expreward, remark,
                    taskstatus, currenttimes, categoryid, createdtime, updatedtime,
                    isdeleterecord, isfrozen, tagcolor, taskdifficultydegree,
                    taskurgencydegree, groupid, tasktype, starttime, endtime,
                    rewardcoinvariable, extrainfo, enableebbinghausmode,
                    ishandleoverdue, tasktargetid
                ) VALUES (?, ?, 0, ?, ?, '', ?, 1, 5, ?, ?, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, '', 0, 0, NULL)
                """,
                [
                    (1, "待完成任务", 10, 15, 0, 1000, 1000),
                    (2, "已完成任务", 20, 30, 1, 2000, 2000),
                    (3, "已放弃任务", 40, 60, 2, 3000, 3000),
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
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时任务状态数据库目录没有清理")

    def get_json(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_local_dashboard_and_done_filter_exclude_abandoned_tasks(self):
        overview = self.get_json("/api/dashboard/overview?source=local")
        done_tasks = self.get_json("/api/tasks?source=local&filter=done")

        self.assertEqual(overview["tasks"]["total"], 3)
        self.assertEqual(overview["tasks"]["active"], 1)
        self.assertEqual(overview["tasks"]["done"], 1)
        self.assertEqual([row["id"] for row in done_tasks], [2])

    def test_history_economy_and_history_export_exclude_abandoned_tasks(self):
        history = self.get_json("/api/history")
        economy = self.get_json("/api/economy")
        exported = self.get_json("/api/export/history?format=json")

        task_events = [row for row in history["events"] if row["type"] == "task"]
        self.assertEqual([row["id"] for row in task_events], [2])
        self.assertEqual(history["task_count"], 1)
        self.assertEqual(economy["total_coin"], 20)
        self.assertEqual(economy["total_exp"], 30)
        self.assertEqual([row["title"] for row in exported], ["已完成任务"])

    def test_cloud_done_filter_and_dashboard_exclude_abandoned_tasks(self):
        cloud_tasks = [
            {"id": 1, "name": "手机待完成", "status": 0},
            {"id": 2, "name": "手机已完成", "status": 1},
            {"id": 3, "name": "手机已放弃", "status": 2},
        ]

        def fake_cloud_request(_config, route, timeout=12):
            data = {
                "/info": {},
                "/tasks": cloud_tasks,
                "/items": [],
                "/achievement_categories": [],
                "/coin": {"coin": 0},
                "/skills": [],
            }[route]
            return {"route": route, "base_url": "http://phone", "data": data}

        with patch.object(server, "cloud_category_map", return_value={}), patch.object(
            server, "cloud_request", side_effect=fake_cloud_request
        ):
            done_tasks = self.get_json("/api/tasks?source=cloud&filter=done")
            overview = server.cloud_dashboard_overview()

        self.assertEqual([row["id"] for row in done_tasks], [2])
        self.assertEqual(overview["tasks"]["total"], 3)
        self.assertEqual(overview["tasks"]["active"], 1)
        self.assertEqual(overview["tasks"]["done"], 1)


if __name__ == "__main__":
    unittest.main()
