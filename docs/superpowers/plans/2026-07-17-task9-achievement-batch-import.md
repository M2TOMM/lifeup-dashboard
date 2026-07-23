# Task 9 自定义成就批量管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为本地 LifeUp 备份工作副本增加安全的自定义成就 CSV/JSON 批量新增流程。

**Status:** 已于 2026-07-17 完成实现、真实副本恢复演练和最终验收。

**Architecture:** 在 `server.py` 中复用 Task 8 的有界文件解析模式，并把规范化结果送入 Task 6 现有的 `preview_token + digest + snapshot + BEGIN IMMEDIATE` 执行链。前端在 `index.html` 的本地成就页复用统一批量预览窗口；系统成就、解锁条件与 cloud 数据始终不写入。

**Tech Stack:** Python 3 标准库、Flask、SQLite、原生 HTML/CSS/JavaScript、`unittest`、Node `vm.Script`。

## Global Constraints

- 原始备份 `C:\Users\M2TO\Documents\LifeUp\LifeupBackup.zip` 永不修改、覆盖、移动或删除。
- 只写 `/api/open-upload` 创建的 `workspaces/browser-imports/` 托管副本。
- Task 9 只写 `userachievementmodel`；`achievementinfomodel` 和 `unlockconditionmodel` 始终只读。
- 只允许 `achievements/create`；不实现编辑、删除、解锁条件或图标文件上传。
- cloud 上传、预览和执行全部返回 `403 LOCAL_WRITE_REQUIRES_LOCAL_SOURCE`。
- 文件只接受 UTF-8/UTF-8 BOM CSV/JSON，大小 1～1,048,576 字节，行数 1～200。
- 不增加第三方依赖，不拆分 `server.py` / `index.html`，不编辑 `dist/`。
- 保留 Task 5～8 现有未提交改动；不执行 reset、checkout、Git 提交、推送或发布。
- 所有 SQL 参数化；前端数据库文本必须经 `escHtml` / `escAttr` 或安全文本写入。
- 严格执行 RED → GREEN → REFACTOR；每个 GREEN 后运行对应定向测试。

## File Map

- Create: `tests/test_achievement_batch_import.py` — Task 9 的上传、预览、执行、事务、cloud 隔离和 UI 契约测试。
- Modify: `server.py` — 成就模板、文件解析、行规范化、冲突检查、插入 helper 和批量执行分支。
- Modify: `index.html` — 成就导入状态、上传窗口、预览行、重复策略和状态清理。
- Create: `outputs/achievement_import_template.csv` — UTF-8 BOM 成就 CSV 示例。
- Create: `outputs/achievement_import_template.json` — UTF-8 BOM 成就 JSON 示例。
- Modify: `tasks/plan.md` — 标记 Task 9 实现与验收阶段。
- Modify: `tasks/todo.md` — 完成 Task 9，保留 Task 10 图标资源管理。
- Modify: `outputs/LIFEUP_DASHBOARD_PRODUCT_ROADMAP.md` — 记录 Task 9 已交付及 Task 10 边界。
- Create: `outputs/LIFEUP_DASHBOARD_TASK9_HANDOFF_2026-07-17.md` — 最终状态、验证证据、运行状态与下一步。

---

### Task 1: 成就模板与文件解析接口

**Files:**
- Create: `tests/test_achievement_batch_import.py`
- Modify: `server.py:50-65`
- Modify: `server.py:5655-5854` 后新增成就导入函数
- Create: `outputs/achievement_import_template.csv`
- Create: `outputs/achievement_import_template.json`

**Interfaces:**
- Consumes: `MAX_BATCH_SIZE`, `_local_batch_error()`, `reject_cloud_local_write()`。
- Produces: `ACHIEVEMENT_IMPORT_COLUMNS`, `MAX_ACHIEVEMENT_IMPORT_FILE_BYTES`, `_parse_achievement_import_csv(text)`, `_parse_achievement_import_json(text)`，以及三个 `/api/local/achievement-import-*` 接口。

- [x] **Step 1: 创建测试夹具和模板/解析失败测试**

在 `tests/test_achievement_batch_import.py` 创建 `AchievementBatchImportApiTests`，继承 `tests.fixtures.LocalCrudApiTestCase`，并在 `setUp()` 后为一次性数据库补齐系统成就和条件表：

```python
class AchievementBatchImportApiTests(LocalCrudApiTestCase):
    def setUp(self):
        super().setUp()
        connection = self.connect()
        try:
            connection.executescript("""
                CREATE TABLE achievementinfomodel (
                    id INTEGER PRIMARY KEY,
                    title TEXT
                );
                CREATE TABLE unlockconditionmodel (
                    id INTEGER PRIMARY KEY,
                    userachievementid INTEGER,
                    isdel INTEGER DEFAULT 0
                );
                INSERT INTO achievementinfomodel (id, title)
                VALUES (1, 'achievement_base_new_player');
            """)
            connection.commit()
        finally:
            connection.close()

    def upload(self, name, content, expected_status=200, headers=None):
        response = self.client.post(
            "/api/local/achievement-import-files",
            data={"file": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
            headers=headers or {},
        )
        self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
        return response.get_json()

    def test_templates_and_utf8_csv_and_json_file_parsing(self):
        csv_response = self.client.get("/api/local/achievement-import-templates/csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"action,name,category,description,coin,exp,icon,conditions,duplicate_policy", csv_response.data)

        json_response = self.client.get("/api/local/achievement-import-templates/json")
        self.assertEqual(json_response.status_code, 200)
        self.assertTrue(json_response.data.startswith(b"\xef\xbb\xbf"))

        parsed_csv = self.upload(
            "achievements.csv",
            "action,name,category,description,coin,exp,icon,conditions,duplicate_policy\r\n"
            "create,筑基里程碑,里程碑,完成筑基,88,144,golden-core.png,,\r\n".encode("utf-8-sig"),
        )
        self.assertEqual(parsed_csv["rows"][0]["line"], 2)
        self.assertEqual(parsed_csv["rows"][0]["data"]["name"], "筑基里程碑")

        parsed_json = self.upload(
            "achievements.json",
            json.dumps([{"action": "create", "name": "金丹里程碑"}], ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(parsed_json["rows"][0]["line"], 1)
        self.assertEqual(parsed_json["rows"][0]["action"], "create")

    def test_upload_rejections_have_stable_achievement_codes(self):
        cases = (
            ("achievements.txt", b"action", "ACHIEVEMENT_IMPORT_UNSUPPORTED_FORMAT"),
            ("achievements.csv", b"\xff\xfeinvalid", "ACHIEVEMENT_IMPORT_INVALID_ENCODING"),
            ("achievements.csv", b'action,name\r\n"unterminated', "ACHIEVEMENT_IMPORT_INVALID_CSV"),
            ("achievements.json", b"{not-json", "ACHIEVEMENT_IMPORT_INVALID_JSON"),
            ("achievements.csv", b"action,name,unknown\r\ncreate,A,B\r\n", "ACHIEVEMENT_IMPORT_INVALID_COLUMNS"),
            ("achievements.json", b"[]", "ACHIEVEMENT_IMPORT_INVALID_ROWS"),
        )
        for name, content, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.upload(name, content, 400)["code"], code)
```

- [x] **Step 2: 运行 RED 测试并确认失败原因正确**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_templates_and_utf8_csv_and_json_file_parsing tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_upload_rejections_have_stable_achievement_codes -v
```

Expected: FAIL，原因是三个成就导入路由返回 404；不能是测试夹具或导入错误。

- [x] **Step 3: 实现常量、模板、CSV/JSON 解析和上传接口**

在 `server.py` 常量区加入：

```python
MAX_ACHIEVEMENT_IMPORT_FILE_BYTES = 1024 * 1024
ACHIEVEMENT_IMPORT_COLUMNS = (
    'action', 'name', 'category', 'description', 'coin', 'exp', 'icon',
    'conditions', 'duplicate_policy',
)
```

按 Task 8 的 `_parse_item_import_*` 结构实现 `_achievement_import_template_rows()`、`_validate_achievement_import_columns()`、`_parse_achievement_import_csv()`、`_parse_achievement_import_json()` 和 `parse_achievement_import_file()`。CSV 必需列为 `action/name/category/description/coin/exp/icon`，可省略 `conditions/duplicate_policy`；JSON 只允许九个契约字段。上传错误统一经：

```python
def _achievement_import_error(code, message, status=400, suggestion=None):
    return _local_batch_error(code, message, status, suggestion)
```

模板路由的文件名固定为 `achievement_import_template.csv` 和 `achievement_import_template.json`，两者都带 UTF-8 BOM；上传接口在读取文件前先调用 `reject_cloud_local_write()`。

- [x] **Step 4: 运行 GREEN 测试**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_templates_and_utf8_csv_and_json_file_parsing tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_upload_rejections_have_stable_achievement_codes -v
```

Expected: 2 tests，`OK`。

- [x] **Step 5: 写入固定示例文件并核对 BOM/字段**

使用接口响应内容生成 `outputs/achievement_import_template.csv` 与 `outputs/achievement_import_template.json`，随后验证：

```powershell
python -c "from pathlib import Path; files=[Path('outputs/achievement_import_template.csv'),Path('outputs/achievement_import_template.json')]; print([(p.name,p.read_bytes()[:3],len(p.read_bytes())) for p in files])"
```

Expected: 两个文件前三字节均为 `b'\xef\xbb\xbf'`，长度大于 3。

---

### Task 2: 成就预览规范化、分类映射与冲突规则

**Files:**
- Modify: `tests/test_achievement_batch_import.py`
- Modify: `server.py:1114-1798`

**Interfaces:**
- Consumes: `_task_import_integer()`, `_task_import_name_map()`, `_append_local_batch_row_error()`。
- Produces: `_normalize_achievement_create_data(row, data, category_map)`；`_normalize_local_batch_rows()` 对 `entity='achievements'` 返回可执行的 `achievements/create` 计划。

- [x] **Step 1: 写字段、条件、分类、系统冲突和重复策略 RED 测试**

增加 `preview()` helper，并覆盖以下断言：

```python
def preview(self, rows, expected_status=200, headers=None):
    response = self.client.post(
        "/api/local/batch-previews",
        json={"entity": "achievements", "rows": rows},
        headers=headers or {},
    )
    self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
    return response.get_json()

def test_preview_normalizes_base_fields_and_category(self):
    preview = self.preview([{
        "line": 7,
        "action": "create",
        "data": {
            "name": "  筑基里程碑  ", "category": " 里程碑 ",
            "description": "完成筑基", "coin": "88", "exp": 144,
            "icon": "golden-core.png", "conditions": "",
            "duplicate_policy": "",
        },
    }])
    self.assertTrue(preview["can_execute"])
    row = preview["rows"][0]
    self.assertEqual(row["line"], 7)
    self.assertEqual(row["normalized_data"]["name"], "筑基里程碑")
    self.assertEqual(row["normalized_data"]["category_id"], 7)
    self.assertEqual(row["normalized_data"]["duplicate_policy"], "create")
    self.assertEqual(row["planned_action"]["action"], "create")

def test_conditions_system_name_and_invalid_icon_block_preview(self):
    preview = self.preview([
        {"line": 1, "action": "create", "data": {"name": "有条件", "conditions": "完成任务 10 次"}},
        {"line": 2, "action": "create", "data": {"name": "achievement_base_new_player"}},
        {"line": 3, "action": "create", "data": {"name": "危险图标", "icon": "javascript:alert(1)"}},
    ])
    codes = [{error["code"] for error in row["errors"]} for row in preview["rows"]]
    self.assertIn("ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED", codes[0])
    self.assertIn("SYSTEM_ACHIEVEMENT_NAME_CONFLICT", codes[1])
    self.assertIn("INVALID_ACHIEVEMENT_ICON", codes[2])
    self.assertFalse(preview["can_execute"])

def test_existing_and_file_duplicates_require_row_policy(self):
    first = self.preview([
        {"line": 2, "action": "create", "data": {"name": "已有成就"}},
        {"line": 3, "action": "create", "data": {"name": "同批重复"}},
        {"line": 4, "action": "create", "data": {"name": " 同批重复 "}},
    ])
    self.assertFalse(first["can_execute"])
    self.assertTrue(all("DUPLICATE_POLICY_REQUIRED" in {e["code"] for e in row["errors"]} for row in first["rows"]))
```

测试夹具需插入一个未删除的自定义成就“已有成就”、分类 `id=7/categoryname='里程碑'`，并额外插入两个大小写相同的分类以测试 `AMBIGUOUS_ACHIEVEMENT_CATEGORY`。

- [x] **Step 2: 运行 RED 测试**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_preview_normalizes_base_fields_and_category tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_conditions_system_name_and_invalid_icon_block_preview tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_existing_and_file_duplicates_require_row_policy -v
```

Expected: FAIL，现有代码仍返回 `ACHIEVEMENT_WRITE_NOT_AVAILABLE`。

- [x] **Step 3: 实现成就行规范化**

新增完整 helper：

```python
def _normalize_achievement_create_data(row, data, category_map):
    normalized = row['normalized_data']
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else name
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        _append_local_batch_row_error(row, 'INVALID_ACHIEVEMENT_NAME', '成就名称必须是 1～200 个字符。', 'data.name')
    else:
        normalized['name'] = name

    category = data.get('category')
    category = category.strip() if isinstance(category, str) else category
    if category in (None, ''):
        normalized['category'] = ''
        normalized['category_id'] = 0
    elif not isinstance(category, str):
        _append_local_batch_row_error(row, 'INVALID_ACHIEVEMENT_CATEGORY', '成就分类必须是文本名称。', 'data.category')
    else:
        matches = category_map.get(category.casefold(), [])
        if not matches:
            _append_local_batch_row_error(row, 'INVALID_ACHIEVEMENT_CATEGORY', f'找不到成就分类“{category}”。', 'data.category')
        elif len(matches) > 1:
            _append_local_batch_row_error(row, 'AMBIGUOUS_ACHIEVEMENT_CATEGORY', f'存在多个同名成就分类“{category}”。', 'data.category')
        else:
            normalized['category_id'], normalized['category'] = matches[0]

    description = data.get('description', '')
    description = description.strip() if isinstance(description, str) else description
    if not isinstance(description, str) or len(description) > 2000:
        _append_local_batch_row_error(row, 'INVALID_ACHIEVEMENT_DESCRIPTION', '成就描述必须是不超过 2000 个字符的文本。', 'data.description')
    else:
        normalized['description'] = description

    for field, code, label in (('coin', 'INVALID_ACHIEVEMENT_COIN', '金币奖励'), ('exp', 'INVALID_ACHIEVEMENT_EXP', '经验奖励')):
        value = _task_import_integer(data.get(field), 0, 0)
        if value is None:
            _append_local_batch_row_error(row, code, f'{label}必须是 0～{MAX_ITEM_PRICE} 的整数。', f'data.{field}')
        else:
            normalized[field] = value

    icon = data.get('icon', '')
    icon = icon.strip() if isinstance(icon, str) else icon
    unsafe_icon = isinstance(icon, str) and (any(ord(char) < 32 for char in icon) or re.match(r'^(?:javascript|vbscript|data):', icon, re.I))
    if not isinstance(icon, str) or len(icon) > 500 or unsafe_icon:
        _append_local_batch_row_error(row, 'INVALID_ACHIEVEMENT_ICON', '图标引用必须是不超过 500 个字符的安全文本。', 'data.icon')
    else:
        normalized['icon'] = icon

    conditions = data.get('conditions', '')
    if conditions is not None and (not isinstance(conditions, str) or conditions.strip()):
        _append_local_batch_row_error(row, 'ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED', 'Task 9 不支持导入解锁条件，请清空 conditions。', 'data.conditions')
    else:
        normalized['conditions'] = ''

    policy = data.get('duplicate_policy')
    policy = policy.strip().lower() if isinstance(policy, str) else policy
    if policy in (None, ''):
        policy = ''
    if policy not in ('', 'skip', 'create'):
        _append_local_batch_row_error(row, 'INVALID_DUPLICATE_POLICY', '重复策略只接受 skip 或 create。', 'data.duplicate_policy')
    else:
        normalized['duplicate_policy'] = policy
```

在 `_normalize_local_batch_rows()` 中：

- 把 `achievements: {'create'}` 加入 `allowed_actions`。
- 预加载 `userachcategorymodel` 分类 map。
- 预加载 `achievementinfomodel.title` 的 `strip().casefold()` 集合。
- 对 `achievements/create` 调用 helper，并收集 `create_achievement_name_indices`。
- 系统同名直接追加 `SYSTEM_ACHIEVEMENT_NAME_CONFLICT`。
- 自定义/文件内同名生成 `duplicate = {'found', 'existing_achievement_ids', 'import_lines'}`。
- 有重复且策略为空时追加 `DUPLICATE_POLICY_REQUIRED`；无重复且为空时归一为 `create`。
- 删除无条件添加 `ACHIEVEMENT_WRITE_NOT_AVAILABLE` 的旧分支。
- ready 行的 `planned_action` 加入 `duplicate_found`。

- [x] **Step 4: 运行 GREEN 测试并回归 Task 6/8 预览**

```powershell
python -m unittest tests.test_achievement_batch_import tests.test_local_batch_preview tests.test_item_batch_import -v
```

Expected: 新增预览测试通过；把 Task 6 原先“成就全部阻止”测试改为断言 `action='delete'` 仍返回 `INVALID_ACTION`，不能删除这条安全回归。

---

### Task 3: 共享插入 helper 与单事务执行

**Files:**
- Modify: `tests/test_achievement_batch_import.py`
- Modify: `server.py:1809-1878`
- Modify: `server.py:4526-4553`

**Interfaces:**
- Consumes: `_execute_local_batch_action(cursor, planned_action)` 和现有执行接口的 token/digest/snapshot/transaction 契约。
- Produces: `_insert_achievement_with_cursor(cursor, data) -> int`，供单条新增和批量新增共用。

- [x] **Step 1: 写执行、skip、回滚和只写目标表 RED 测试**

```python
def execute(self, preview, expected_status=200, headers=None):
    response = self.client.post(
        f"/api/local/batch-previews/{preview['preview_token']}/executions",
        json={"digest": preview["digest"]},
        headers=headers or {},
    )
    self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
    return response.get_json()

def test_create_and_skip_execute_without_conditions_or_system_changes(self):
    connection = self.connect()
    before_system = connection.execute("SELECT COUNT(*) FROM achievementinfomodel").fetchone()[0]
    before_conditions = connection.execute("SELECT COUNT(*) FROM unlockconditionmodel").fetchone()[0]
    connection.close()
    preview = self.preview([
        {"line": 1, "action": "create", "data": {"name": "已有成就", "duplicate_policy": "skip"}},
        {"line": 2, "action": "create", "data": {"name": "新增成就", "category": "里程碑", "coin": 88, "exp": 144, "icon": "golden-core.png"}},
    ])
    executed = self.execute(preview)
    self.assertEqual([row["affected"] for row in executed["rows"]], [0, 1])
    connection = self.connect()
    created = connection.execute("SELECT * FROM userachievementmodel WHERE content='新增成就'").fetchone()
    self.assertEqual((created["type"], created["categoryid"], created["rewardcoin"], created["expreward"]), (0, 7, 88, 144))
    self.assertEqual((created["achievementstatus"], created["currentvalue"], created["progress"], created["isdelete"]), (0, 0, 0, 0))
    self.assertEqual(connection.execute("SELECT COUNT(*) FROM achievementinfomodel").fetchone()[0], before_system)
    self.assertEqual(connection.execute("SELECT COUNT(*) FROM unlockconditionmodel").fetchone()[0], before_conditions)
    connection.close()

def test_database_failure_rolls_back_all_achievement_rows_and_keeps_snapshot(self):
    preview = self.preview([
        {"line": 1, "action": "create", "data": {"name": "先创建成就"}},
        {"line": 2, "action": "create", "data": {"name": "触发失败成就"}},
    ])
    connection = self.connect()
    connection.executescript("""
        CREATE TRIGGER fail_achievement_import
        BEFORE INSERT ON userachievementmodel
        WHEN NEW.content='触发失败成就'
        BEGIN SELECT RAISE(ABORT, 'private achievement trigger detail'); END;
    """)
    connection.commit()
    connection.close()
    failed = self.execute(preview, 500)
    self.assertEqual(failed["code"], "BATCH_EXECUTION_FAILED")
    self.assertNotIn("private achievement", json.dumps(failed, ensure_ascii=False))
    connection = self.connect()
    count = connection.execute("SELECT COUNT(*) FROM userachievementmodel WHERE content IN ('先创建成就','触发失败成就')").fetchone()[0]
    connection.close()
    self.assertEqual(count, 0)
    self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)
```

- [x] **Step 2: 运行 RED 测试**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_create_and_skip_execute_without_conditions_or_system_changes tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_database_failure_rolls_back_all_achievement_rows_and_keeps_snapshot -v
```

Expected: FAIL，原因是 `_execute_local_batch_action()` 尚未支持 `achievements/create`。

- [x] **Step 3: 提取共享插入 helper 并接入执行器**

```python
def _insert_achievement_with_cursor(cur, data):
    now = now_ms()
    cur.execute("""
        INSERT INTO userachievementmodel (
            content, description, type, categoryid, rewardcoin, icon,
            achievementstatus, currentvalue, progress, createtime, updatetime,
            isdelete, isgotreward, rewardcoinvariable, orderincategory, expreward
        ) VALUES (?, ?, 0, ?, ?, ?, 0, 0, 0, ?, ?, 0, 0, 0, 0, ?)
    """, (
        data.get('name', '新成就'), data.get('description', ''),
        data.get('category_id', 0), data.get('coin', 0), data.get('icon', ''),
        now, now, data.get('exp', 0),
    ))
    return cur.lastrowid
```

`add_achievement()` 改为调用该 helper 后提交；异常响应仍沿用当前单条接口行为。`_execute_local_batch_action()` 在读取 `target_id` 前加入：

```python
if entity == 'achievements' and action == 'create':
    if planned_action.get('duplicate_found') and data['duplicate_policy'] == 'skip':
        return {'affected': 0, 'skipped': True, 'reason': 'duplicate'}
    _insert_achievement_with_cursor(cursor, data)
    return {'affected': 1}
```

- [x] **Step 4: 运行 GREEN 和 CRUD 回归**

```powershell
python -m unittest tests.test_achievement_batch_import tests.test_local_crud.AchievementCrudRegressionTests tests.test_local_batch_preview -v
```

Expected: 全部 `OK`；单条成就 CRUD 的 `type` 参数仍保留原行为，批量新增固定 `type=0`。

- [x] **Step 5: 增加 cloud 三层隔离测试**

测试同一组 `X-LifeUp-Data-Source: cloud` 请求：上传接口 403、预览接口 403、本地生成 token 后用 cloud 执行仍 403；同时断言未创建成就、快照或额外 token。

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportApiTests.test_cloud_source_cannot_upload_preview_or_execute_achievement_import -v
```

Expected: 1 test，`OK`。

---

### Task 4: 成就页上传、预览与重复策略 UI

**Files:**
- Modify: `tests/test_achievement_batch_import.py`
- Modify: `index.html:943-960`
- Modify: `index.html:1245-1565`
- Modify: `index.html:4267-4334`

**Interfaces:**
- Consumes: `api()`, `showModal()`, `openLocalBatchPreview()`, `renderLocalBatchPreview()`, `executeLocalBatchPreview()`, `escHtml()` 和 `escAttr()`。
- Produces: `openAchievementImportDialog()`, `showAchievementImportFileName()`, `parseAchievementImportFile()`, `previewAchievementImportRows()`, `setAchievementImportDuplicatePolicy()`。

- [x] **Step 1: 写静态契约和 Node 运行时 RED 测试**

```python
class AchievementBatchImportUiContractTests(unittest.TestCase):
    def test_achievement_page_uses_upload_preview_duplicate_and_execution_contract(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("成就 CSV/JSON 批量导入", html)
        self.assertIn("/api/local/achievement-import-files", html)
        self.assertIn("/api/local/achievement-import-templates/csv", html)
        self.assertIn("/api/local/achievement-import-templates/json", html)
        self.assertIn("previewAchievementImportRows", html)
        self.assertIn("setAchievementImportDuplicatePolicy", html)
        self.assertIn("existing_achievement_ids", html)
        self.assertNotIn("innerHTML = row.data.name", html)
```

Node 测试沿用商品导入的沙箱，文件名改为 `achievements.csv`，断言 FormData 请求路径、`entity === 'achievements'`、逐行 `duplicate_policy='skip'` 写回，以及 cloud 模式被 `isCloudReadOnlyWrite()` 阻止。

- [x] **Step 2: 运行 RED 测试**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportUiContractTests -v
```

Expected: FAIL，原因是前端函数和入口尚不存在。

- [x] **Step 3: 实现 UI 状态与上传流程**

在全局状态区加入：

```javascript
var achievementImportRows = [];
var achievementImportFileName = '';
```

在本地写入状态清理函数中清空两者；在 `isCloudReadOnlyWrite()` 的本地接口识别中加入 `/api/local/achievement-import-files`。实现上传函数时复用商品导入结构，FormData 不手动设置 `Content-Type`：

```javascript
async function parseAchievementImportFile() {
  var input = document.getElementById('achievementImportFile');
  var status = document.getElementById('achievementImportStatus');
  var button = document.getElementById('achievementImportParseBtn');
  var file = input && input.files ? input.files[0] : null;
  if (!file) { status.textContent = '请先选择文件。'; return; }
  if (file.size > 1024 * 1024) { status.textContent = '文件不能超过 1 MiB。'; return; }
  button.disabled = true;
  try {
    var form = new FormData();
    form.append('file', file);
    var parsed = await api('/api/local/achievement-import-files', { method: 'POST', body: form });
    achievementImportRows = Array.isArray(parsed.rows) ? parsed.rows : [];
    achievementImportFileName = file.name;
    closeModal();
    return previewAchievementImportRows();
  } catch (error) {
    status.textContent = error.message || '文件解析失败。';
  } finally {
    button.disabled = false;
  }
}

function setAchievementImportDuplicatePolicy(rowIndex, policy) {
  var row = achievementImportRows[rowIndex];
  if (!row || !row.data) return;
  row.data.duplicate_policy = policy;
  localBatchPreviewState = null;
}

function previewAchievementImportRows() {
  if (!Array.isArray(achievementImportRows) || achievementImportRows.length === 0) {
    toast('请先上传成就文件', 'error');
    return Promise.resolve();
  }
  return openLocalBatchPreview('achievements', achievementImportRows);
}
```

导入窗口显示模板链接、1 MiB/200 行/UTF-8 限制，以及“解锁条件必须为空”的红色说明。

- [x] **Step 4: 扩展统一预览渲染与执行后清理**

`renderLocalBatchPreview()` 增加成就分支：显示名称、分类、金币/经验、系统冲突和逐行重复策略；`executeLocalBatchPreview()` 在执行前记录 `wasAchievementImport`，成功后清空成就导入状态并刷新 `loadAchievements()`。

本地成就页工具栏加入：

```javascript
(dataSource === 'cloud' ? '' : '<button class="btn btn-green" onclick="openAchievementImportDialog()">📥 成就 CSV/JSON 批量导入</button>')
```

所有名称、分类、错误和文件名必须经过 `escHtml` / `escAttr`，不能把数据拼入 `onclick` 参数；策略选择只传数组索引和固定字符串。

- [x] **Step 5: 运行 GREEN 与 JavaScript 语法检查**

```powershell
python -m unittest tests.test_achievement_batch_import.AchievementBatchImportUiContractTests tests.test_batch_ui tests.test_local_batch_preview.LocalBatchPreviewUiContractTests -v
$node = 'C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
@'
const fs = require('fs');
const vm = require('vm');
const path = 'C:/Users/M2TO/Documents/LifeUp/lifeup-dashboard/index.html';
const html = fs.readFileSync(path, 'utf8');
let script = '';
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) script += match[1] + '\n';
new vm.Script(script, { filename: path });
console.log('frontend JS syntax ok');
'@ | & $node
```

Expected: 所有 UI 测试 `OK`；Node 输出 `frontend JS syntax ok`。

---

### Task 5: 完整回归、真实副本验收与交接

**Files:**
- Modify: `tasks/plan.md`
- Modify: `tasks/todo.md`
- Modify: `outputs/LIFEUP_DASHBOARD_PRODUCT_ROADMAP.md`
- Create: `outputs/LIFEUP_DASHBOARD_TASK9_HANDOFF_2026-07-17.md`

**Interfaces:**
- Consumes: Task 1～4 完成的所有接口和 UI。
- Produces: 可恢复的真实副本验收证据、清理后的运行状态和 Task 10 续接点。

- [x] **Step 1: 运行 Task 9 定向测试和完整回归**

```powershell
python -m unittest tests.test_achievement_batch_import -v
python -m unittest discover -s tests -v
python -m py_compile server.py desktop_app.py tests\test_achievement_batch_import.py
git diff --check
```

Expected: 全部测试 `OK`；Python 编译无输出；`git diff --check` 无 whitespace error。记录实际测试数量与耗时，不沿用 Task 8 的 130 条历史数字。

- [x] **Step 2: 启动服务并重新加载原托管副本**

先检查 5000 端口；启动前后都通过 `/api/status` 核对工作副本为：

```text
workspaces/browser-imports/20260711-104636-512c4541-LifeupBackup.zip
```

不得把 `C:\Users\M2TO\Documents\LifeUp\LifeupBackup.zip` 直接加载为可写工作区。

- [x] **Step 3: 执行真实副本新增、导出和恢复演练**

上传包含以下三类行的 UTF-8 CSV：一个无重复基础成就、一个已有自定义成就的 `skip` 行、一个分类/奖励/安全图标引用完整的新增行。执行前记录：

- `userachievementmodel` 未删除行数。
- `achievementinfomodel` 行数与内容摘要。
- `unlockconditionmodel` 行数与内容摘要。
- `/api/snapshots` 当前列表。

预览必须 `can_execute=true`，执行后新增行 `affected=1`、skip 行 `affected=0`。核对固定状态列、相同毫秒创建/更新时间，以及系统表/条件表摘要完全未变。导出 ZIP 后调用现有完整性检查，要求 archive/database 均为 `ok`。

- [x] **Step 4: 恢复快照并清理所有测试产物**

恢复执行前自动快照，重新加载同一托管副本；确认测试成就为 0、原自定义成就数量恢复、系统与条件表不变、`GET /api/snapshots` 返回 `count: 0`。删除本轮临时 CSV、测试导出 ZIP、恢复 ZIP和临时 schema 副本；保留前序交接明确要求保留的历史文件。

- [x] **Step 5: 用 HTTP 页面完成浏览器验收**

访问 `http://127.0.0.1:5000/`，检查本地成就页：入口可见、模板链接可下载、选择文件后文件名显示、预览字段正确、cloud 模式不显示写入口。检查控制台 error/warn、相关网络请求状态和当前数据源标识；不使用 `file://`。

- [x] **Step 6: 复核原始备份指纹**

```powershell
$p='C:\Users\M2TO\Documents\LifeUp\LifeupBackup.zip'
$item=Get-Item -LiteralPath $p
$hash=Get-FileHash -LiteralPath $p -Algorithm SHA256
[pscustomobject]@{SHA256=$hash.Hash;Length=$item.Length;LastWriteTimeUtc=$item.LastWriteTimeUtc.ToString('o')}
```

Expected:

```text
SHA256: D794C78B277F68F1AE60A1F6F06E981C8A758F88C91C350F637A282741A46D46
Length: 43070521
LastWriteTimeUtc: 2026-07-02T14:13:27.0000000Z
```

- [x] **Step 7: 更新路线图并创建 Task 9 交接**

交接文档必须记录：实现接口、测试实际数量、真实副本前后行数、快照/导出/恢复结果、浏览器控制台结果、原始备份指纹、当前服务端口/工作副本、未提交改动边界，以及 Task 10“图标文件上传、浏览、失效引用检查和批量替换”。不要复制本计划和设计全文，只链接：

- `docs/superpowers/specs/2026-07-16-task9-achievement-batch-design.md`
- `docs/superpowers/plans/2026-07-17-task9-achievement-batch-import.md`

## Self-Review Result

- Spec coverage: 模板、上传边界、字段规范化、分类歧义、系统冲突、条件阻止、重复策略、单事务、快照、cloud 隔离、前端状态清理、真实恢复和原始备份复核均有对应任务。
- Placeholder scan: 未发现占位语句或无内容的“添加测试/错误处理”步骤。
- Type consistency: 前后端统一使用 `entity='achievements'`、`action='create'`、`name/category/category_id/description/coin/exp/icon/conditions/duplicate_policy`；重复元数据统一为 `existing_achievement_ids/import_lines`。
- Scope control: 不写系统成就或条件，不上传图标，不拆分大型文件，不新增依赖，不执行 Git 提交。
