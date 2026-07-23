import gc
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
import warnings

import server
from tests.fixtures import SCHEMA_SQL, SEED_SQL


ROOT = Path(__file__).resolve().parents[1]


class TaskBatchImportApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-task-import-")
        self.workspace = os.path.join(self.root, "workspace")
        self.database = os.path.join(self.workspace, "databases", "LifeUpDB.db")
        self.snapshot_dir = os.path.join(self.root, "snapshots")
        os.makedirs(os.path.dirname(self.database), exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executescript(SEED_SQL)
            connection.commit()
        finally:
            connection.close()
        self.snapshot_patch = mock.patch.object(
            server, "SNAPSHOT_DIR", self.snapshot_dir
        )
        self.snapshot_patch.start()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self.database,
                "tmpdir": self.workspace,
                "loaded": True,
            }
        )
        server.LOCAL_BATCH_PREVIEWS.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.LOCAL_BATCH_PREVIEWS.clear()
        server.STATE.clear()
        server.STATE.update(self._old_state)
        self.snapshot_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时任务导入目录没有清理")

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def upload(self, name, content, expected_status=200, headers=None):
        stream = io.BytesIO(content)
        response = None
        try:
            response = self.client.post(
                "/api/local/task-import-files",
                data={"file": (stream, name)},
                content_type="multipart/form-data",
                headers=headers,
            )
            body = response.get_json()
            self.assertEqual(response.status_code, expected_status, body)
            return body
        finally:
            stream.close()
            if response is not None:
                response.request.input_stream.close()
                response.close()

    def preview(self, rows, expected_status=201, headers=None):
        response = self.client.post(
            "/api/local/batch-previews",
            json={"entity": "tasks", "rows": rows},
            headers=headers,
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def execute(self, preview, expected_status=200):
        response = self.client.post(
            f"/api/local/batch-previews/{preview['preview_token']}/executions",
            json={"digest": preview["digest"]},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def test_templates_and_utf8_csv_and_json_file_parsing(self):
        csv_response = self.client.get("/api/local/task-import-templates/csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", csv_response.content_type)
        self.assertIn(
            "title,category,frequency,target_count,priority,difficulty,skills,coin,exp,note,item_rewards,is_frozen,duplicate_policy",
            csv_response.data.decode("utf-8-sig"),
        )

        json_response = self.client.get("/api/local/task-import-templates/json")
        self.assertEqual(json_response.status_code, 200)
        json_template = json.loads(json_response.data.decode("utf-8-sig"))
        self.assertIsInstance(json_template, list)
        self.assertEqual(
            json_template[0]["item_rewards"],
            [{"name": "🎁诸天系统·绑定礼包", "amount": 1}],
        )

        csv_body = (
            "\ufefftitle,category,frequency,target_count,coin,exp,skills,is_frozen,duplicate_policy\r\n"
            '"复盘,记录",生活,daily,2,10,5,心境,否,create\r\n'
        ).encode("utf-8")
        parsed_csv = self.upload("tasks.csv", csv_body)
        self.assertEqual(parsed_csv["format"], "csv")
        self.assertEqual(parsed_csv["rows"][0]["line"], 2)
        self.assertEqual(parsed_csv["rows"][0]["data"]["title"], "复盘,记录")

        parsed_json = self.upload(
            "tasks.json",
            json.dumps(
                [
                    {
                        "title": "阅读",
                        "category": "修炼",
                        "skills": ["体魄", "心境"],
                    }
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        self.assertEqual(parsed_json["format"], "json")
        self.assertEqual(parsed_json["rows"][0]["line"], 1)
        self.assertEqual(parsed_json["rows"][0]["data"]["skills"], ["体魄", "心境"])

    def test_legacy_csv_columns_remain_supported(self):
        legacy_csv = (
            "\ufefftitle,category,frequency,target_count,coin,exp,skills,is_frozen,duplicate_policy\r\n"
            "旧模板任务,修炼,每日,1,2,3,体魄,否,create\r\n"
        ).encode("utf-8")
        parsed = self.upload("legacy.csv", legacy_csv)
        preview = self.preview(parsed["rows"])
        self.assertTrue(preview["can_execute"])
        self.assertEqual(preview["rows"][0]["normalized_data"]["priority"], 1)
        self.assertEqual(preview["rows"][0]["normalized_data"]["difficulty"], 1)
        self.assertEqual(preview["rows"][0]["normalized_data"]["note"], "")
        self.assertEqual(preview["rows"][0]["normalized_data"]["item_rewards"], [])

    def test_upload_rejections_have_stable_codes(self):
        missing = self.client.post(
            "/api/local/task-import-files",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["code"], "TASK_IMPORT_FILE_REQUIRED")
        cases = (
            ("tasks.txt", b"title", "TASK_IMPORT_UNSUPPORTED_FORMAT"),
            ("tasks.csv", b"\xff\xfeinvalid", "TASK_IMPORT_INVALID_ENCODING"),
            (
                "tasks.csv",
                b'title,category,frequency,target_count,coin,exp,skills,is_frozen,duplicate_policy\r\n"unterminated',
                "TASK_IMPORT_INVALID_CSV",
            ),
            ("tasks.json", b"{not-json", "TASK_IMPORT_INVALID_JSON"),
            ("tasks.json", b" " * (1024 * 1024 + 1), "TASK_IMPORT_FILE_TOO_LARGE"),
            (
                "tasks.csv",
                b"title,category,unknown\r\nA,B,C\r\n",
                "TASK_IMPORT_INVALID_COLUMNS",
            ),
            ("tasks.json", b"[]", "TASK_IMPORT_INVALID_ROWS"),
            (
                "tasks.json",
                json.dumps([{"title": str(i)} for i in range(201)]).encode(),
                "TASK_IMPORT_INVALID_ROWS",
            ),
        )
        for name, content, code in cases:
            with self.subTest(code=code):
                body = self.upload(name, content, expected_status=400)
                self.assertEqual(body["ok"], False)
                self.assertEqual(body["code"], code)

    def test_oversized_upload_closes_multipart_temporary_stream(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            body = self.upload(
                "tasks.json",
                b" " * (1024 * 1024 + 1),
                expected_status=400,
            )
            gc.collect()

        self.assertEqual(body["code"], "TASK_IMPORT_FILE_TOO_LARGE")
        self.assertFalse(
            [warning for warning in caught if warning.category is ResourceWarning]
        )

    def test_ambiguous_names_and_field_boundaries_are_row_errors(self):
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO categorymodel (id, categoryname, isdelete, categorytype, orderincategory) "
                "VALUES (7, '生活', 0, 0, 2)"
            )
            connection.execute(
                "INSERT INTO skillmodel (id, content, isdel) VALUES (12, '心境', 0)"
            )
            connection.commit()
        finally:
            connection.close()

        ambiguous = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "title": "名称歧义",
                        "category": "生活",
                        "skills": "心境",
                    },
                }
            ]
        )
        self.assertTrue(
            {"CATEGORY_AMBIGUOUS", "SKILL_AMBIGUOUS"}.issubset(
                {error["code"] for error in ambiguous["rows"][0]["errors"]}
            )
        )

        invalid = self.preview(
            [
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "title": " ",
                        "category": "修炼",
                        "skills": "体魄|体魄|心境|第四项",
                        "duplicate_policy": "later",
                    },
                }
            ]
        )
        self.assertTrue(
            {"INVALID_TITLE", "INVALID_SKILLS", "INVALID_DUPLICATE_POLICY"}.issubset(
                {error["code"] for error in invalid["rows"][0]["errors"]}
            )
        )

    def test_preview_normalizes_names_values_and_reports_mapping_errors(self):
        ready = self.preview(
            [
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "title": " 晨间复盘 ",
                        "category": "生活",
                        "frequency": "每日",
                        "target_count": "2",
                        "coin": "10",
                        "exp": "5",
                        "skills": "心境|体魄",
                        "is_frozen": "是",
                        "duplicate_policy": "create",
                    },
                }
            ]
        )
        self.assertTrue(ready["can_execute"])
        self.assertEqual(
            ready["rows"][0]["normalized_data"],
            {
                "title": "晨间复盘",
                "category": "生活",
                "category_id": 6,
                "frequency": 1,
                "target_count": 2,
                "coin": 10,
                "exp": 5,
                "skills": ["心境", "体魄"],
                "skill_ids": [11, 10],
                "is_frozen": True,
                "priority": 1,
                "difficulty": 1,
                "note": "",
                "item_rewards": [],
                "attr1": "心境",
                "attr2": "体魄",
                "attr3": "",
                "duplicate_policy": "create",
            },
        )

        invalid = self.preview(
            [
                {
                    "line": 3,
                    "action": "create",
                    "data": {
                        "title": "边界错误",
                        "category": "不存在",
                        "frequency": "每小时",
                        "target_count": 0,
                        "coin": -1,
                        "exp": True,
                        "skills": "不存在",
                        "is_frozen": "也许",
                    },
                }
            ]
        )
        codes = {error["code"] for error in invalid["rows"][0]["errors"]}
        self.assertTrue(
            {
                "CATEGORY_NOT_FOUND",
                "INVALID_FREQUENCY",
                "INVALID_TARGET_COUNT",
                "INVALID_COIN",
                "INVALID_EXP",
                "SKILL_NOT_FOUND",
                "INVALID_FROZEN_STATE",
            }.issubset(codes)
        )

    def test_complete_fields_normalize_and_execute_in_one_step(self):
        parsed = self.upload(
            "complete.json",
            json.dumps(
                [
                    {
                        "title": "完整字段任务",
                        "category": "修炼",
                        "frequency": "单次",
                        "target_count": 3,
                        "priority": 4,
                        "difficulty": 3,
                        "skills": ["体魄", "心境"],
                        "coin": 88,
                        "exp": 66,
                        "note": "完成后领取商品奖励",
                        "item_rewards": [{"name": "奖励种子", "amount": 2}],
                        "is_frozen": True,
                        "duplicate_policy": "create",
                    }
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        preview = self.preview(parsed["rows"])
        self.assertTrue(preview["can_execute"])
        normalized = preview["rows"][0]["normalized_data"]
        self.assertEqual(normalized["priority"], 4)
        self.assertEqual(normalized["difficulty"], 3)
        self.assertEqual(
            (normalized["attr1"], normalized["attr2"], normalized["attr3"]),
            ("体魄", "心境", ""),
        )
        self.assertEqual(
            normalized["item_rewards"],
            [{"item_id": 101, "name": "奖励种子", "amount": 2}],
        )
        result = self.execute(preview)
        self.assertEqual(result["summary"]["affected"], 1)

        connection = self.connect()
        try:
            task = connection.execute(
                "SELECT id, remark, taskurgencydegree, taskdifficultydegree, "
                "relatedattribute1, relatedattribute2, relatedattribute3 "
                "FROM taskmodel WHERE content='完整字段任务'"
            ).fetchone()
            self.assertIsNotNone(task)
            self.assertEqual(tuple(task)[1:], ("完成后领取商品奖励", 4, 3, "体魄", "心境", ""))
            rewards = connection.execute(
                "SELECT shopitemmodelid, amount FROM taskrewardmodel WHERE taskmodelid=?",
                (task[0],),
            ).fetchall()
            self.assertEqual([tuple(row) for row in rewards], [(101, 2)])
        finally:
            connection.close()

    def test_complete_field_validation_reports_stable_errors(self):
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO shopitemmodel "
                "(id, itemname, price, icon, description, stocknumber, shopcategoryid, "
                "createtime, isdel, isdisablepurchase, inventorymodel_id, remoteismine, "
                "customusebuttontext, purchaselimits, extrainfo, orderincategory) "
                "SELECT 102, itemname, price, icon, description, stocknumber, shopcategoryid, "
                "createtime, isdel, isdisablepurchase, inventorymodel_id, remoteismine, "
                "customusebuttontext, purchaselimits, extrainfo, orderincategory "
                "FROM shopitemmodel WHERE id=101"
            )
            connection.commit()
        finally:
            connection.close()

        invalid = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "title": "完整字段错误",
                        "category": "修炼",
                        "priority": 0,
                        "difficulty": 5,
                        "note": "x" * 2001,
                        "item_rewards": "奖励种子*0|不存在商品*1",
                    },
                }
            ]
        )
        codes = {error["code"] for error in invalid["rows"][0]["errors"]}
        self.assertTrue(
            {
                "INVALID_PRIORITY",
                "INVALID_DIFFICULTY",
                "INVALID_NOTE",
                "INVALID_ITEM_REWARD_AMOUNT",
                "ITEM_REWARD_AMBIGUOUS",
                "ITEM_REWARD_NOT_FOUND",
            }.issubset(codes)
        )

    def test_existing_and_file_duplicates_require_a_row_policy(self):
        existing_id = self.client.post(
            "/api/tasks/add",
            json={"title": "已有任务", "category_id": 5},
        ).get_json()["id"]
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"title": "已有任务", "category": "修炼"},
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {"title": "文件重复", "category": "生活"},
                },
                {
                    "line": 5,
                    "action": "create",
                    "data": {"title": " 文件重复 ", "category": "生活"},
                },
            ]
        )
        self.assertFalse(preview["can_execute"])
        self.assertEqual(preview["summary"]["duplicates"], 3)
        self.assertEqual(
            preview["rows"][0]["duplicate"]["existing_task_ids"], [existing_id]
        )
        self.assertEqual(preview["rows"][1]["duplicate"]["import_lines"], [2, 5])
        for row in preview["rows"]:
            self.assertIn(
                "DUPLICATE_POLICY_REQUIRED",
                {error["code"] for error in row["errors"]},
            )

    def test_skip_and_create_execute_in_one_transaction_with_real_affected_count(self):
        existing_id = self.client.post(
            "/api/tasks/add",
            json={"title": "重复任务", "category_id": 5},
        ).get_json()["id"]
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "title": "重复任务",
                        "category": "修炼",
                        "duplicate_policy": "skip",
                    },
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "title": "新任务",
                        "category": "生活",
                        "frequency": "weekly",
                        "target_count": 7,
                        "coin": 3,
                        "exp": 4,
                        "skills": ["心境"],
                        "is_frozen": 1,
                    },
                },
            ]
        )
        result = self.execute(preview)
        self.assertEqual(
            result["summary"],
            {"total": 2, "succeeded": 2, "failed": 0, "affected": 1},
        )
        self.assertEqual(
            result["rows"][0]["result"],
            {"affected": 0, "skipped": True, "reason": "duplicate"},
        )
        self.assertEqual(result["rows"][1]["result"], {"affected": 1})

        connection = self.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM taskmodel WHERE content='重复任务' AND isdeleterecord=0"
                ).fetchone()[0],
                1,
            )
            created = connection.execute(
                "SELECT id, taskfrequency, rewardcoin, expreward, categoryid, isfrozen, tasktargetid "
                "FROM taskmodel WHERE content='新任务'"
            ).fetchone()
            self.assertIsNotNone(created)
            self.assertEqual(tuple(created)[1:6], (2, 3, 4, 6, 1))
            self.assertEqual(
                connection.execute(
                    "SELECT targettimes FROM tasktargetmodel WHERE id=?",
                    (created[6],),
                ).fetchone()[0],
                7,
            )
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT skillids FROM taskmodel_skillids WHERE taskmodel_id=?",
                        (created[0],),
                    ).fetchall()
                ],
                [(11,)],
            )
            self.assertIsNotNone(existing_id)
        finally:
            connection.close()
        self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)

    def test_database_failure_rolls_back_all_created_tasks(self):
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"title": "先创建", "category": "修炼"},
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {"title": "触发失败", "category": "生活"},
                },
            ]
        )
        connection = self.connect()
        try:
            before_targets = connection.execute(
                "SELECT COUNT(*) FROM tasktargetmodel"
            ).fetchone()[0]
            connection.executescript(
                """
                CREATE TRIGGER fail_task_import
                BEFORE INSERT ON taskmodel
                WHEN NEW.content = '触发失败'
                BEGIN
                    SELECT RAISE(ABORT, 'do not leak this detail');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()

        failed = self.execute(preview, expected_status=500)
        self.assertEqual(failed["code"], "BATCH_EXECUTION_FAILED")
        self.assertNotIn("do not leak", json.dumps(failed, ensure_ascii=False))
        connection = self.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM taskmodel WHERE content IN ('先创建','触发失败')"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM tasktargetmodel").fetchone()[0],
                before_targets,
            )
        finally:
            connection.close()

    def test_cloud_source_cannot_upload_or_preview_task_creation(self):
        headers = {"X-LifeUp-Data-Source": "cloud"}
        blocked_upload = self.upload(
            "tasks.json",
            b"[{}]",
            expected_status=403,
            headers=headers,
        )
        self.assertEqual(blocked_upload["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE")
        blocked_preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"title": "云端禁止", "category": "修炼"},
                }
            ],
            expected_status=403,
            headers=headers,
        )
        self.assertEqual(blocked_preview["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE")


class TaskBatchImportUiContractTests(unittest.TestCase):
    def test_ui_uses_parser_and_task6_preview_execution_contract(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("CSV/JSON 批量导入", html)
        self.assertIn("/api/local/task-import-files", html)
        self.assertIn("/api/local/task-import-templates/csv", html)
        self.assertIn("/api/local/task-import-templates/json", html)
        self.assertIn("/api/local/batch-previews", html)
        self.assertIn("duplicate_policy", html)
        self.assertIn("商品名*数量|商品名*数量", html)
        self.assertIn("按选择重新预览", html)
        self.assertNotIn("innerHTML = row.data.title", html)


if __name__ == "__main__":
    unittest.main()
