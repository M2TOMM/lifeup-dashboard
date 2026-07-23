import os
import shutil
import sqlite3
import tempfile
import unittest

import server


SCHEMA_SQL = """
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
CREATE TABLE categorymodel (
    id INTEGER PRIMARY KEY,
    categoryname TEXT,
    isdelete INTEGER,
    categorytype INTEGER,
    orderincategory INTEGER
);
CREATE TABLE skillmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    isdel INTEGER
);

CREATE TABLE inventorymodel (
    id INTEGER PRIMARY KEY,
    createtime INTEGER,
    stocknumber INTEGER,
    updatetime INTEGER,
    isstarred INTEGER
);
CREATE TABLE shopitemmodel (
    id INTEGER PRIMARY KEY,
    itemname TEXT,
    price INTEGER,
    icon TEXT,
    description TEXT,
    stocknumber INTEGER,
    shopcategoryid INTEGER,
    createtime INTEGER,
    isdel INTEGER,
    isdisablepurchase INTEGER,
    inventorymodel_id INTEGER,
    remoteismine INTEGER,
    customusebuttontext TEXT,
    purchaselimits TEXT,
    extrainfo TEXT,
    orderincategory INTEGER
);
CREATE TABLE shopcategorymodel (
    id INTEGER PRIMARY KEY,
    categoryname TEXT,
    isdelete INTEGER,
    orderincategory INTEGER
);
CREATE TABLE goodseffectmodel (
    id INTEGER PRIMARY KEY,
    createtime INTEGER,
    shopitemid INTEGER,
    goodseffecttype INTEGER,
    relatedinfos TEXT,
    isdel INTEGER,
    updatetime INTEGER,
    relatedid INTEGER,
    values_lpcolumn INTEGER
);

CREATE TABLE userachievementmodel (
    id INTEGER PRIMARY KEY,
    content TEXT,
    description TEXT,
    type INTEGER,
    categoryid INTEGER,
    rewardcoin INTEGER,
    expreward INTEGER,
    icon TEXT,
    achievementstatus INTEGER,
    currentvalue INTEGER,
    progress INTEGER,
    createtime INTEGER,
    finishtime INTEGER,
    updatetime INTEGER,
    isdelete INTEGER,
    isgotreward INTEGER,
    targetcompletetime INTEGER,
    rewardcoinvariable INTEGER,
    orderincategory INTEGER
);
CREATE TABLE userachcategorymodel (
    id INTEGER PRIMARY KEY,
    categoryname TEXT,
    isdelete INTEGER,
    orderincategory INTEGER
);
"""


SEED_SQL = """
INSERT INTO tasktargetmodel
    (id, targettimes, extraexpreward, repeatendinclusive, repeatendmode, repeatendbehavior)
VALUES (40, 99, 0, 1, 0, 0);
INSERT INTO categorymodel
    (id, categoryname, isdelete, categorytype, orderincategory)
VALUES (5, '修炼', 0, 0, 0), (6, '生活', 0, 0, 1);
INSERT INTO skillmodel (id, content, isdel)
VALUES (10, '体魄', 0), (11, '心境', 0);

INSERT INTO inventorymodel (id, createtime, stocknumber, updatetime, isstarred)
VALUES (501, 1000, 3, 1000, 0);
INSERT INTO shopitemmodel (
    id, itemname, price, icon, description, stocknumber, shopcategoryid,
    createtime, isdel, isdisablepurchase, inventorymodel_id, remoteismine,
    customusebuttontext, purchaselimits, extrainfo, orderincategory
) VALUES (
    101, '奖励种子', 10, '', '任务奖励夹具', -1, 2,
    1000, 0, 0, 501, 0, '', '[]', '{"seed":true}', 0
);
INSERT INTO shopcategorymodel (id, categoryname, isdelete, orderincategory)
VALUES (2, '道具', 0, 0), (3, '奖励', 0, 1);

INSERT INTO userachcategorymodel (id, categoryname, isdelete, orderincategory)
VALUES (7, '里程碑', 0, 0), (8, '挑战', 0, 1);
"""


class TemporaryLocalDatabase:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="lifeup-local-crud-")
        self.db_path = os.path.join(self.root, "LifeUpDB.db")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executescript(SEED_SQL)
            connection.commit()
        finally:
            connection.close()

    def connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class LocalCrudApiTestCase(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.fixture = TemporaryLocalDatabase()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self.fixture.db_path,
                "tmpdir": self.fixture.root,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        root = self.fixture.root
        server.STATE.clear()
        server.STATE.update(self._old_state)
        self.fixture.cleanup()
        self.assertFalse(os.path.exists(root), "临时 CRUD 数据库目录没有清理")

    def connect(self):
        return self.fixture.connect()

    def post_json(self, path, payload, expected_status=200):
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def create_task(self, **overrides):
        payload = {
            "title": "每日修炼",
            "frequency": 1,
            "target_count": 7,
            "coin": 21,
            "exp": 34,
            "note": "初始备注",
            "category_id": 5,
            "difficulty": 2,
            "tagcolor": 3,
            "priority": 4,
            "tasktype": 1,
            "rewardcoinvariable": 5,
            "coin_punishment_factor": 0.25,
            "exp_punishment_factor": 0.5,
            "auto_use_items": True,
            "enable_ebbinghaus": 1,
            "ishandleoverdue": 1,
            "skill_ids": [10],
            "item_rewards": [{"item_id": 101, "amount": 2}],
        }
        payload.update(overrides)
        result = self.post_json("/api/tasks/add", payload)
        return result["id"]

    def create_item(self, **overrides):
        payload = {
            "name": "元数据宝箱",
            "price": 120,
            "icon": "chest.png",
            "description": "初始描述",
            "count": -1,
            "category_id": 2,
            "isdisablepurchase": 0,
            "customusebuttontext": "开启",
            "purchaselimits": [{"type": "daily", "value": 1}],
            "extrainfo": {
                "futureFeature": {"enabled": True, "version": 3},
                "limitScope": 0,
            },
            "effects": [{"type": "coin", "value": 9}],
        }
        payload.update(overrides)
        result = self.post_json("/api/items/add", payload)
        return result["id"]

    def create_achievement(self, **overrides):
        payload = {
            "name": "筑基里程碑",
            "description": "完成筑基",
            "type": 3,
            "category_id": 7,
            "coin": 88,
            "exp": 144,
            "icon": "foundation.png",
        }
        payload.update(overrides)
        result = self.post_json("/api/achievements/add", payload)
        return result["id"]
