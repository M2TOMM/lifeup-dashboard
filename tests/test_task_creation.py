import os
import shutil
import sqlite3
import tempfile
import unittest

import server


class TaskCreationRegressionTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self._tmpdir = tempfile.mkdtemp(prefix="lifeup-task-test-")
        self._db_path = os.path.join(self._tmpdir, "LifeUpDB.db")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE tasktargetmodel (
                    id INTEGER PRIMARY KEY,
                    targettimes INTEGER,
                    extraexpreward INTEGER,
                    repeatendinclusive INTEGER,
                    repeatendmode INTEGER,
                    repeatendbehavior INTEGER
                );
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
                    priority INTEGER,
                    tasktargetid INTEGER,
                    userid INTEGER,
                    isshared INTEGER,
                    tasktype INTEGER,
                    isneedtoremake INTEGER,
                    enableebbinghausmode INTEGER,
                    taskurgencydegree INTEGER,
                    ishandleoverdue INTEGER,
                    rewardcoinvariable INTEGER,
                    relatedattribute1 TEXT,
                    relatedattribute2 TEXT,
                    relatedattribute3 TEXT,
                    teamrecordid INTEGER,
                    teamid INTEGER,
                    taskid INTEGER,
                    taskcountextraid INTEGER,
                    lasttaskid INTEGER,
                    nexttaskid INTEGER,
                    groupid INTEGER,
                    orderincategory INTEGER,
                    isusespecificexpiretime INTEGER,
                    isuserinputstarttime INTEGER,
                    starttime INTEGER,
                    endtime INTEGER,
                    extrainfo TEXT,
                    completereward TEXT
                );
                CREATE TABLE taskmodel_skillids (
                    taskmodel_id INTEGER,
                    skillids INTEGER
                );
                CREATE TABLE taskrewardmodel (
                    id INTEGER PRIMARY KEY,
                    taskmodelid INTEGER,
                    shopitemmodelid INTEGER,
                    amount INTEGER,
                    createtime INTEGER,
                    updatetime INTEGER
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self._db_path,
                "tmpdir": self._tmpdir,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_task_persists_handle_overdue_setting(self):
        response = self.client.post(
            "/api/tasks/add",
            json={
                "title": "回归测试任务",
                "frequency": 1,
                "target_count": 1,
                "category_id": 5,
                "ishandleoverdue": 1,
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT content, ishandleoverdue FROM taskmodel"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("回归测试任务", 1))


if __name__ == "__main__":
    unittest.main()
