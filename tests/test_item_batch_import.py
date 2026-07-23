import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import server
from tests.fixtures import SCHEMA_SQL, SEED_SQL


ROOT = Path(__file__).resolve().parents[1]
ITEM_IMPORT_HEADER = (
    "action,item_id,name,category,price,stock,is_purchase_enabled,"
    "effect_type,effect_value,effect_skill,price_mode,price_value,duplicate_policy"
)


class ItemBatchImportApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-item-import-")
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
        self.assertFalse(os.path.exists(self.root), "临时商品导入目录没有清理")

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def upload(self, name, content, expected_status=200, headers=None):
        stream = io.BytesIO(content)
        response = None
        try:
            response = self.client.post(
                "/api/local/item-import-files",
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
                response.close()

    def preview(self, rows, expected_status=201, headers=None):
        response = self.client.post(
            "/api/local/batch-previews",
            json={"entity": "items", "rows": rows},
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
        csv_response = self.client.get("/api/local/item-import-templates/csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", csv_response.content_type)
        self.assertIn(ITEM_IMPORT_HEADER, csv_response.data.decode("utf-8-sig"))

        json_response = self.client.get("/api/local/item-import-templates/json")
        self.assertEqual(json_response.status_code, 200)
        self.assertIsInstance(json.loads(json_response.data.decode("utf-8-sig")), list)

        csv_body = (
            "\ufeff" + ITEM_IMPORT_HEADER + "\r\n"
            'create,,"补给,宝箱",道具,25,-1,是,coin,8,,,,create\r\n'
            "price,101,,,,,,,,,percent,10,\r\n"
        ).encode("utf-8")
        parsed_csv = self.upload("items.csv", csv_body)
        self.assertEqual(parsed_csv["format"], "csv")
        self.assertEqual(parsed_csv["rows"][0]["line"], 2)
        self.assertEqual(parsed_csv["rows"][0]["action"], "create")
        self.assertEqual(parsed_csv["rows"][0]["data"]["name"], "补给,宝箱")
        self.assertEqual(parsed_csv["rows"][1]["action"], "price")
        self.assertEqual(parsed_csv["rows"][1]["data"]["item_id"], "101")

        parsed_json = self.upload(
            "items.json",
            json.dumps(
                [
                    {
                        "action": "create",
                        "name": "修炼丹",
                        "category": "道具",
                        "effect_type": "exp",
                        "effect_value": 50,
                        "effect_skill": "体魄",
                    }
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        self.assertEqual(parsed_json["format"], "json")
        self.assertEqual(parsed_json["rows"][0]["line"], 1)
        self.assertEqual(parsed_json["rows"][0]["action"], "create")
        self.assertEqual(parsed_json["rows"][0]["data"]["effect_skill"], "体魄")

        artifact_csv = (ROOT / "outputs" / "item_import_template.csv").read_bytes()
        artifact_json = (ROOT / "outputs" / "item_import_template.json").read_bytes()
        self.assertTrue(artifact_csv.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(artifact_json.startswith(b"\xef\xbb\xbf"))
        parsed_artifact_csv = self.upload("item_import_template.csv", artifact_csv)
        parsed_artifact_json = self.upload("item_import_template.json", artifact_json)
        self.assertEqual(len(parsed_artifact_csv["rows"]), 2)
        self.assertEqual(len(parsed_artifact_json["rows"]), 2)
        self.assertEqual(parsed_artifact_csv["rows"][1]["action"], "price")

    def test_preview_normalizes_create_fields_and_price_comparison(self):
        preview = self.preview(
            [
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "name": " 修炼丹 ",
                        "category": " 道具 ",
                        "price": "25",
                        "stock": "-1",
                        "is_purchase_enabled": "是",
                        "effect_type": "exp",
                        "effect_value": "50",
                        "effect_skill": "体魄",
                    },
                },
                {
                    "line": 3,
                    "action": "price",
                    "data": {
                        "id": 101,
                        "price_mode": "percent",
                        "price_value": "50",
                    },
                },
            ]
        )

        self.assertTrue(preview["can_execute"], preview["rows"])
        self.assertEqual(
            preview["rows"][0]["normalized_data"],
            {
                "name": "修炼丹",
                "category": "道具",
                "category_id": 2,
                "price": 25,
                "stock": -1,
                "is_purchase_enabled": True,
                "isdisablepurchase": 0,
                "effect_type": "exp",
                "effect_value": 50,
                "effect_skill": "体魄",
                "effect_skill_id": 10,
                "duplicate_policy": "create",
            },
        )
        self.assertEqual(
            preview["rows"][1]["normalized_data"],
            {
                "id": 101,
                "current_price": 10,
                "price_mode": "percent",
                "price_value": 50,
                "price": 15,
            },
        )

    def test_create_executes_item_inventory_and_basic_effect_in_one_transaction(self):
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "name": "修炼丹",
                        "category": "道具",
                        "price": 25,
                        "stock": -1,
                        "is_purchase_enabled": False,
                        "effect_type": "exp",
                        "effect_value": 50,
                        "effect_skill": "体魄",
                    },
                }
            ]
        )
        result = self.execute(preview)

        self.assertEqual(
            result["summary"],
            {"total": 1, "succeeded": 1, "failed": 0, "affected": 1},
        )
        connection = self.connect()
        try:
            item = connection.execute(
                """
                SELECT id, itemname, price, stocknumber, shopcategoryid,
                       isdisablepurchase, inventorymodel_id, purchaselimits, extrainfo
                FROM shopitemmodel WHERE itemname='修炼丹' AND isdel=0
                """
            ).fetchone()
            self.assertIsNotNone(item)
            inventory = connection.execute(
                "SELECT stocknumber FROM inventorymodel WHERE id=?",
                (item["inventorymodel_id"],),
            ).fetchone()
            effect = connection.execute(
                """
                SELECT goodseffecttype, relatedinfos, values_lpcolumn
                FROM goodseffectmodel WHERE shopitemid=? AND isdel=0
                """,
                (item["id"],),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(
            tuple(item)[1:6], ("修炼丹", 25, -1, 2, 1)
        )
        self.assertEqual(inventory["stocknumber"], -1)
        self.assertEqual(json.loads(item["purchaselimits"]), [])
        self.assertEqual(json.loads(item["extrainfo"]), {})
        self.assertEqual(
            (effect["goodseffecttype"], effect["values_lpcolumn"]), (4, 50)
        )
        self.assertEqual(json.loads(effect["relatedinfos"])["skills"], [10])
        self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)

    def test_existing_and_file_duplicates_require_and_obey_row_policy(self):
        blocked = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"name": "奖励种子", "category": "道具"},
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {"name": "文件重复", "category": "道具"},
                },
                {
                    "line": 5,
                    "action": "create",
                    "data": {"name": " 文件重复 ", "category": "奖励"},
                },
            ]
        )
        self.assertFalse(blocked["can_execute"])
        self.assertEqual(blocked["summary"]["duplicates"], 3)
        self.assertEqual(
            blocked["rows"][0]["duplicate"]["existing_item_ids"], [101]
        )
        self.assertEqual(
            blocked["rows"][1]["duplicate"]["import_lines"], [2, 5]
        )
        for row in blocked["rows"]:
            self.assertIn(
                "DUPLICATE_POLICY_REQUIRED",
                {error["code"] for error in row["errors"]},
            )

        ready = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "name": "奖励种子",
                        "category": "道具",
                        "duplicate_policy": "skip",
                    },
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "name": "奖励种子",
                        "category": "奖励",
                        "duplicate_policy": "create",
                    },
                },
            ]
        )
        result = self.execute(ready)
        self.assertEqual(
            result["summary"],
            {"total": 2, "succeeded": 2, "failed": 0, "affected": 1},
        )
        self.assertEqual(
            result["rows"][0]["result"],
            {"affected": 0, "skipped": True, "reason": "duplicate"},
        )
        connection = self.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM shopitemmodel WHERE itemname='奖励种子' AND isdel=0"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)

    def test_price_execution_rejects_changed_target_and_rolls_back_other_rows(self):
        created = self.client.post(
            "/api/items/add",
            json={
                "name": "并发保护商品",
                "price": 20,
                "count": 3,
                "category_id": 2,
                "extrainfo": {"futureFeature": {"version": 9}},
                "effects": [{"type": "lootbox", "item_id": 101}],
            },
        ).get_json()["id"]
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "price",
                    "data": {"id": 101, "price_mode": "set", "price_value": 50},
                },
                {
                    "line": 2,
                    "action": "price",
                    "data": {"id": created, "price_mode": "add", "price_value": 5},
                },
            ]
        )

        connection = self.connect()
        try:
            connection.execute(
                "UPDATE shopitemmodel SET price=999 WHERE id=?", (created,)
            )
            connection.commit()
        finally:
            connection.close()

        failed = self.execute(preview, expected_status=409)
        self.assertEqual(failed["code"], "BATCH_TARGET_CHANGED")
        connection = self.connect()
        try:
            first_price = connection.execute(
                "SELECT price FROM shopitemmodel WHERE id=101"
            ).fetchone()[0]
            changed = connection.execute(
                "SELECT price, extrainfo FROM shopitemmodel WHERE id=?", (created,)
            ).fetchone()
            effect_count = connection.execute(
                "SELECT COUNT(*) FROM goodseffectmodel WHERE shopitemid=? AND isdel=0",
                (created,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(first_price, 10)
        self.assertEqual(changed["price"], 999)
        self.assertEqual(json.loads(changed["extrainfo"])["futureFeature"]["version"], 9)
        self.assertEqual(effect_count, 1)

    def test_three_price_modes_preserve_unknown_metadata_and_complex_effects(self):
        second = self.client.post(
            "/api/items/add",
            json={"name": "增减价商品", "price": 200, "count": 1, "category_id": 2},
        ).get_json()["id"]
        third = self.client.post(
            "/api/items/add",
            json={"name": "百分比商品", "price": 333, "count": 1, "category_id": 2},
        ).get_json()["id"]
        connection = self.connect()
        try:
            connection.execute(
                """
                UPDATE shopitemmodel
                SET price=101, extrainfo=?, purchaselimits=?
                WHERE id=101
                """,
                (
                    json.dumps({"futureFeature": {"enabled": True}}, ensure_ascii=False),
                    json.dumps([{"type": "daily", "value": 2}], ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO goodseffectmodel
                (createtime, shopitemid, goodseffecttype, relatedinfos, isdel,
                 updatetime, relatedid, values_lpcolumn)
                VALUES (1, 101, 7, '{"itemsInfos":[]}', 0, 1, 0, 0)
                """
            )
            connection.commit()
        finally:
            connection.close()

        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "price",
                    "data": {"id": 101, "price_mode": "set", "price_value": 75},
                },
                {
                    "line": 2,
                    "action": "price",
                    "data": {"id": second, "price_mode": "add", "price_value": -50},
                },
                {
                    "line": 3,
                    "action": "price",
                    "data": {"id": third, "price_mode": "percent", "price_value": -10},
                },
            ]
        )
        self.assertEqual(
            [row["normalized_data"]["price"] for row in preview["rows"]],
            [75, 150, 300],
        )
        self.execute(preview)

        connection = self.connect()
        try:
            prices = [
                connection.execute(
                    "SELECT price FROM shopitemmodel WHERE id=?", (item_id,)
                ).fetchone()[0]
                for item_id in (101, second, third)
            ]
            protected = connection.execute(
                "SELECT extrainfo, purchaselimits FROM shopitemmodel WHERE id=101"
            ).fetchone()
            effect_count = connection.execute(
                "SELECT COUNT(*) FROM goodseffectmodel WHERE shopitemid=101 AND isdel=0"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(prices, [75, 150, 300])
        self.assertTrue(json.loads(protected["extrainfo"])["futureFeature"]["enabled"])
        self.assertEqual(json.loads(protected["purchaselimits"])[0]["value"], 2)
        self.assertEqual(effect_count, 1)

    def test_invalid_create_field_types_are_stable_row_errors(self):
        preview = self.preview(
            [
                {
                    "line": 9,
                    "action": "create",
                    "data": {
                        "name": "边界商品",
                        "category": "道具",
                        "price": True,
                        "stock": -2,
                        "is_purchase_enabled": "也许",
                        "effect_type": "exp",
                        "effect_value": 0,
                        "effect_skill": 7,
                        "duplicate_policy": "later",
                    },
                }
            ]
        )
        self.assertFalse(preview["can_execute"])
        codes = {error["code"] for error in preview["rows"][0]["errors"]}
        self.assertTrue(
            {
                "INVALID_PRICE",
                "INVALID_STOCK",
                "INVALID_PURCHASE_STATE",
                "INVALID_EFFECT_VALUE",
                "INVALID_EFFECT_SKILL",
                "INVALID_DUPLICATE_POLICY",
            }.issubset(codes),
            codes,
        )

    def test_database_failure_rolls_back_created_items_inventory_and_effects(self):
        rows = [
            {
                "line": 1,
                "action": "create",
                "data": {
                    "name": "先创建商品",
                    "category": "道具",
                    "effect_type": "coin",
                    "effect_value": 3,
                },
            },
            {
                "line": 2,
                "action": "create",
                "data": {"name": "触发失败商品", "category": "奖励"},
            },
        ]
        preview = self.preview(rows)
        connection = self.connect()
        try:
            before_inventory = connection.execute(
                "SELECT COUNT(*) FROM inventorymodel"
            ).fetchone()[0]
            connection.executescript(
                """
                CREATE TRIGGER fail_item_import
                BEFORE INSERT ON shopitemmodel
                WHEN NEW.itemname = '触发失败商品'
                BEGIN
                    SELECT RAISE(ABORT, 'do not expose this item detail');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()

        failed = self.execute(preview, expected_status=500)
        self.assertEqual(failed["code"], "BATCH_EXECUTION_FAILED")
        self.assertNotIn("do not expose", json.dumps(failed, ensure_ascii=False))
        connection = self.connect()
        try:
            item_count = connection.execute(
                """
                SELECT COUNT(*) FROM shopitemmodel
                WHERE itemname IN ('先创建商品', '触发失败商品')
                """
            ).fetchone()[0]
            inventory_count = connection.execute(
                "SELECT COUNT(*) FROM inventorymodel"
            ).fetchone()[0]
            effect_count = connection.execute(
                """
                SELECT COUNT(*) FROM goodseffectmodel g
                JOIN shopitemmodel s ON s.id=g.shopitemid
                WHERE s.itemname IN ('先创建商品', '触发失败商品')
                """
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual((item_count, effect_count), (0, 0))
        self.assertEqual(inventory_count, before_inventory)
        self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)

    def test_upload_rejections_have_stable_item_codes(self):
        self.assertEqual(server.MAX_ITEM_IMPORT_FILE_BYTES, 1024 * 1024)
        missing = self.client.post(
            "/api/local/item-import-files",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["code"], "ITEM_IMPORT_FILE_REQUIRED")
        cases = (
            ("items.txt", b"action", "ITEM_IMPORT_UNSUPPORTED_FORMAT"),
            ("items.csv", b"\xff\xfeinvalid", "ITEM_IMPORT_INVALID_ENCODING"),
            (
                "items.csv",
                (ITEM_IMPORT_HEADER + '\r\n"unterminated').encode(),
                "ITEM_IMPORT_INVALID_CSV",
            ),
            ("items.json", b"{not-json", "ITEM_IMPORT_INVALID_JSON"),
            (
                "items.csv",
                b"action,name,unknown\r\ncreate,A,B\r\n",
                "ITEM_IMPORT_INVALID_COLUMNS",
            ),
            ("items.json", b"[]", "ITEM_IMPORT_INVALID_ROWS"),
            (
                "items.json",
                json.dumps([{"action": "create"}] * 201).encode(),
                "ITEM_IMPORT_INVALID_ROWS",
            ),
        )
        for name, content, code in cases:
            with self.subTest(code=code):
                body = self.upload(name, content, expected_status=400)
                self.assertEqual(body["ok"], False)
                self.assertEqual(body["code"], code)
        with mock.patch.object(server, "MAX_ITEM_IMPORT_FILE_BYTES", 16):
            too_large = self.upload(
                "items.json", b" " * 17, expected_status=400
            )
        self.assertEqual(too_large["code"], "ITEM_IMPORT_FILE_TOO_LARGE")

    def test_cloud_source_cannot_upload_preview_or_execute_item_import(self):
        headers = {"X-LifeUp-Data-Source": "cloud"}
        blocked_upload = self.upload(
            "items.json",
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
                    "data": {"name": "云端禁止", "category": "道具"},
                }
            ],
            expected_status=403,
            headers=headers,
        )
        self.assertEqual(blocked_preview["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE")

        local_preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"name": "仅本地", "category": "道具"},
                }
            ]
        )
        response = self.client.post(
            f"/api/local/batch-previews/{local_preview['preview_token']}/executions",
            json={"digest": local_preview["digest"]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )


class ItemBatchImportUiContractTests(unittest.TestCase):
    def test_item_page_uses_upload_preview_duplicate_and_execution_contract(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("商品 CSV/JSON 批量导入", html)
        self.assertIn("/api/local/item-import-files", html)
        self.assertIn("/api/local/item-import-templates/csv", html)
        self.assertIn("/api/local/item-import-templates/json", html)
        self.assertIn("previewItemImportRows", html)
        self.assertIn("setItemImportDuplicatePolicy", html)
        self.assertIn("按选择重新预览", html)
        self.assertIn("existing_item_ids", html)
        self.assertNotIn("innerHTML = row.data.name", html)

    def test_item_upload_runtime_uses_formdata_preview_and_cloud_guard(self):
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
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
  source += match[1] + '\n';
}
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
const baseElement = {
  classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
  setAttribute: noop, focus: noop
};
const file = { name: 'items.csv', size: 123 };
const elements = {
  itemImportFile: { ...baseElement, files: [file] },
  itemImportStatus: { ...baseElement },
  itemImportParseBtn: { ...baseElement }
};
const document = {
  body: { classList },
  getElementById: (id) => elements[id] || { ...baseElement },
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  createElement: () => ({ ...baseElement })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
class MockFormData {
  constructor() { this.entries = []; }
  append(...args) { this.entries.push(args); }
}
const sandbox = {
  console, document, localStorage, FormData: MockFormData,
  window: { addEventListener: noop }, location: { protocol: 'http:' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => true, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

let apiCalls = [];
let previewCall = null;
sandbox.api = (path, options) => {
  apiCalls.push({ path, options });
  return Promise.resolve({
    rows: [{ line: 1, action: 'create', data: { name: '导入商品', category: '道具' } }]
  });
};
sandbox.openLocalBatchPreview = (entity, rows) => {
  previewCall = { entity, rows };
  return Promise.resolve({ ok: true });
};

(async () => {
  await sandbox.parseItemImportFile();
  if (apiCalls.length !== 1 || apiCalls[0].path !== '/api/local/item-import-files') {
    throw new Error('item file did not use its upload endpoint');
  }
  const options = apiCalls[0].options || {};
  if (!(options.body instanceof MockFormData) || options.headers && options.headers['Content-Type']) {
    throw new Error('item upload did not preserve browser multipart boundary');
  }
  if (!previewCall || previewCall.entity !== 'items' || previewCall.rows.length !== 1) {
    throw new Error('parsed item rows did not enter item preview');
  }
  sandbox.itemImportRows = [{ data: { duplicate_policy: '' } }];
  sandbox.setItemImportDuplicatePolicy(0, 'skip');
  if (sandbox.itemImportRows[0].data.duplicate_policy !== 'skip') {
    throw new Error('item duplicate policy was not stored');
  }
  sandbox.dataSource = 'cloud';
  if (!sandbox.isCloudReadOnlyWrite('/api/local/item-import-files', { method: 'POST' })) {
    throw new Error('cloud mode did not block item import upload');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_batch_price_runtime_supports_add_and_percent_modes(self):
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
const element = { classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], setAttribute: noop, focus: noop };
let mode = 'add';
let value = '-25';
const selected = [{ value: '101' }, { value: '102' }];
const document = {
  body: { classList },
  getElementById: (id) => id === 'f_batchpricemode' ? { ...element, value: mode } :
    (id === 'f_batchprice' ? { ...element, value } : { ...element }),
  querySelector: () => null,
  querySelectorAll: selector => selector === '.batch-check:checked' ? selected : [],
  addEventListener: noop,
  createElement: () => ({ ...element })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
const sandbox = { console, document, localStorage, window: { addEventListener: noop }, location: { protocol: 'http:' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController, fetch: noop,
  confirm: () => true, alert: noop, Blob, URL };
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
let preview = null;
let toasts = [];
sandbox.openLocalBatchPreview = (entity, rows) => { preview = { entity, rows }; return Promise.resolve(); };
sandbox.toast = (message, type) => toasts.push({ message, type });
sandbox.closeModal = noop;

sandbox.doBatchPrice();
if (!preview || preview.entity !== 'items' || preview.rows[0].data.price_mode !== 'add' ||
    preview.rows[0].data.price_value !== -25) {
  throw new Error('add price mode was not submitted');
}
mode = 'percent'; value = '-20'; preview = null;
sandbox.doBatchPrice();
if (!preview || preview.rows[1].data.price_mode !== 'percent' || preview.rows[1].data.price_value !== -20) {
  throw new Error('percent price mode was not submitted');
}
mode = 'percent'; value = '-101'; preview = null; toasts = [];
sandbox.doBatchPrice();
if (preview || !toasts.some(entry => entry.type === 'error')) {
  throw new Error('percent below -100 reached preview');
}
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
