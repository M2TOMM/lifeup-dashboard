# LifeUp Dashboard 项目协作规则

## 沟通与工作方式

- 默认使用简体中文；面向编程初学者，用简单、具体的语言说明结果和风险。
- 开始复杂任务前先给出 3～5 步短计划，执行期间简短汇报关键进展。
- 能从代码、测试或运行状态安全确认的事情直接确认，不要频繁询问用户。
- 代码审查先列问题、严重级别、影响和依据；用户明确要求修复后再修改。
- 修改前先阅读相关源码、测试和一处相似实现，避免凭交接文档或 README 猜测当前行为。
- 最终答复按“结果 → 修改内容 → 验证结果 → 下一步”组织，并提供可点击的完整文件路径。

审查严重级别统一为：

- `P0`：可能损坏/覆盖备份、误写手机数据、泄露 Token，或导致核心功能完全不可用，立即处理。
- `P1`：主要流程错误、数据源串用、安全校验缺失或稳定可复现的数据错误，本轮优先处理。
- `P2`：次要功能、可用性、兼容性或维护性问题，安排后续修复。
- `P3`：低风险优化和代码风格建议，不阻塞交付。

## 项目定位

这是一个 Flask + 原生 HTML/JavaScript + SQLite 的 LifeUp 电脑端管理工具，包含两种严格隔离的数据源：

- `local`：读取 LifeUp 备份 ZIP，在解压后的工作副本中管理数据，再显式导出 ZIP。
- `cloud`：读取手机云人升实时数据；当前已有数据保持只读，只允许走安全流程新增任务。

手机 LifeUp 是实时数据的事实来源。该项目不是双向同步工具，不得自动合并两种数据源。

## 开始任务前

1. 运行 `git status --short`，确认已有未提交改动；这些改动可能属于用户或其他任务，不得回滚或覆盖。
2. 优先阅读与任务直接相关的源码和测试：
   - `server.py`：Flask API、备份工作区、SQLite、云人升代理。
   - `index.html`：完整前端，包含 HTML、CSS 和原生 JavaScript。
   - `desktop_app.py`：pywebview 桌面壳。
   - `tests/`：当前回归测试和行为契约。
3. `docs/handoff_2026-07-05.md` 与 `docs/cloud-lifeup-handoff_2026-07-04.md` 只作背景资料；其中端口、行号、运行状态可能过时，必须从当前代码和实际服务重新确认。
4. `README.md` 仍可能落后于双数据源实现，不可把它单独当作事实来源。

## 不可突破的安全边界

### 原始备份

- 绝不修改、覆盖、移动或删除原始备份：`C:\Users\M2TO\Documents\LifeUp\LifeupBackup.zip`。
- 浏览器导入必须使用 `/api/open-upload` 创建的 `workspaces/browser-imports/` 工作副本。
- 本地增删改、手动验证和导出只针对工作副本；不要直接写手机 SQLite。
- 不调用 `/data/import`，也不要把云端数据写进本地备份。
- 测试产生的任务、商品或其他记录必须清理；最安全的做法是重新加载测试前的同一工作副本，并确认原始备份的修改时间未变化。

### 手机云人升

- 云端任务、商品、成就等已有数据默认只读；不得实现或调用编辑、删除、批量改写。
- 当前唯一允许的云端写入是“新增任务”，且必须经过：服务端 `preview_token` → 用户明确确认 → `idempotency_key` 幂等执行。
- 自动化测试必须 mock 云端写请求。除非用户明确要求并确认，不得向真实手机发送测试任务。
- 不得静默切换或混合 `local` / `cloud`。接口和页面必须遵守用户选择的 `source`；读取失败应明确报错。
- 只允许执行经过校验、以 `lifeup://api/` 开头的官方 URL。
- Token 只保存在进程内存中；`lifeup_cloud_config.json` 只能保存 Host 和端口。不要在文件、测试快照、日志或最终答复中泄露 Token。

## 修改代码的规则

- 保留与当前任务无关的内容；面对脏工作区，先查看 diff，再做最小范围修改。
- 修复缺陷时先稳定复现并定位根因；能写回归测试的，先增加失败测试，再修复实现。
- 修改前后端契约时同时检查 `server.py`、`index.html` 和相关测试，避免只修一侧。
- 所有 SQL 使用参数化查询；新增或修改 `INSERT` 时核对列数、占位符数和参数数一致。
- 前端渲染外部或数据库内容时使用安全文本写入/转义；禁止把未经处理的数据拼进 `innerHTML` 或事件属性。
- 图片 URL 仅允许项目支持的安全协议；拒绝 `javascript:` 等可执行协议。
- 不新增依赖，除非现有标准库或项目代码无法稳定完成任务；新增依赖前说明理由和维护成本。
- 不编辑 `dist/` 生成物，除非用户明确要求重新打包桌面版。

## 运行与浏览器验证

- 浏览器版默认从项目目录运行：`python server.py`，地址为 `http://127.0.0.1:5000/`。
- 先用 `Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue` 确认当前服务；不要仅依赖旧交接文档中的端口或 PID。
- 浏览器功能只通过 HTTP 地址验证，不使用 `file://.../index.html`；文件选择和后端 API 在 `file://` 下无法正常工作。
- 重启服务前先读取 `/api/status` 并记录当前工作副本路径。重启后继续加载同一工作副本，绝不改为加载原始备份。
- 浏览器验证至少检查：页面实际结果、控制台 error/warn、相关网络请求状态和数据源标识。

## 验证要求

先运行与改动最相关的测试；准备宣布完成前运行完整回归：

```powershell
python -m unittest discover -s tests -v
python -m py_compile server.py desktop_app.py
git diff --check
```

修改 `index.html` 中的 JavaScript 后，额外运行语法检查：

```powershell
$node = 'C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
@'
const fs = require('fs');
const vm = require('vm');
const path = 'C:/Users/M2TO/Documents/LifeUp/lifeup-dashboard/index.html';
const html = fs.readFileSync(path, 'utf8');
let script = '';
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
  script += match[1] + '\n';
}
new vm.Script(script, { filename: path });
console.log('frontend JS syntax ok');
'@ | & $node
```

验证不能止于退出码为 0：还要确认测试数量和结果、实际响应或 UI 行为、测试数据已清理、原始备份未变化。若某项无法运行，明确说明原因和未覆盖风险。

## 文件与交付物

- 临时日志、截图、测试产物放入 `work/`；备份工作副本放入 `workspaces/`。
- 用户要求的最终文档或其他交付文件放入 `outputs/`；项目自身的长期开发文档可放入 `docs/`。
- 不提交 `work/`、`workspaces/`、本地云配置、Token、数据库副本或用户备份。
- 除非用户明确要求，不执行 Git 提交、推送、发布或覆盖式打包。

## 完成检查清单

- 用户反馈的问题已真实复现并验证修复，而不是只完成代码修改。
- `local` 与 `cloud` 数据源仍保持隔离。
- 原始备份、手机已有数据和用户未提交改动均未受影响。
- 相关测试、完整回归、Python/JavaScript 语法检查和 `git diff --check` 已按改动范围执行。
- 手动测试数据已清理，服务仍加载正确的工作副本。
- 最终答复说明修改文件、验证证据、剩余风险和最简单的下一步。
