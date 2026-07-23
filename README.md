# LifeUp Dashboard

LifeUp Dashboard 是一个 Windows 本机工具，用来在电脑上管理 LifeUp 备份，并查看手机云人升的实时数据。项目使用 Flask、原生 HTML/JavaScript、SQLite 和 pywebview。

它不是双向同步工具。两种数据源始终分开：

| 数据源 | 用途 | 当前写入范围 |
|---|---|---|
| 本地备份 `local` | 打开 ZIP 的托管工作副本，管理任务、商品、自定义成就、图标、快照和统计 | 只修改解压后的工作副本，最后导出新的 ZIP |
| 手机云人升 `cloud` | 读取手机实时任务、商品、成就、统计和分类/技能 | 已有数据只读；只允许经过预览和确认后新增任务 |

## 安全原则

- 不修改、覆盖、移动或删除你选中的原始 LifeUp 备份。
- 浏览器上传会先复制到 `workspaces/browser-imports/`，后续编辑只发生在工作副本中。
- 导出总是生成新的 ZIP，并在发布前检查 ZIP 和 SQLite 完整性。
- 本地批量操作遵循“校验 → 预览 → 快照 → 单事务执行 → 报告”。
- 手机已有数据只读；新增任务必须经过预览令牌、用户确认和幂等执行。
- 手机 Token 只保存在当前服务进程内存中，不写入配置、日志或发布包。

## 快速开始

### 浏览器版

在 Windows PowerShell 中运行：

```powershell
cd C:\Users\你的用户名\Documents\LifeUp\lifeup-dashboard
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

然后打开 [http://127.0.0.1:5000/](http://127.0.0.1:5000/)。不要直接双击 `index.html`；`file://` 页面无法使用文件选择和后端 API。

### 桌面版开发运行

```powershell
cd C:\Users\你的用户名\Documents\LifeUp\lifeup-dashboard
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop_app.py
```

桌面版把配置、工作副本、快照、导出和日志放在 `%LOCALAPPDATA%\LifeUpDashboard`，不会写进 EXE 或其临时解压目录。

## 第一次安全使用

1. 从 LifeUp App 导出一个 ZIP 备份，并保留原文件不动。
2. 启动 Dashboard，保持数据源为“本地备份”。
3. 点击“打开存档”→“选择文件”。浏览器版会自动创建托管工作副本。
4. 修改前先到“快照管理”创建快照。
5. 完成修改后点击“导出备份”，得到新的 ZIP。
6. 在 Dashboard 中重新打开这个导出 ZIP，核对任务、商品或成就数量。
7. 确认无误后，再由你手动在手机 LifeUp 中恢复新 ZIP。

完整操作说明见 [用户指南](docs/USER_GUIDE.md)。

## 主要功能

- 本地任务、商品和自定义成就 CRUD 及 CSV/JSON 批量管理。
- 真实快照创建、恢复和删除；恢复时创建新的托管工作副本。
- 图标浏览、上传、引用检查、替换和导出前完整性检查。
- 真实宏愿、周/月/年复盘、日常与番茄热力图。
- 手机连接诊断、渐进加载、短时内存缓存和只读数据浏览。
- 手机新增任务的分类/技能选择、批量预览、幂等执行和无敏感信息操作记录。
- 本机维护页：先预览、再勾选、最后确认删除项目托管的临时文件。
- LifeUp MCP：让 Codex/Claude 通过现有 Flask API 安全查询，并在明确确认后新增一个手机任务。

MCP 安装、Codex/Claude 配置和安全调用步骤见 [MCP 使用指南](docs/MCP_GUIDE.md)。

## 项目结构

```text
lifeup-dashboard/
├── server.py                 Flask API、本地工作区、SQLite 与手机代理
├── mcp_server.py             仅调用固定 Flask API 的 MCP stdio 适配层
├── index.html                原生 HTML/CSS/JavaScript 前端
├── desktop_app.py            pywebview 桌面壳
├── docs/USER_GUIDE.md        初学者用户指南
├── docs/MCP_GUIDE.md         Codex/Claude MCP 安装与安全使用指南
├── tests/                    隔离真实备份和真实手机写入的回归测试
├── tools/build_desktop.ps1   可复现桌面构建脚本
├── tools/audit_release.py    发布包敏感内容审计
├── outputs/                  用户交付文档、模板和发布 ZIP
├── work/                     临时日志、构建和验证文件（不提交）
└── workspaces/               浏览器工作副本、快照和恢复副本（不提交）
```

## 测试与检查

```powershell
python -m unittest discover -s tests -v
python -m py_compile server.py desktop_app.py mcp_server.py
git diff --check
```

修改 `index.html` 后，还应使用 Node.js `vm.Script` 检查所有内联 JavaScript 语法。项目协作规则和完整命令见 [AGENTS.md](AGENTS.md)。

## 构建桌面发布包

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_desktop.ps1 -Version 1.2.0
```

脚本在 `work/` 中创建隔离虚拟环境和 PyInstaller 临时产物，在构建前运行完整测试，并审计 EXE、发布目录和最终 ZIP。最终文件写入 `outputs/`，不会覆盖 `dist/` 中的历史 EXE。

## 已知边界

- 系统成就保持只读；仅自定义成就可编辑。
- 手机已有任务、商品和成就不支持编辑或删除。
- 云端新增任务的真实手机写入不会进入自动化测试，必须由用户明确确认后手动验证。
- `dist/` 中的文件是历史构建，不代表当前源码；请使用构建脚本生成当前版本。
