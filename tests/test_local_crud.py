import json
import os
from pathlib import Path

import server

from fixtures import LocalCrudApiTestCase, TemporaryLocalDatabase


class FixtureIsolationTests(LocalCrudApiTestCase):
    def test_fixture_uses_only_disposable_database_and_removes_it(self):
        project_root = Path(__file__).resolve().parents[1]
        original_backup = (project_root.parent / "LifeupBackup.zip").resolve()
        fixture_database = Path(self.fixture.db_path).resolve()

        self.assertNotEqual(fixture_database, original_backup)
        self.assertTrue(fixture_database.is_relative_to(Path(self.fixture.root).resolve()))
        self.assertIsNone(server.STATE["backup_path"])

        nested = TemporaryLocalDatabase()
        nested_root = nested.root
        nested_database = nested.db_path
        self.assertTrue(os.path.exists(nested_database))
        nested.cleanup()
        self.assertFalse(os.path.exists(nested_root))


class TaskCrudRegressionTests(LocalCrudApiTestCase):
    def test_create_persists_primary_reward_and_relationship_fields(self):
        task_id = self.create_task()

        connection = self.connect()
        try:
            task = connection.execute(
                """
                SELECT content, taskfrequency, rewardcoin, expreward, rewardcoinvariable,
                       tasktargetid, categoryid, isdeleterecord, tasktype,
                       enableebbinghausmode, ishandleoverdue, extrainfo
                FROM taskmodel WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT targettimes FROM tasktargetmodel WHERE id=?",
                (task["tasktargetid"],),
            ).fetchone()
            skills = connection.execute(
                "SELECT skillids FROM taskmodel_skillids WHERE taskmodel_id=?",
                (task_id,),
            ).fetchall()
            rewards = connection.execute(
                "SELECT shopitemmodelid, amount FROM taskrewardmodel WHERE taskmodelid=?",
                (task_id,),
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(task["content"], "每日修炼")
        self.assertEqual(task["taskfrequency"], 1)
        self.assertEqual((task["rewardcoin"], task["expreward"], task["rewardcoinvariable"]), (21, 34, 5))
        self.assertEqual((task["categoryid"], task["tasktype"]), (5, 1))
        self.assertEqual((task["enableebbinghausmode"], task["ishandleoverdue"]), (1, 1))
        self.assertEqual(task["isdeleterecord"], 0)
        self.assertEqual(target["targettimes"], 7)
        self.assertEqual([row["skillids"] for row in skills], [10])
        self.assertEqual([(row["shopitemmodelid"], row["amount"]) for row in rewards], [(101, 2)])
        self.assertEqual(
            json.loads(task["extrainfo"]),
            {
                "autoUseItems": True,
                "coinPunishmentFactor": 0.25,
                "expPunishmentFactor": 0.5,
                "t_f_m": 1,
                "writeFeelings": False,
            },
        )

    def test_read_uses_tasktarget_relationship_and_returns_rewards(self):
        task_id = self.create_task(target_count=13)
        self.assertNotEqual(task_id, 41, "夹具必须让任务 ID 与目标 ID 不同")

        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200, response.get_json())
        tasks = response.get_json()
        task = next(row for row in tasks if row["id"] == task_id)

        self.assertEqual(task["target_count"], 13)
        self.assertEqual(task["skill_ids"], [10])
        self.assertEqual(task["item_rewards"], [{"id": 1, "item_id": 101, "amount": 2}])
        self.assertEqual(task["extrainfo_obj"]["coinPunishmentFactor"], 0.25)

    def test_update_changes_main_fields_and_preserves_unknown_metadata_and_rewards(self):
        task_id = self.create_task()
        connection = self.connect()
        try:
            metadata = json.loads(
                connection.execute(
                    "SELECT extrainfo FROM taskmodel WHERE id=?", (task_id,)
                ).fetchone()["extrainfo"]
            )
            metadata["futureTaskMetadata"] = {"opaque": [1, 2, 3]}
            connection.execute(
                "UPDATE taskmodel SET extrainfo=? WHERE id=?",
                (json.dumps(metadata), task_id),
            )
            connection.commit()
        finally:
            connection.close()

        self.post_json(
            "/api/tasks/update",
            {
                "id": task_id,
                "title": "每周复盘",
                "frequency": 2,
                "target_count": 9,
                "coin": 55,
                "exp": 89,
                "rewardcoinvariable": 8,
                "note": "更新备注",
                "category_id": 6,
                "difficulty": 3,
                "tagcolor": 4,
                "priority": 2,
                "tasktype": 1,
                "coin_punishment_factor": 0.75,
                "skill_ids": [11],
            },
        )

        connection = self.connect()
        try:
            task = connection.execute(
                """
                SELECT content, taskfrequency, rewardcoin, expreward, rewardcoinvariable,
                       remark, categoryid, taskdifficultydegree, taskurgencydegree,
                       tasktype, tasktargetid, extrainfo
                FROM taskmodel WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            target_count = connection.execute(
                "SELECT targettimes FROM tasktargetmodel WHERE id=?",
                (task["tasktargetid"],),
            ).fetchone()["targettimes"]
            skill_ids = [
                row["skillids"]
                for row in connection.execute(
                    "SELECT skillids FROM taskmodel_skillids WHERE taskmodel_id=?",
                    (task_id,),
                ).fetchall()
            ]
            rewards = connection.execute(
                "SELECT shopitemmodelid, amount FROM taskrewardmodel WHERE taskmodelid=?",
                (task_id,),
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual((task["content"], task["taskfrequency"]), ("每周复盘", 2))
        self.assertEqual((task["rewardcoin"], task["expreward"], task["rewardcoinvariable"]), (55, 89, 8))
        self.assertEqual((task["categoryid"], task["taskdifficultydegree"], task["taskurgencydegree"]), (6, 3, 2))
        self.assertEqual(target_count, 9)
        self.assertEqual(skill_ids, [11])
        self.assertEqual([(row["shopitemmodelid"], row["amount"]) for row in rewards], [(101, 2)])
        metadata = json.loads(task["extrainfo"])
        self.assertEqual(metadata["coinPunishmentFactor"], 0.75)
        self.assertEqual(metadata["futureTaskMetadata"], {"opaque": [1, 2, 3]})

    def test_partial_freeze_preserves_main_and_reward_fields(self):
        task_id = self.create_task()

        self.post_json("/api/tasks/update", {"id": task_id, "isfrozen": True})

        connection = self.connect()
        try:
            task = connection.execute(
                """
                SELECT content, taskfrequency, rewardcoin, expreward,
                       rewardcoinvariable, categoryid, tasktype, isfrozen, extrainfo
                FROM taskmodel WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            reward_count = connection.execute(
                "SELECT COUNT(*) FROM taskrewardmodel WHERE taskmodelid=?",
                (task_id,),
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(task["isfrozen"], 1)
        self.assertEqual((task["content"], task["taskfrequency"]), ("每日修炼", 1))
        self.assertEqual((task["rewardcoin"], task["expreward"], task["rewardcoinvariable"]), (21, 34, 5))
        self.assertEqual((task["categoryid"], task["tasktype"]), (5, 1))
        self.assertEqual(json.loads(task["extrainfo"])["coinPunishmentFactor"], 0.25)
        self.assertEqual(reward_count, 1)

    def test_delete_is_soft_and_keeps_relationship_records(self):
        task_id = self.create_task()

        self.post_json("/api/tasks/delete", {"id": task_id})
        response = self.client.get("/api/tasks?show_frozen=1")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(task_id, [task["id"] for task in response.get_json()])

        connection = self.connect()
        try:
            task = connection.execute(
                "SELECT isdeleterecord, tasktargetid FROM taskmodel WHERE id=?",
                (task_id,),
            ).fetchone()
            target_count = connection.execute(
                "SELECT COUNT(*) FROM tasktargetmodel WHERE id=?",
                (task["tasktargetid"],),
            ).fetchone()[0]
            reward_count = connection.execute(
                "SELECT COUNT(*) FROM taskrewardmodel WHERE taskmodelid=?",
                (task_id,),
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(task["isdeleterecord"], 1)
        self.assertEqual((target_count, reward_count), (1, 1))


class ItemCrudRegressionTests(LocalCrudApiTestCase):
    def test_create_persists_primary_inventory_effect_and_unknown_metadata(self):
        item_id = self.create_item()

        connection = self.connect()
        try:
            item = connection.execute(
                """
                SELECT itemname, price, icon, description, stocknumber, shopcategoryid,
                       isdel, isdisablepurchase, inventorymodel_id,
                       customusebuttontext, purchaselimits, extrainfo
                FROM shopitemmodel WHERE id=?
                """,
                (item_id,),
            ).fetchone()
            inventory = connection.execute(
                "SELECT stocknumber FROM inventorymodel WHERE id=?",
                (item["inventorymodel_id"],),
            ).fetchone()
            effect = connection.execute(
                "SELECT goodseffecttype, values_lpcolumn FROM goodseffectmodel WHERE shopitemid=?",
                (item_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual((item["itemname"], item["price"], item["shopcategoryid"]), ("元数据宝箱", 120, 2))
        self.assertEqual((item["stocknumber"], inventory["stocknumber"]), (-1, -1))
        self.assertEqual((item["isdel"], item["isdisablepurchase"]), (0, 0))
        self.assertEqual(item["customusebuttontext"], "开启")
        self.assertEqual(json.loads(item["purchaselimits"]), [{"type": "daily", "value": 1}])
        self.assertEqual(json.loads(item["extrainfo"])["futureFeature"]["version"], 3)
        self.assertEqual((effect["goodseffecttype"], effect["values_lpcolumn"]), (2, 9))

    def test_read_returns_primary_fields_and_unknown_metadata(self):
        item_id = self.create_item()

        response = self.client.get("/api/items")
        self.assertEqual(response.status_code, 200, response.get_json())
        item = next(row for row in response.get_json() if row["id"] == item_id)

        self.assertEqual((item["name"], item["price"], item["category_name"]), ("元数据宝箱", 120, "道具"))
        self.assertEqual(item["inventory_count"], -1)
        self.assertEqual(json.loads(item["extrainfo"])["futureFeature"]["enabled"], True)

    def test_update_changes_primary_fields_without_overwriting_metadata_or_effects(self):
        item_id = self.create_item()

        self.post_json(
            "/api/items/update",
            {
                "id": item_id,
                "name": "元数据宝箱·改",
                "price": 233,
                "icon": "chest-v2.png",
                "description": "更新描述",
                "count": 6,
                "category_id": 3,
                "isdisablepurchase": 1,
                "customusebuttontext": "兑换",
                "purchaselimits": [{"type": "total", "value": 2}],
                "extrainfo": {"futureFeature": "不应覆盖"},
            },
        )

        connection = self.connect()
        try:
            item = connection.execute(
                """
                SELECT itemname, price, description, stocknumber, shopcategoryid,
                       isdisablepurchase, customusebuttontext, purchaselimits, extrainfo
                FROM shopitemmodel WHERE id=?
                """,
                (item_id,),
            ).fetchone()
            effect_count = connection.execute(
                "SELECT COUNT(*) FROM goodseffectmodel WHERE shopitemid=?",
                (item_id,),
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual((item["itemname"], item["price"], item["stocknumber"]), ("元数据宝箱·改", 233, 6))
        self.assertEqual((item["shopcategoryid"], item["isdisablepurchase"]), (3, 1))
        self.assertEqual(item["customusebuttontext"], "兑换")
        self.assertEqual(json.loads(item["purchaselimits"]), [{"type": "total", "value": 2}])
        self.assertEqual(json.loads(item["extrainfo"])["futureFeature"], {"enabled": True, "version": 3})
        self.assertEqual(effect_count, 1)

    def test_delete_is_soft_for_item_record(self):
        item_id = self.create_item()

        self.post_json("/api/items/delete", {"id": item_id})
        response = self.client.get("/api/items")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(item_id, [item["id"] for item in response.get_json()])

        connection = self.connect()
        try:
            item = connection.execute(
                "SELECT isdel, extrainfo FROM shopitemmodel WHERE id=?", (item_id,)
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(item["isdel"], 1)
        self.assertEqual(json.loads(item["extrainfo"])["futureFeature"]["version"], 3)


class AchievementCrudRegressionTests(LocalCrudApiTestCase):
    def test_create_persists_primary_fields_and_type(self):
        achievement_id = self.create_achievement()

        connection = self.connect()
        try:
            achievement = connection.execute(
                """
                SELECT content, description, type, categoryid, rewardcoin, expreward,
                       icon, achievementstatus, isdelete
                FROM userachievementmodel WHERE id=?
                """,
                (achievement_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual((achievement["content"], achievement["description"]), ("筑基里程碑", "完成筑基"))
        self.assertEqual((achievement["type"], achievement["categoryid"]), (3, 7))
        self.assertEqual((achievement["rewardcoin"], achievement["expreward"]), (88, 144))
        self.assertEqual((achievement["achievementstatus"], achievement["isdelete"]), (0, 0))

    def test_read_returns_primary_fields_and_type(self):
        achievement_id = self.create_achievement()

        response = self.client.get("/api/achievements")
        self.assertEqual(response.status_code, 200, response.get_json())
        achievement = next(row for row in response.get_json() if row["id"] == achievement_id)

        self.assertEqual((achievement["name"], achievement["type"]), ("筑基里程碑", 3))
        self.assertEqual((achievement["coin"], achievement["exp"]), (88, 144))
        self.assertEqual(achievement["category_name"], "里程碑")

    def test_update_from_ui_preserves_existing_type_when_type_is_omitted(self):
        achievement_id = self.create_achievement(type=3)

        self.post_json(
            "/api/achievements/update",
            {
                "id": achievement_id,
                "name": "金丹里程碑",
                "description": "完成金丹",
                "category_id": 8,
                "coin": 233,
                "exp": 377,
                "icon": "golden-core.png",
            },
        )

        connection = self.connect()
        try:
            achievement = connection.execute(
                """
                SELECT content, description, type, categoryid, rewardcoin, expreward, icon
                FROM userachievementmodel WHERE id=?
                """,
                (achievement_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual((achievement["content"], achievement["description"]), ("金丹里程碑", "完成金丹"))
        self.assertEqual(achievement["type"], 3)
        self.assertEqual((achievement["categoryid"], achievement["rewardcoin"], achievement["expreward"]), (8, 233, 377))

    def test_delete_is_soft_for_achievement_record(self):
        achievement_id = self.create_achievement()

        self.post_json("/api/achievements/delete", {"id": achievement_id})
        response = self.client.get("/api/achievements")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(achievement_id, [row["id"] for row in response.get_json()])

        connection = self.connect()
        try:
            achievement = connection.execute(
                "SELECT isdelete, type FROM userachievementmodel WHERE id=?",
                (achievement_id,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual((achievement["isdelete"], achievement["type"]), (1, 3))
