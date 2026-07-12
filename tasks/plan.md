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

- [ ] 创建快照后，`workspaces/snapshots/` 中存在独立、可校验的 ZIP。
- [ ] 原工作副本继续变化时，快照内容保持不变。
- [ ] 恢复不会覆盖原始备份或当前工作副本文件。

**Verification:**

- [ ] 自动化测试创建、列出、恢复和删除快照元数据。
- [ ] 手动修改一条测试任务后恢复快照，确认数据回到修改前并清理测试记录。

**Dependencies:** Task 2

**Files likely touched:** `server.py`, `index.html`, `tests/test_snapshots.py`

**Estimated scope:** M

### Task 4: 批量接口参数和事务校验

**Description:** 收紧任务、商品批量接口，禁止未知 action、非法 ID、非法价格和部分写入。

**Acceptance criteria:**

- [ ] 仅接受允许的 action 和正整数 ID 列表，限制单次批量数量。
- [ ] 商品价格等字段按业务范围校验，无效请求返回 400。
- [ ] 所有批量更新在一个事务内完成，失败时全部回滚。

**Verification:**

- [ ] 自动化覆盖未知 action、空列表、重复 ID、非数字 ID、非法价格和回滚。
- [ ] 现有任务/商品批量按钮继续正常工作。

**Dependencies:** None

**Files likely touched:** `server.py`, `tests/test_batch_validation.py`, `index.html`

**Estimated scope:** M

### Task 5: 本地核心 CRUD 回归矩阵

**Description:** 为任务、商品和成就的新增、编辑、删除建立可复用的临时数据库测试夹具。

**Acceptance criteria:**

- [ ] 三类实体的主要 CRUD 字段和软删除均有回归测试。
- [ ] 商品未知 `extrainfo`、任务奖励字段和成就类型得到保留。
- [ ] 测试不读取或修改用户真实备份。

**Verification:**

- [ ] `python -m unittest discover -s tests -v` 全部通过。
- [ ] 测试结束后无残留临时数据库或测试记录。

**Dependencies:** Task 4

**Files likely touched:** `tests/fixtures.py`, `tests/test_local_crud.py`, `server.py`

**Estimated scope:** M

## Checkpoint: 可信基础

- [ ] 所有测试、Python/JavaScript 语法检查和 `git diff --check` 通过。
- [ ] 原始备份修改时间未变化。
- [ ] 真实工作副本完成一次快照、修改、导出、重新打开和恢复演练。

## Phase 1: 批量管理工作台

### Task 6: 统一批量预览契约

**Description:** 建立任务、商品、成就共用的批量解析、错误表示、重复检测和执行报告结构。

**Acceptance criteria:**

- [ ] 预览结果统一包含行号、状态、错误、标准化数据和计划动作。
- [ ] 预览内容变更后旧执行令牌失效。
- [ ] 执行前自动创建快照，并返回逐行结果和汇总。

**Verification:**

- [ ] 契约测试覆盖全有效、部分错误、重复项和预览过期。
- [ ] UI 能清楚显示可执行行和阻止执行的错误行。

**Dependencies:** Tasks 3, 4

**Files likely touched:** `server.py`, `index.html`, `tests/test_local_batch_preview.py`

**Estimated scope:** M

### Task 7: 任务 CSV/JSON 批量管理

**Description:** 在统一契约上实现任务模板下载、批量新增和常用字段调整。

**Acceptance criteria:**

- [ ] 模板覆盖标题、分类、频率、目标次数、金币、经验、技能和冻结状态。
- [ ] 分类/技能支持名称映射，并提示不存在或歧义项。
- [ ] 潜在重复任务在预览中标出，由用户选择跳过或新增。

**Verification:**

- [ ] 自动化覆盖 CSV 编码、JSON、重复项和字段范围。
- [ ] 用临时工作副本完成 10 条任务预览、执行、导出和恢复。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_task_batch_import.py`, `outputs/task_import_template.csv`

**Estimated scope:** M

### Task 8: 商品批量管理

**Description:** 实现商品批量新增和改价，同时保留已有复杂效果元数据。

**Acceptance criteria:**

- [ ] 支持名称、分类、价格、库存、购买状态和基础效果。
- [ ] 批量改价支持固定值、增减值和百分比，并显示前后对比。
- [ ] 编辑已有商品不删除未识别的 `extrainfo` 内容。

**Verification:**

- [ ] 自动化覆盖三种改价方式、限制字段和元数据保留。
- [ ] 手动执行后恢复快照，确认可完整回退。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_item_batch_import.py`

**Estimated scope:** M

### Task 9: 自定义成就批量管理

**Description:** 实现自定义成就的模板、预览和批量新增，系统成就保持只读。

**Acceptance criteria:**

- [ ] 支持名称、分类、描述、奖励和图标字段。
- [ ] 系统成就或无法安全映射的条件结构不能进入执行列表。
- [ ] 每行执行结果可追溯到输入文件行号。

**Verification:**

- [ ] 自动化覆盖自定义/系统成就区分、字段验证和回滚。
- [ ] 手动创建测试成就后恢复快照并确认清理完成。

**Dependencies:** Task 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_achievement_batch_import.py`

**Estimated scope:** M

### Task 10: 图标资源管理器

**Description:** 建立备份内图标浏览、上传、搜索、引用检查和批量替换能力。

**Acceptance criteria:**

- [ ] 只允许安全图片类型、限制大小，并用生成文件名写入工作副本。
- [ ] 能找出数据库引用但文件不存在的图标，以及未被引用的文件。
- [ ] 批量替换先预览受影响实体，执行后可通过快照恢复。

**Verification:**

- [ ] 自动化覆盖伪装文件、路径穿越、重复文件名和引用检查。
- [ ] 导出后重新打开并确认新增图标正常显示。

**Dependencies:** Tasks 3, 6

**Files likely touched:** `server.py`, `index.html`, `tests/test_icon_manager.py`

**Estimated scope:** M

## Checkpoint: 批量管理

- [ ] 任务、商品、成就和图标均遵守统一预览流程。
- [ ] 任一批量功能失败时数据库没有部分写入。
- [ ] 每项批量操作都有快照和逐行报告。

## Phase 2: 真实目标和复盘

### Task 11: 真实宏愿映射

**Description:** 用用户配置的任务/成就分类映射生成长期目标进度，替换随机宏愿数据。

**Acceptance criteria:**

- [ ] 每个宏愿的进度可追溯到具体任务或成就。
- [ ] 映射配置与 LifeUp 数据分开保存，并明确标注来源。
- [ ] 没有配置时显示引导，不生成随机数字。

**Verification:**

- [ ] 自动化覆盖空配置、有效映射和已删除实体。
- [ ] 浏览器核对宏愿汇总与关联记录一致。

**Dependencies:** Task 5

**Files likely touched:** `server.py`, `index.html`, `tests/test_goals.py`

**Estimated scope:** M

### Task 12: 真实周复盘

**Description:** 从任务记录、番茄记录、经济数据和成就进度生成可追溯周报。

**Acceptance criteria:**

- [ ] 每个统计数字和建议都能展开查看来源记录。
- [ ] 数据不足时明确显示缺少什么，不使用模拟数据补齐。
- [ ] 本地和云端报告分别计算，不混合数据。

**Verification:**

- [ ] 固定数据夹具产生稳定、可重复的周报结果。
- [ ] 浏览器验证来源标签和明细展开。

**Dependencies:** Task 11

**Files likely touched:** `server.py`, `index.html`, `tests/test_review.py`

**Estimated scope:** M

### Task 13: 日常与番茄真实热力图

**Description:** 替换日常页面模拟热力图，并校准本地/云端番茄记录字段。

**Acceptance criteria:**

- [ ] 热力图只使用真实完成记录。
- [ ] 不同字段缺失时有兼容映射或明确提示。
- [ ] 支持日、周、月三个时间范围。

**Verification:**

- [ ] 自动化覆盖时区边界、缺字段和空数据。
- [ ] 用实际工作副本抽查一天记录与 LifeUp 显示一致。

**Dependencies:** Task 5

**Files likely touched:** `server.py`, `index.html`, `tests/test_daily_focus.py`

**Estimated scope:** M

## Checkpoint: 真实控制台

- [ ] “大道宏愿”“道行复盘”和日常热力图不再使用随机数据。
- [ ] 所有页面明确显示数据源和最后刷新时间。
- [ ] 统计结果能够追溯到原始记录。

## Phase 3: 云端与交付

### Task 14: 云端连接诊断和渐进加载

**Description:** 细化连接错误，给慢接口增加进度、局部成功和短时内存缓存。

**Acceptance criteria:**

- [ ] 用户能区分配置、网络、认证、超时和响应格式问题。
- [ ] 成就按分类显示进度，单类失败不清空成功数据。
- [ ] 缓存只在内存中，并显示来源和刷新时间。

**Verification:**

- [ ] mock 覆盖各类错误、局部失败和缓存过期。
- [ ] 浏览器验证加载进度、重试和错误提示。

**Dependencies:** Task 5

**Files likely touched:** `server.py`, `index.html`, `tests/test_cloud_resilience.py`

**Estimated scope:** M

### Task 15: 云端新增任务体验和操作记录

**Description:** 用实时分类/技能选择器替代手填 ID，并记录不含 Token 的执行报告。

**Acceptance criteria:**

- [ ] 分类和技能可搜索选择，数据来自当前手机连接。
- [ ] CSV 错误精确到行和字段，成功结果逐条展示。
- [ ] 操作记录不包含 Token，重复执行仍受幂等保护。

**Verification:**

- [ ] 所有自动化测试 mock 真实云端写入。
- [ ] 经用户明确确认后，仅用一个测试任务做真实手机冒烟验证。

**Dependencies:** Task 14

**Files likely touched:** `server.py`, `index.html`, `tests/test_cloud_task_composer.py`

**Estimated scope:** M

### Task 16: 文档、工作区维护和桌面发布

**Description:** 更新说明文档，增加可预览的工作区清理，并完成桌面版发布冒烟测试。

**Acceptance criteria:**

- [ ] README 准确描述双数据源、工作副本和安全导出流程。
- [ ] 清理功能先预览，只删除用户确认的托管临时文件。
- [ ] 发布包不含备份、Token、配置、工作区或日志。

**Verification:**

- [ ] 在干净环境完成打开、导入、修改、快照、导出和重新打开。
- [ ] 完整测试、语法检查、`git diff --check` 和浏览器控制台检查通过。

**Dependencies:** Tasks 2, 3, 10, 15

**Files likely touched:** `README.md`, `desktop_app.py`, `.gitignore`, `docs/USER_GUIDE.md`, `tools/`

**Estimated scope:** M

## Checkpoint: 产品可交付

- [ ] 所有阶段验收标准通过。
- [ ] 原始备份和手机已有数据未被自动修改。
- [ ] 新用户只看文档即可完成一次安全管理闭环。

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

按顺序执行 Task 1、Task 2、Task 3。完成“安全导入 → 原子导出 → 真快照”后，在检查点向用户展示一次完整恢复演练，再继续批量工作台。
