# LifeUp Dashboard MCP 使用指南

LifeUp MCP 是现有 Dashboard 的一层轻量适配器。它把 Codex、Claude 等 MCP 客户端的工具调用转换为本机 Flask HTTP 请求，不会直接打开 SQLite、备份 ZIP、宏愿映射或手机配置。

## 1. 工作方式

```text
Codex / Claude
      ↓ MCP stdio
mcp_server.py
      ↓ 仅限本机、固定白名单 HTTP API
server.py（Flask）
      ↓
local 工作副本 / cloud 手机云人升
```

安全边界：

- Dashboard 地址只允许 `localhost`、`127.0.0.1` 或 `::1`，不允许外部主机、用户名、密码或路径。
- MCP 只能调用代码中列出的固定 API；不能调用本地保存、删除、批量修改等接口。
- 每次工具调用必须明确填写 `source=local` 或 `source=cloud`，不会自动切换或混合数据源。
- MCP 不读取或保存手机 Token；Token 仍只存在 Dashboard 服务进程内存中。
- 新增任务只支持 `cloud`，必须先预览，再由用户明确确认，并提供幂等键。
- 不提供编辑、删除、批量写入或修改手机已有数据的 MCP 工具。

## 2. 先启动 Dashboard

在第一个 Windows PowerShell 窗口运行：

```powershell
cd C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

看到服务启动后，浏览器访问 [http://127.0.0.1:5000/](http://127.0.0.1:5000/)。

- 查询 `local` 前，先用页面加载托管工作副本或已核对的导出副本。不要把原始备份用于写入验证。
- 查询 `cloud` 前，先在页面的“手机连接/新增任务”中测试连接。手机 Token 不要写入 MCP 配置。

## 3. 直接检查 MCP 协议

下面的命令只检查服务能否启动和返回工具列表，不会查询或修改 LifeUp 数据：

```powershell
cd C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard
$request = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"powershell-check","version":"1.0"}}}'
$request | .\.venv\Scripts\python.exe .\mcp_server.py
```

正常结果包含 `serverInfo.name = lifeup-dashboard`。

## 4. Codex 配置

打开 `C:\Users\M2TO\.codex\config.toml`，加入：

```toml
[mcp_servers.lifeup_dashboard]
command = "C:\\Users\\M2TO\\Documents\\LifeUp\\lifeup-dashboard\\.venv\\Scripts\\python.exe"
args = ["C:\\Users\\M2TO\\Documents\\LifeUp\\lifeup-dashboard\\mcp_server.py"]
startup_timeout_sec = 10
tool_timeout_sec = 60
env = { LIFEUP_DASHBOARD_URL = "http://127.0.0.1:5000" }
```

保存后重新启动 Codex。Dashboard 的 Flask 服务仍需按第 2 节单独运行。

## 5. Claude Desktop 配置

打开 `%APPDATA%\Claude\claude_desktop_config.json`。如果文件已有其他 MCP 服务，只把 `lifeup-dashboard` 合并到现有 `mcpServers` 对象，不要覆盖其他配置：

```json
{
  "mcpServers": {
    "lifeup-dashboard": {
      "command": "C:\\Users\\M2TO\\Documents\\LifeUp\\lifeup-dashboard\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\M2TO\\Documents\\LifeUp\\lifeup-dashboard\\mcp_server.py"
      ],
      "env": {
        "LIFEUP_DASHBOARD_URL": "http://127.0.0.1:5000"
      }
    }
  }
}
```

保存后重新启动 Claude Desktop。

## 6. 可用工具

| 工具 | 用途 | 写入 |
|---|---|---|
| `lifeup_get_status` | 查看 local 载入状态或 cloud 内存连接配置状态 | 否 |
| `lifeup_list_tasks` | 查询任务，可按状态、关键词、分类筛选 | 否 |
| `lifeup_list_items` | 查询商品，可按关键词、分类筛选 | 否 |
| `lifeup_list_achievements` | 查询自定义成就和子分类 | 否 |
| `lifeup_get_dashboard` | 查询总览统计 | 否 |
| `lifeup_get_focus` | 查询闭关/番茄专注统计 | 否 |
| `lifeup_get_wishes` | 查询真实宏愿；当前仅 `local` 可用 | 否 |
| `lifeup_preview_task` | 校验并预览一个 `cloud` 新增任务 | 否 |
| `lifeup_create_task` | 用户明确确认后执行已预览任务 | 仅新增一个 cloud 任务 |

查询示例提示词：

```text
请使用 lifeup_list_tasks 查询 local 数据源中的进行中任务，只读取，不做任何修改。
```

```text
请使用 lifeup_get_dashboard 查询 cloud 数据源。如果手机连接不可用，直接告诉我错误，不要切换到 local。
```

## 7. 安全新增一个手机任务

第一步只预览，不执行。任务 URL 必须以 `lifeup://api/add_task?` 开头，标题使用 `todo` 参数。可以在 PowerShell 中安全编码标题：

```powershell
$title = [uri]::EscapeDataString('散步 20 分钟')
$lifeupUrl = "lifeup://api/add_task?todo=$title&coin=5"
$lifeupUrl
```

把结果交给客户端，并明确要求“只调用 `lifeup_preview_task`，不要执行”。预览结果会返回 Flask 已解析的任务摘要、短时有效的 `preview_token` 和摘要指纹。

第二步核对标题、分类和技能。只有你明确同意后，客户端才能调用 `lifeup_create_task`，并必须提交：

- `source = cloud`
- 上一步的 `preview_token`
- `confirmed = true`
- 8～128 个字符的唯一 `idempotency_key`

如果返回“结果不确定”或网络超时，先刷新手机任务列表核对，不要自动重试，也不要换一个幂等键重复发送。

## 8. 常见问题

### MCP 能发现工具，但调用提示无法连接

先确认另一个 PowerShell 窗口中的 `server.py` 仍在运行，再访问 [http://127.0.0.1:5000/api/status](http://127.0.0.1:5000/api/status)。MCP 客户端启动适配器，不会自动替你启动 Dashboard Flask 服务。

### local 查询提示尚未加载备份

回到 Dashboard 页面，通过“选择文件”加载托管工作副本。不要让 MCP 直接读取原始备份路径。

### cloud 查询失败

回到 Dashboard 页面重新测试手机连接并重新输入 Token。Token 只保存在当前 Flask 进程内存中，重启服务后需要重新输入。

### 为什么没有编辑或删除工具

首版 MCP 坚持只读优先。唯一写操作是经过预览、明确确认和幂等保护的手机新增任务；编辑、删除、批量写入和手机已有数据修改不在安全范围内。
