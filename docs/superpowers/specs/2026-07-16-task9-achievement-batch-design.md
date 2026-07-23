# Task 9 设计：自定义成就 CSV/JSON 批量管理

日期：2026-07-16
状态：用户已批准，Task 9 已于 2026-07-17 实现并验收

## 1. 目标

在本地备份工作副本中提供自定义成就 CSV/JSON 批量新增功能。用户可以下载模板、上传文件、查看逐行预览、处理同名自定义成就，再通过 Task 6 的统一安全契约执行。

Task 9 只写 `userachievementmodel`。系统成就表 `achievementinfomodel` 始终只读，解锁条件表 `unlockconditionmodel` 本轮不写入。

## 2. 成功标准

- 支持名称、分类、描述、金币奖励、经验奖励和图标引用。
- 文件最大 1 MiB、最多 200 行，只接受 UTF-8/UTF-8 BOM CSV 或 JSON。
- 每行预览保留原始文件行号，并显示规范化数据、计划动作和错误。
- 与已有自定义成就或文件内其他行同名时，用户逐行选择 `skip` 或 `create`。
- 与系统成就同名时直接阻止，不能用 `create` 绕过。
- 非空解锁条件直接阻止，不创建任何 `unlockconditionmodel` 记录。
- 执行前自动快照，所有新增位于一个 `BEGIN IMMEDIATE` 事务；任意失败整批回滚。
- cloud 模式的上传、预览和执行全部拒绝。
- 真实工作副本验收后恢复快照并清理测试记录和临时产物。

## 3. 不在本轮范围

- 不编辑或删除已有自定义成就。
- 不新增、编辑或删除系统成就。
- 不导入或生成解锁条件。
- 不上传图标文件，不检查图标文件是否存在；图标资源上传和引用修复留给 Task 10。
- 不改写手机云端已有数据。
- 不拆分 `server.py` 或 `index.html`，不增加第三方依赖。

## 4. 选定架构

复用 Task 6 的统一批量框架，不建立第二套执行系统：

1. 模板和上传接口把 CSV/JSON 转成标准行结构。
2. `POST /api/local/batch-previews` 使用 `entity='achievements'` 规范化并验证每一行。
3. 服务端生成 digest 和一次性、600 秒预览 token。
4. 用户确认后调用现有执行接口。
5. 执行器先创建服务器快照，再在单个 SQLite 事务中新增全部可执行成就。
6. 成功后返回逐行结果；数据库异常时回滚全部新增，快照保留用于检查和恢复。

现有 Task 6 对所有成就返回 `ACHIEVEMENT_WRITE_NOT_AVAILABLE`。Task 9 将其收紧为：仅允许 `achievements/create`，其他成就 action 继续返回稳定错误。

## 5. 文件契约

CSV 列顺序和 JSON 允许字段统一为：

```text
action,name,category,description,coin,exp,icon,conditions,duplicate_policy
```

下载模板包含全部九列。CSV 必须包含 `action`、`name`、`category`、`description`、`coin`、`exp`、`icon`，可省略 `conditions` 和 `duplicate_policy`；JSON 行可省略所有可选字段。CSV/JSON 出现契约外字段时返回文件级列错误。

### 5.1 字段规则

| 字段 | 规则 |
|---|---|
| `action` | 必须为 `create`。 |
| `name` | 必填；`strip()` 后 1～200 字符。 |
| `category` | 可留空，留空映射为 `categoryid=0`；非空时按 `strip().casefold()` 唯一匹配未删除的 `userachcategorymodel.categoryname`。 |
| `description` | 可选；默认空字符串，最多 2000 字符。 |
| `coin` | 可选；默认 0，整数范围 0～2,147,483,647。 |
| `exp` | 可选；默认 0，整数范围 0～2,147,483,647。 |
| `icon` | 可选；默认空字符串，最多 500 字符；拒绝控制字符和 `javascript:`、`vbscript:`、`data:` 等可执行或内嵌协议。Task 9 只保存引用，不上传图片。 |
| `conditions` | 可省略或留空；任何非空内容返回 `ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED`。 |
| `duplicate_policy` | 无重复时空值归一为 `create`；有自定义成就同名或文件内同名时必须为 `skip` 或 `create`。 |

### 5.2 新记录固定值

批量新增记录使用以下固定值，与当前单条新增行为保持一致：

- `type=0`
- `achievementstatus=0`
- `currentvalue=0`
- `progress=0`
- `isdelete=0`
- `isgotreward=0`
- `rewardcoinvariable=0`
- `orderincategory=0`
- `createtime` 与 `updatetime` 使用同一当前毫秒时间
- `extrainfo` 不接受文件输入，沿用当前单条新增行为和数据库默认值

不创建 `unlockconditionmodel` 行。

### 5.3 重复和系统成就边界

- 自定义成就名称使用 `strip().casefold()` 比较。
- 已有自定义成就同名：预览标记为重复，要求逐行选择 `skip` 或 `create`。
- 文件内多行同名：所有相关行都标记为重复并要求策略。
- `skip` 是成功行，执行结果 `affected: 0`。
- `create` 真正新增，执行结果 `affected: 1`。
- 名称与 `achievementinfomodel.title` 保存的原始系统标识同名：返回 `SYSTEM_ACHIEVEMENT_NAME_CONFLICT`，该行不能执行，也不提供绕过策略。当前备份中的 `title` 可能是本地化键，Task 9 不猜测或改写其显示文案。

## 6. API 契约

新增接口：

- `GET /api/local/achievement-import-templates/csv`
- `GET /api/local/achievement-import-templates/json`
- `POST /api/local/achievement-import-files`

复用接口：

- `POST /api/local/batch-previews`
- `POST /api/local/batch-previews/<preview_token>/executions`

上传接口返回 Task 6 标准行结构：

```json
{
  "rows": [
    {
      "line": 2,
      "action": "create",
      "data": {
        "name": "筑基里程碑",
        "category": "里程碑",
        "description": "完成筑基",
        "coin": 88,
        "exp": 144,
        "icon": "golden-core.png",
        "conditions": "",
        "duplicate_policy": ""
      }
    }
  ]
}
```

cloud 请求返回 `403 LOCAL_WRITE_REQUIRES_LOCAL_SOURCE`，不得创建预览 token、快照或数据库记录。

## 7. 稳定错误

文件级错误使用 `ACHIEVEMENT_IMPORT_*`：

- `ACHIEVEMENT_IMPORT_FILE_REQUIRED`
- `ACHIEVEMENT_IMPORT_UNSUPPORTED_FORMAT`
- `ACHIEVEMENT_IMPORT_FILE_TOO_LARGE`
- `ACHIEVEMENT_IMPORT_INVALID_ENCODING`
- `ACHIEVEMENT_IMPORT_INVALID_CSV`
- `ACHIEVEMENT_IMPORT_INVALID_JSON`
- `ACHIEVEMENT_IMPORT_INVALID_COLUMNS`
- `ACHIEVEMENT_IMPORT_INVALID_ROWS`

逐行错误至少包括：

- `INVALID_ACTION`
- `INVALID_ACHIEVEMENT_NAME`
- `INVALID_ACHIEVEMENT_CATEGORY`
- `AMBIGUOUS_ACHIEVEMENT_CATEGORY`
- `INVALID_ACHIEVEMENT_DESCRIPTION`
- `INVALID_ACHIEVEMENT_COIN`
- `INVALID_ACHIEVEMENT_EXP`
- `INVALID_ACHIEVEMENT_ICON`
- `ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED`
- `SYSTEM_ACHIEVEMENT_NAME_CONFLICT`
- `DUPLICATE_ACHIEVEMENT_NAME`
- `DUPLICATE_POLICY_REQUIRED`
- `INVALID_DUPLICATE_POLICY`

任何错误行都会令 `can_execute=false`。错误必须显示字段名、文件行号和可执行的修正说明。

## 8. 页面设计

本地“成就管理”页增加“成就 CSV/JSON 批量导入”按钮。cloud 模式不显示可执行入口，并保留只读提示。

导入窗口包含：

- CSV 模板下载。
- JSON 模板下载。
- 文件选择器与当前文件名。
- 1 MiB、200 行、UTF-8 和“不支持解锁条件”的说明。
- “解析并预览”和“取消”按钮。

预览复用 Task 6 现有表格，显示：

- 文件行号。
- 成就名称和分类。
- 金币/经验奖励。
- 计划动作。
- 重复策略选择器。
- 逐行错误。

修改重复策略后必须重新请求预览，旧 token 随状态变化失效。页面离开、切换数据源、重新上传或执行完成后清空 Task 9 导入状态。

所有文件和数据库文本使用 `escHtml` / `escAttr` 或安全文本写入，不能拼接未经处理的事件属性或可执行 URL。

## 9. 数据库执行

新增一个只负责插入自定义成就的小 helper，供现有单条新增和 Task 9 执行器复用。SQL 必须参数化，并核对列数、占位符数和参数数一致。

执行顺序：

1. 验证 token、digest、数据源和预览状态。
2. 创建服务器快照。
3. `BEGIN IMMEDIATE`。
4. 逐行处理：`skip` 不写入；`create` 插入 `userachievementmodel`。
5. 返回与输入行号对应的逐行结果。
6. 全部成功后提交；任意异常回滚。

执行器不读取或写入 `achievementinfomodel` 与 `unlockconditionmodel`，系统成就名称只在预览阶段用于冲突检查。

## 10. 测试策略

严格按 RED → GREEN → REFACTOR：

1. RED：模板、上传边界和 CSV/JSON 解析测试。
2. GREEN：实现模板与解析接口。
3. RED：字段规范化、分类映射、系统冲突、条件阻止和重复策略测试。
4. GREEN：实现 `achievements/create` 预览。
5. RED：新增、`skip`、事务回滚、快照、cloud 隔离和逐行报告测试。
6. GREEN：实现插入 helper 与执行器。
7. RED/GREEN：成就页上传、预览、重复策略、状态清理和安全渲染测试。
8. REFACTOR：只清理 Task 9 直接产生的重复，不拆分大型文件。

定向测试文件：`tests/test_achievement_batch_import.py`。

完成前运行：

```powershell
python -m unittest tests.test_achievement_batch_import -v
python -m unittest discover -s tests -v
python -m py_compile server.py desktop_app.py tests\test_achievement_batch_import.py
git diff --check
```

修改 `index.html` 后额外用项目 `AGENTS.md` 中的 Node `vm.Script` 命令检查全部内联 JavaScript。

## 11. 真实工作副本验收

只使用 `/api/open-upload` 已创建的 `workspaces/browser-imports/` 托管副本，不直接写原始备份。

验收流程：

1. 记录当前工作副本和原始备份指纹。
2. 上传包含正常行、重复策略行和基础字段的测试 CSV。
3. 确认预览行号、字段、错误和计划动作。
4. 执行前确认自动快照已创建。
5. 执行后核对新成就字段、固定状态值，确认系统成就和条件表未变化。
6. 导出 ZIP 并验证 archive/database 完整性。
7. 恢复执行前快照，重新加载原托管副本。
8. 确认测试成就为 0、快照和临时文件已清理、浏览器无 warning/error。
9. 再次核对原始备份 SHA-256、大小和修改时间未变化。

## 12. 预计修改文件

- `server.py`
- `index.html`
- `tests/test_achievement_batch_import.py`
- `outputs/achievement_import_template.csv`
- `outputs/achievement_import_template.json`
- `tasks/plan.md`
- `tasks/todo.md`
- `outputs/LIFEUP_DASHBOARD_PRODUCT_ROADMAP.md`
- `outputs/LIFEUP_DASHBOARD_TASK9_HANDOFF_2026-07-17.md`

## 13. 后续任务

Task 10 再处理图标文件上传、浏览、失效引用检查和批量替换。成就条件批量导入需要单独设计，不随 Task 9 或 Task 10 自动扩展。
