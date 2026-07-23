# Implementation Plan: LifeUp Dashboard 持续完善

## Overview

本计划把 LifeUp Dashboard 从“功能很多的本地工具”推进为“可信赖的 LifeUp 电脑端管理产品”。实施顺序遵循：备份安全和恢复能力 → 统一批量框架 → 真实数据洞察 → 云端体验 → 桌面发布。每项任务都形成可独立验证的纵向功能切片。

## Architecture Decisions

- 本地备份和手机云端继续作为两个严格隔离的数据源；不建设同步引擎。
- 所有本地写入只修改解压后的工作副本，最终通过新 ZIP 导出。
- 所有高影响批量操作采用“校验 → 预览 → 快照 → 事务执行 → 报告”的统一流程。
- 云端已有数据继续只读；新增任务保持 `preview_token` + 用户确认 + `idempotency_key`。
- 保留 Flask + 原生 HTML/JavaScript 技术栈；不在本轮进行框架迁移。
- 演示数据和真实数据必须在数据层和界面上明确分开。

## Phase 0: 可信基础

### Task 1: 备份导入安全校验

**Description:** 在解压前验证 ZIP 结构、条目路径、文件大小和数据库存在性，确保无效文件不会破坏当前已加载工作区。

**Acceptance criteria:**

- [x] 拒绝路径穿越、缺少 `databases/LifeUpDB.db`、损坏或超限的 ZIP。
- [x] 验证失败后，原先加载的工作副本和页面状态保持可用。
- [x] 返回初学者能看懂的错误原因和处理建议。

**Verification:**

- [x] 新增恶意路径、缺库、损坏 ZIP 的自动化测试。
- [x] 浏览器选择错误文件后仍能继续查看此前备份。

**Dependencies:** None

**Files likely touched:** `server.py`, `tests/test_backup_validation.py`, `index.html`

**Estimated scope:** M

### Task 2: 原子导出和完整性验证

**Description:** 导出先写临时 ZIP，验证归档和 SQLite 完整性，再生成最终文件；浏览器流程明确拒绝覆盖原始备份。

**Acceptance criteria:**

- [x] 导出中断或验证失败时，不留下伪装成成功文件的损坏 ZIP。
- [x] 导出的 ZIP 可重新加载，数据库通过 `PRAGMA integrity_check`。
- [x] 响应返回路径、大小、时间和校验结果。

**Verification:**

- [x] 自动化覆盖成功、写入失败、完整性失败和原始路径拒绝。
- [x] 手动完成“导出 → 重新打开 → 核对任务数量”的闭环。

**Dependencies:** Task 1

**Files likely touched:** `server.py`, `tests/test_backup_export.py`, `index.html`

**Estimated scope:** M

### Task 3: 真正的快照和恢复

**Description:** 用服务端真实 ZIP 副本替换当前仅记录路径的浏览器快照，并在恢复时创建新的工作副本。

**Acceptance criteria:**

- [x] 创建快照后，`workspaces/snapshots/` 中存在独立、可校验的 ZIP。
- [x] 原工作副本继续变化时，快照内容保持不变。
- [x] 恢复不会覆盖原始备份或当前工作副本文件。

**Verification:**

- [x] 自动化测试创建、列出、恢复和删除快照元数据。
- [x] 手动修改一条测试任务后恢复快照，确认数据回到修改前并清理测试记录。

**Dependencies:** Task 2

**Files likely touched:** `server.py`, `index.html`, `tests/test_snapshots.py`

**Estimated scope:** M

### Task 4: 批量接口参数和事务校验

**Description:** 收紧任务、商品批量接口，禁止未知 action、非法 ID、非法价格和部分写入。

**Acceptance criteria:**

- [x] 仅接受允许的 action 和正整数 ID 列表，限制单次批量数量。
- [x] 商品价格等字段按业务范围校验，无效请求返回 400。
- [x] 所有批量更新在一个事务内完成，失败时全部回滚。

**Verification:**

- [x] 自动化覆盖未知 action、空列表、重复 ID、非数字 ID、非法价格和回滚。
- [x] 现有任务/商品批量按钮继续正常工作。

**Dependencies:** None

**Files likely touched:** `server.py`, `tests/test_batch_validation.py`, `index.html`

**Estimated scope:** M

### Task 5: 本地核心 CRUD 回归矩阵

**Description:** 为任务、商品和成就的新增、编辑、删除建立可复用的临时数据库测试夹具。

**Acceptance criteria:**

- [x] 三类实体的主要 CRUD 字段和软删除均有回归测试。
- [x] 商品未知 `extrainfo`、任务奖励字段和成就类型得到保留。
- [x] 测试不读取或修改用户真实备份。

**Verification:**

- [x] `python -m unittest discover -s tests -v` 全部通过。
- [x] 测试结束后无残留临时数据库或测试记录。

**Dependencies:** Task 4

**Files likely touched:** `tests/fixtures.py`, `tests/test_local_crud.py`, `server.py`

**Estimated scope:** M

## Checkpoint: 可信基础

- [x] 所有测试、Python/JavaScript 语法检查和 `git diff --check` 通过。
- [x] 原始备份修改时间未变化。
- [x] 真实工作副本完成一次快照、修改、导出、重新打开和恢复演练。

## Phase 1: 批量管理工作台

### Task 6: 统一批量预览契约

**Description:** 建立任务、商品、成就共用的批量解析、错误表示、重复检测和执行报告结构。

**Acceptance criteria:**

- [x] 预览结果统一包含行号、状态、错误、标准化数据和计划动作。
- [x] 预览内容变更后旧执行令牌失效。
- [x] 执行前自动创建快照，并返回逐行结果和汇总。

**Verification:**

- [x] 契约测试覆盖全有效、部分错误、重复项和预览过期。
- [x] UI 能清楚显示可执行行和阻止执行的错误行。
- [x] 独立规格/规范审查通过；越界 ID、异常 digest 和令牌清理边界均有回归测试。
- [x] 托管工作副本完成浏览器预览、执行、自动快照、恢复和产物清理。

**Dependencies:** Tasks 3, 4

**Files likely touched:** `server.py`, `index.html`, `tests/test_local_batch_preview.py`

**Estimated scope:** M

### Task 7: 任务 CSV/JSON 批量管理

**Description:** 在统一契约上实现任务模板下载、批量新增和常用字段调整。

**Acceptance criteria:**

- [x] 模板覆盖标题、分类、频率、目标次数、金币、经验、技能和冻结状态。
- [x] 分类/技能支持名称映射，并提示不存在或歧义项。
- [x] 潜在重复任务在预览中标出，由用户逐行选择跳过或仍然新增。
- [x] 执行复用 Task 6 的一次性 token、digest、自动快照和单 SQLite 事务。

**Verification:**

- [x] 自动化覆盖 CSV/JSON、编码与大小、名称映射、重复项、字段范围、回滚和 cloud 隔离。
- [x] 用托管工作副本完成 10 条任务预览、执行、导出和恢复，并清理本轮产物。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_task_batch_import.py`, `outputs/task_import_template.csv`

**Estimated scope:** M

### Task 8: 商品批量管理

**Description:** 实现商品批量新增和改价，同时保留已有复杂效果元数据。

**Acceptance criteria:**

- [x] 支持名称、分类、价格、库存、购买状态和基础效果。
- [x] 批量改价支持固定值、增减值和百分比，并显示前后对比。
- [x] 编辑已有商品不删除未识别的 `extrainfo` 内容。

**Verification:**

- [x] 自动化覆盖三种改价方式、限制字段和元数据保留。
- [x] 在托管工作副本执行新增与三种改价后恢复快照，确认可完整回退并清理本轮产物。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_item_batch_import.py`

**Estimated scope:** M

### Task 9: 自定义成就批量管理

**Description:** 实现自定义成就的模板、预览和批量新增，系统成就保持只读。

**Acceptance criteria:**

- [x] 支持名称、分类、描述、奖励和安全图标引用字段。
- [x] 系统成就或无法安全映射的条件结构不能进入执行列表。
- [x] 每行执行结果可追溯到输入文件行号。

**Verification:**

- [x] 自动化覆盖自定义/系统成就区分、字段验证和回滚。
- [x] 手动创建测试成就后恢复快照并确认清理完成。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_achievement_batch_import.py`

**Estimated scope:** M

### Task 10: 图标资源管理器

**Description:** 建立备份内图标浏览、上传、搜索、引用检查和批量替换能力。

**Acceptance criteria:**

- [x] 只允许 PNG/JPEG/GIF/WebP、限制为 5 MiB，并用内容摘要生成文件名写入工作副本。
- [x] 能找出数据库引用但文件不存在的图标、无法识别内容、扩展名与真实格式不一致以及无直接引用文件。
- [x] 批量替换先预览受影响商品/自定义成就，执行前自动快照，执行后可恢复。
- [x] 导出前强制检查直接本地图标引用；缺失文件或无法识别内容会阻止导出，但不阻止先创建快照。

**Verification:**

- [x] 自动化覆盖伪装/截断文件、路径穿越、同名不覆盖、内容去重、引用检查、cloud 隔离和导出阻止。
- [x] 真实工作副本完成上传、引用替换、导出 ZIP/SQLite 校验、快照恢复和重新加载闭环。

**Dependencies:** Tasks 3, 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_icon_manager.py`

**Estimated scope:** M

## Checkpoint: 批量管理

- [x] 任务、商品、成就和图标均遵守统一预览流程。
- [x] 任一批量功能失败时数据库没有部分写入。
- [x] 每项批量操作都有快照和逐行报告。

## Phase 2: 真实目标和复盘

### Task 11: 真实宏愿映射

**Description:** 用用户配置的任务/成就分类映射生成长期目标进度，替换随机宏愿数据。

**Acceptance criteria:**

- [x] 每个宏愿的进度可追溯到具体任务或成就。
- [x] 映射配置与 LifeUp 数据分开保存，并明确标注来源。
- [x] 没有配置时显示引导，不生成随机数字。

**Verification:**

- [x] 自动化覆盖空配置、有效映射和已删除实体。
- [x] 浏览器核对宏愿汇总与关联记录一致。

**Dependencies:** Task 5

**Files touched:** `server.py`, `index.html`, `.gitignore`, `tests/test_goals.py`, `tests/test_demo_page_labels.py`

**Estimated scope:** M

### Task 12: 真实周复盘

**Description:** 从任务记录、番茄记录、经济数据和成就进度生成可追溯的自然周、自然月和自然年复盘。

**Acceptance criteria:**

- [x] 每个统计数字和观察都能展开查看本期与对比期来源记录。
- [x] 数据不足时明确显示缺少什么，不使用模拟数据补齐。
- [x] 本地和云端报告分别计算，不混合数据。

**Verification:**

- [x] 固定数据夹具产生稳定、可重复的周/月/年复盘结果。
- [x] 浏览器验证周/月/年窗口、来源标签、明细展开和空数据提示。

**Dependencies:** Task 11

**Files touched:** `server.py`, `index.html`, `tests/test_review.py`, `tests/test_demo_page_labels.py`, `docs/decisions/ADR-001-real-review-statistics.md`

**Estimated scope:** M

### Task 13: 日常与番茄真实热力图

**Status:** 已完成（2026-07-18）

**Description:** 已用真实每日任务完成和番茄记录替换日常页面模拟热力图，并校准本地/云端时间与时长字段。

**Acceptance criteria:**

- [x] 热力图只使用真实完成记录。
- [x] 不同字段缺失时有兼容映射或明确提示。
- [x] 支持日、周、月三个时间范围。

**Verification:**

- [x] 自动化覆盖时区边界、缺字段、空数据、秒/毫秒/ISO 时间和番茄时长兼容。
- [x] 实际工作副本的日/周/月 API、页面空状态与复盘统计一致，来源和刷新时间可见。

**Dependencies:** Task 5

**Files touched:** `server.py`, `index.html`, `tests/test_daily_focus.py`, `docs/decisions/ADR-002-real-activity-heatmap.md`

**Estimated scope:** M

## Checkpoint: 真实控制台

- [x] “大道宏愿”“道行复盘”和日常热力图不再使用随机数据。
- [x] 所有页面明确显示数据源和最后刷新时间。
- [x] 统计结果能够追溯到原始记录。

## Phase 3: 云端与交付

### Task 14: 云端连接诊断和渐进加载

**Status:** 已完成（2026-07-18）

**Description:** 细化连接错误，给慢接口增加进度、局部成功和短时内存缓存。

**Acceptance criteria:**

- [x] 用户能区分配置、网络、认证、超时和响应格式问题。
- [x] 成就按分类显示进度，单类失败不清空成功数据。
- [x] 缓存只在内存中，并显示来源和刷新时间。

**Verification:**

- [x] mock 覆盖各类错误、局部失败和缓存过期。
- [x] 浏览器验证加载进度、重试、缓存来源和错误提示。

**Dependencies:** Task 5

**Files touched:** `server.py`, `index.html`, `tests/test_cloud_resilience.py`, `docs/decisions/ADR-003-cloud-resilience-progressive-loading.md`

**Estimated scope:** M

### Task 15: 云端新增任务体验和操作记录

**Description:** 用实时分类/技能选择器替代手填 ID，并记录不含 Token 的执行报告。

**Acceptance criteria:**

- [x] 分类和技能可搜索选择，数据来自当前手机连接。
- [x] CSV 错误精确到行和字段，成功结果逐条展示。
- [x] 操作记录不包含 Token，重复执行仍受幂等保护。

**Verification:**

- [x] 所有自动化测试 mock 真实云端写入。
- [ ] 经用户明确确认后，仅用一个测试任务做真实手机冒烟验证。

本轮已用真实手机连接完成分类/技能读取、单条预览和错误 CSV 浏览器验收；没有获得真实写入确认，因此没有点击执行按钮。mock 自动化和预览只读验收已覆盖核心安全契约，真实手机写入冒烟保留为可选人工验证，不阻塞 Task 15 功能完成。

**Dependencies:** Task 14

**Files touched:** `server.py`, `index.html`, `tests/test_cloud_task_composer.py`, `tests/test_cloud_preview_execution.py`

**Estimated scope:** M

### Task 16: 文档、工作区维护和桌面发布

**Status:** 已完成（2026-07-18）

**Description:** 更新说明文档，增加可预览的工作区清理，并完成桌面版发布冒烟测试。

**Acceptance criteria:**

- [x] README 准确描述双数据源、工作副本和安全导出流程。
- [x] 清理功能先预览，只删除用户确认的托管临时文件。
- [x] 发布包不含备份、Token、配置、工作区或日志。

**Verification:**

- [x] 在隔离桌面数据目录完成打开、导入、修改、快照、导出和重新打开。
- [x] 完整测试、语法检查、`git diff --check` 和浏览器控制台检查通过。

**Dependencies:** Tasks 2, 3, 10, 15

**Files likely touched:** `README.md`, `desktop_app.py`, `.gitignore`, `docs/USER_GUIDE.md`, `tools/`

**Estimated scope:** M

## Checkpoint: 产品可交付

- [x] 所有阶段验收标准通过。
- [x] 原始备份和手机已有数据未被自动修改。
- [x] 新用户只看文档即可完成一次安全管理闭环。

## Phase 4: AI 接入

### Task 17: LifeUp MCP 适配层

**Description:** 在现有 Flask HTTP API 外增加轻量 MCP 适配层，让 Codex、Claude 等客户端能够安全查询 LifeUp 数据，并复用现有安全流程新增任务；不重复实现数据库、统计或云端业务逻辑。

**Acceptance criteria:**

- [x] MCP 工具仅调用现有 Flask API，不直接打开或修改 LifeUp SQLite、备份 ZIP 和本地映射文件。
- [x] 第一版提供状态、任务、商品、成就、仪表盘、专注和宏愿等只读查询工具，并为每个工具定义稳定的输入/输出结构。
- [x] 新增任务拆分为预览与确认执行工具，复用服务端 `preview_token`、用户明确确认和 `idempotency_key`，不绕过现有安全校验。
- [x] 每次调用必须显式传递 `local` 或 `cloud`；不自动切换、不混合数据源，cloud 不可用时明确返回错误。
- [x] 不开放编辑、删除、批量写入或手机已有数据修改；不在 MCP 响应、日志或配置中泄露 Token。
- [x] 提供适用于 Windows PowerShell 的安装、启动和 Codex/Claude 客户端配置说明。

**Verification:**

- [x] 使用 mock Flask API 覆盖工具发现、参数校验、HTTP 错误、超时、数据源隔离和敏感信息隐藏。
- [x] 对本地运行中的 Dashboard 做只读集成测试，核对 MCP 结果与同参数 HTTP API 响应一致。
- [x] 新增任务自动化测试全部 mock 写入；未向真实手机发送测试任务。
- [x] MCP 服务停止、客户端断开或重复调用时，不修改原始备份、不产生部分写入，也不重复创建任务。

**Dependencies:** Tasks 14, 15, 16

**Files likely touched:** `mcp_server.py`, `tests/test_mcp_server.py`, `README.md`, `docs/USER_GUIDE.md`

**Estimated scope:** M

## Checkpoint: AI 安全接入

- [x] MCP 层只负责协议转换，业务规则仍以 Flask API 为唯一实现。
- [x] 只读工具和安全新增任务流程可由标准 MCP stdio 客户端发现并调用。
- [x] 原始备份、手机已有数据、Token 和本地配置均未受影响或泄露。

## Phase 5: 正确性维护

### Task 18: 修正任务完成状态口径

**Status:** 已完成（2026-07-23）

**Description:** LifeUp 的任务状态为 `0=待完成`、`1=已完成`、`2=已放弃`。Dashboard 多处使用 `taskstatus >= 1` 或等价 Python 判断，导致已放弃任务被计入完成。本任务统一所有本地和云端完成语义，不扩大写入范围。

**Acceptance criteria:**

- [x] 首页统计和任务“已完成”筛选只包含状态 `1`。
- [x] 宏愿、复盘、热力图、历史、经济和历史导出都排除状态 `2`。
- [x] 云端列表和首页统计与本地使用相同语义，状态 `2` 不计入进行中或已完成。
- [x] 成就状态判断保持原有规则，不与任务状态混用。
- [x] 自动化测试包含状态 `0`、`1`、`2` 同时存在的本地和云端样本。

**Verification:**

- [x] 修复前的最小回归测试稳定失败，显示 Dashboard `done=2`、正确值应为 `1`。
- [x] 21 项任务状态、复盘、热力图和宏愿定向测试通过。
- [x] 217 项完整回归通过。
- [x] Python 语法检查和 `git diff --check` 通过。

**Dependencies:** Tasks 11, 12, 13, 17

**Files touched:** `server.py`, `tests/test_task_status_semantics.py`, `tests/test_review.py`, `tests/test_daily_focus.py`, `tests/test_goals.py`, `docs/decisions/ADR-001-real-review-statistics.md`

**Estimated scope:** S

## Phase 6: 仓库安全维护

### Task 19: 仓库整理与安全提交准备

**Status:** 已完成（2026-07-23；后续已按用户授权完成三步本地提交，未 push）

**Description:** 整理 Task 5～18 累积的未提交工作，收紧 Git 忽略规则，区分源码、测试、长期文档与用户交付物，并在不暂存、不提交、不推送的前提下生成可审查的候选提交清单。

**Acceptance criteria:**

- [x] `.gitignore` 排除备份和发布 ZIP、`outputs/` 新交付物、`work/`、`workspaces/`、数据库、日志、Token、本地配置与临时构建产物。
- [x] 已跟踪的历史文件保持原样，不删除、不取消跟踪，也不覆盖用户已有改动。
- [x] 候选清单只包含源码、测试、工具和长期项目文档；“LifeUp 云备份成长伴侣”和个人恢复/导入交付物不进入候选。
- [x] 完整回归、Python/JavaScript 语法检查、`git diff --check`、现有桌面 ZIP 内容审计和原始备份指纹核对通过。
- [x] 没有执行 `git add`、commit、push、reset、发布或真实手机写入。

**Verification:**

- [x] 217 项完整回归通过。
- [x] Python 与前端 JavaScript 语法检查通过。
- [x] `git diff --check` 退出码为 0。
- [x] 现有 `LifeUpDashboard-1.2.0-windows-x64.zip` 内容审计通过，共检查 3 个条目。
- [x] 原始备份大小、修改时间和 SHA-256 与 Task 18 交接一致。

**Files touched:** `.gitignore`, `tasks/plan.md`, `tasks/todo.md`, `outputs/LIFEUP_DASHBOARD_PRODUCT_ROADMAP.md`

**Estimated scope:** S

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LifeUp 不同版本数据库字段不同 | High | 用真实备份夹具和字段兼容层，不凭单一版本硬编码 |
| ZIP 或媒体文件不可信 | High | 解压前校验路径、类型、大小和总量 |
| 批量操作产生部分写入 | High | 事务、快照、预览令牌和逐行报告 |
| 云端网络不稳定 | Medium | 明确错误分类、重试入口、局部成功和短时缓存 |
| `index.html` 单文件继续膨胀 | Medium | 暂不重写；新增功能先按模块函数和测试组织，达到拆分阈值后再单独规划 |
| 演示数据被误认成真实数据 | Medium | 未接入前保持显眼标签，接入后删除随机数据路径 |
| 脏工作区覆盖用户改动 | High | 每次任务先看 `git status` 和局部 diff，不做回滚式操作 |

## Open Questions

- 宏愿优先使用“任务分类”“成就分类”还是单独的本地映射？建议先做可配置映射，不改 LifeUp 数据结构。
- 批量导入遇到错误行时，是整批阻止还是执行有效行？建议默认整批阻止，之后提供显式“仅执行有效行”。
- 工作副本和快照默认保留多少份？建议先按数量保留最近 10 份，并在清理前预览。
- 桌面版是否需要自动检测更新？建议首个稳定版暂不做，先保证可重复打包和手动更新。

## Recommended Start

Task 19“仓库整理与安全提交准备”和后续三步本地提交均已完成，尚未 push 或发布。下一步可在单独取得授权后 push，或先重建包含成就子分类、MCP 和 Task 18 正确统计口径的新桌面版。
