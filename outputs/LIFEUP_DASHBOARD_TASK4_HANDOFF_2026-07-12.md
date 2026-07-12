# LifeUp Dashboard Task 4 交接（2026-07-12）

## 结果

Task 4“批量接口参数和事务校验”已完成。

本轮收紧了本地任务、商品和任务冻结批量接口，补齐了请求边界、目标存在性校验、事务回滚和前端输入拦截。浏览器实测还发现并修复了商品接口使用虚构字段 `purchasable` / `updatetime` 的问题；真实 `shopitemmodel` 使用 `isdisablepurchase`，且没有 `updatetime`。

不要勾选 Phase 0 总检查点：Task 5“本地核心 CRUD 回归矩阵”尚未完成。

## 分阶段提交

- `f359d02 fix: 收紧本地批量接口参数边界`
- `374646d fix: 保证本地批量更新完整回滚`
- 阶段 3 的前端校验、真实字段修复、浏览器验收和本交接文档位于本文件所在提交。

## 当前批量接口契约

### 公共规则

- 请求体必须是 JSON 对象。
- `ids` 必须是非空、无重复的正整数列表；布尔值、字符串、小数、0 和负数均拒绝。
- 单次最多处理 200 条。
- 所有 ID 必须真实存在；混入不存在 ID 时整批返回 400，已有记录不修改。
- 数据库操作使用 `BEGIN IMMEDIATE → 校验目标 → UPDATE → commit`；校验或数据库异常会 rollback。

### 任务

- `/api/tasks/batch` 仅允许：`disable`、`enable`、`delete`、`freeze`、`unfreeze`。
- `/api/tasks/batch/freeze` 要求 `isfrozen` 为真正的 JSON 布尔值。
- 冻结接口可使用 `ids` 或正整数 `groupid`，不能同时提供；分组为空或超过 200 条时拒绝。

### 商品

- `/api/items/batch` 仅允许：`disable`、`enable`、`delete`、`price`。
- `price` 必须是 `0～2147483647` 的整数。
- 停用写入 `isdisablepurchase=1`，启用写入 `isdisablepurchase=0`。
- 商品表没有 `updatetime`，批量 SQL 不再写入这个虚构字段。

### 前端

- 任务和商品批量操作在发送 API 前检查空选择、200 条上限和正整数 ID。
- 批量改价拒绝空值、负数、小数和超过 `2147483647` 的值。
- 价格输入框增加 `min=0`、`max=2147483647`、`step=1`。

## 自动化验证

最终门禁（2026-07-12）：

- `python -m unittest discover -s tests -v`：81/81 通过。
- `python -m py_compile server.py desktop_app.py`：通过。
- `index.html` 内全部 JavaScript 使用 Node `vm.Script` 检查：通过。
- `git diff --check`：通过，仅有 Windows LF/CRLF 提示。

新增覆盖：

- 未知或缺失 action。
- 空 ID、重复 ID、字符串/小数/布尔/非正整数 ID。
- 超过 200 条的批次。
- 商品价格缺失、布尔、字符串、小数、负数、超上限及边界值。
- 混入不存在目标时整批拒绝。
- SQLite trigger 在第二条记录更新时强制失败，任务和商品均证明完整回滚。
- 所有允许 action 的字段结果。
- 冻结专用接口的 ID、状态、分组和成功路径。
- 前端超量批次和非法价格不会调用 API。

## 浏览器验收和清理

HTTP 页面：`http://127.0.0.1:5000/`

实测过程：

1. 任务 ID `588` 通过批量按钮冻结，页面提示“❄️ 已冻结 1 项”。
2. 首次商品 ID `281` 批量停用暴露真实错误 `no such column: purchasable`。
3. 修正真实字段映射并重启服务后，同一商品批量停用提示“✅ 已处理 1 项”，服务器记录 `POST /api/items/batch` 200。
4. 重新加载测试前的同一 `browser-imports` ZIP，确认任务 588 的 `isfrozen=0`、商品 281 的 `isdisablepurchase=0`。
5. 页面最终回到目标控制台，浏览器控制台 0 条 warning/error。

当前服务：

- 监听：`127.0.0.1:5000`
- PID：`42664`（时间相关，继续工作前重新确认）
- 当前工作副本：`C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard\workspaces\browser-imports\20260711-104636-512c4541-LifeupBackup.zip`

## 原始备份安全边界

绝不修改、覆盖、移动或删除：

`C:\Users\M2TO\Documents\LifeUp\LifeupBackup.zip`

清理后指纹：

- SHA-256：`D794C78B277F68F1AE60A1F6F06E981C8A758F88C91C350F637A282741A46D46`
- 大小：43,070,521 字节
- UTC mtime：`2026-07-02T14:13:27Z`

与 Task 4 开始前一致。

## 主要文件

- `server.py`：批量请求校验、目标校验、事务和真实商品字段。
- `index.html`：前端批量数量、ID 和价格校验。
- `tests/test_batch_validation.py`：后端契约、真实表结构和回滚测试。
- `tests/test_batch_ui.py`：前端请求拦截测试。
- `tasks/todo.md`、`tasks/plan.md`：Task 4 验收状态。

## 下一步：Task 5

目标是建立本地核心 CRUD 回归矩阵，不扩大到 Phase 1 的批量导入：

1. 先从真实备份结构提取最小但可信的任务、商品、成就夹具。
2. 为新增、读取、修改、删除各写一条端到端 API 回归，验证关联表和未知元数据不丢失。
3. 每个测试使用独立临时数据库，结束后确认无测试记录或数据库副本残留。
4. 完成后再运行完整回归、Python/JavaScript 语法检查、`git diff --check` 和浏览器冒烟测试。
5. Task 5 完成后，才评估并勾选 Phase 0 总检查点。
