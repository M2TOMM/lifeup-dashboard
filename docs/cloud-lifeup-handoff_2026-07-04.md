# 云人升 API 接入交接文档

日期：2026-07-04
项目路径：`C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard`
本地访问地址：`http://127.0.0.1:5000/`

## 1. 当前项目定位

这个项目原本是一个 LifeUp 备份存档管理器，主要逻辑是：

- 用户选择 LifeUp 备份 ZIP。
- 后端解压备份，读取里面的 SQLite 数据库。
- 页面展示并修改任务、商品、成就等数据。
- 最后重新打包成备份文件。

现在新增了一条云人升 API 线路：

- 手机端 LifeUp 开启云人升服务。
- 电脑端通过 `http://手机IP:端口` 读取手机实时数据。
- 第一阶段只做“读取手机实时数据 + 新增任务”。
- 不编辑、不删除手机端已有数据。
- 不把手机数据强行同步进本地备份。

后续开发时一定要记住：本地备份模式和手机云人升模式是两种数据源，不能混为一谈。

## 2. 当前已完成内容

### 2.1 云人升 API 页面

页面入口：左侧导航的“云人升 API”。

已支持：

- 填写手机 IP。
- 填写端口，默认一般是 `13276`。
- Token 字段保留为可选，但不会保存到本地配置。
- 测试连接，调用手机端 `/info`。
- 保存 Host 和端口到本地 `lifeup_cloud_config.json`。
- 读取云端数据：
  - 任务：`/tasks`
  - 商品：`/items`
  - 技能：`/skills`
  - 金币：`/coin`
  - 成就分类：`/achievement_categories`
  - 成就：先读分类，再逐个调用 `/achievements/{id}`
- 新增单个任务：
  - 先生成 `lifeup://api/add_task?...` 预览。
  - 用户确认后调用 `POST /api/contentprovider`。
- 批量新增任务：
  - 支持 CSV。
  - 支持 JSON 数组。
  - 先预览和校验。
  - 有错误行时禁止执行。
  - 用户确认后顺序发送。

### 2.2 旧管理页已接入云人升只读数据

以下页面现在支持读取手机端实时数据：

- 任务管理
- 商店管理
- 成就管理

这些页面现在有“本地备份 / 手机云人升”数据源切换。

手机云人升模式下：

- 页面显示“手机云人升实时数据”。
- 页面显示只读提示。
- 编辑、删除、批量修改等危险按钮会隐藏或禁用。
- 新增任务会引导用户去“云人升 API”页面操作。
- 商品和成就的云端新增/编辑暂时不做。

## 3. 当前关键文件

### 3.1 后端

文件：`C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard\server.py`

关键区域：

- 云人升配置和请求函数：
  - `load_cloud_config`
  - `save_cloud_config`
  - `normalize_cloud_config`
  - `cloud_request`
  - `cloud_post_json`
- 云人升安全执行：
  - `normalize_lifeup_urls`
  - 只允许执行 `lifeup://api/` 开头的官方 URL。
- 云数据转换函数：
  - `list_cloud_tasks_for_dashboard`
  - `list_cloud_items_for_dashboard`
  - `list_cloud_achievements_for_dashboard`
- 已接入 `source=cloud` 的接口：
  - `GET /api/tasks?source=cloud`
  - `GET /api/items?source=cloud`
  - `GET /api/achievements?source=cloud`
  - `GET /api/categories/tasks?source=cloud`
  - `GET /api/categories/shop?source=cloud`
  - `GET /api/categories/achievements?source=cloud`
- 云人升 API 页面用的接口：
  - `GET/POST /api/cloud/config`
  - `POST /api/cloud/test`
  - `POST /api/cloud/data`
  - `POST /api/cloud/execute`

### 3.2 前端

文件：`C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard\index.html`

关键区域：

- 数据源状态：
  - `dataSource`
  - `setDataSource`
  - `sourceQuery`
  - `addSourceParam`
  - `sourceBanner`
- 已改造页面：
  - `loadTasks`
  - `loadItems`
  - `loadAchievements`
- 云人升 API 页面：
  - `loadCloud`
  - `cloudTest`
  - `cloudSaveConfig`
  - `cloudPreviewTask`
  - `cloudExecuteTask`
  - `cloudPreviewBatch`
  - `cloudExecuteBatch`

### 3.3 本地配置

文件：`C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard\lifeup_cloud_config.json`

用途：

- 保存手机 Host。
- 保存端口。
- 不保存 Token。

这个文件已经加入 `.gitignore`，不要提交到 GitHub。

## 4. 当前验证结果

当前手机云人升连接信息：

- Host：`192.168.3.45`
- Port：`13276`
- LifeUp appVersionName：`1.104.0-rc01`
- API Version：`7`

已验证：

- `POST /api/cloud/test` 成功。
- 通过 API 新增测试任务成功，手机 App 里能看到。
- `GET /api/tasks?source=cloud` 成功，读到 `18` 条任务。
- `GET /api/items?source=cloud` 成功，读到 `210` 件商品。
- `GET /api/achievements?source=cloud` 成功，读到 `55` 条成就。
- 任务管理页面浏览器验证通过，显示 `18` 行。
- 商店管理页面浏览器验证通过，显示 `210` 行。
- 成就管理页面浏览器验证通过，显示 `55` 行。
- 后端语法检查通过：
  - `python -m py_compile server.py`
- 前端 JS 语法检查通过。

注意：

- 成就页加载较慢，可能需要一分钟左右。
- 这是因为成就 API 不是一次全量读取，而是“先读分类，再逐类读取成就”。
- 商品图标里有 Android `content://...` 地址，电脑网页不能直接显示。当前后端已经把这类图标地址隐藏，避免污染列表。

## 5. 如何运行

在 PowerShell 中执行：

```powershell
cd C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard
python server.py
```

然后打开：

```text
http://127.0.0.1:5000/
```

如果端口已经被占用，可以先查：

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen
```

当前服务已经在本机运行，监听 `127.0.0.1:5000`。

## 6. 安全边界

第一版必须坚持以下原则：

- 云人升模式只读已有任务、商品、成就。
- 只允许新增任务。
- 不编辑手机端已有任务。
- 不删除手机端已有数据。
- 不调用 `/data/import`。
- 不直接写手机端 SQLite。
- 不把云端数据自动写进本地备份。
- 所有写操作必须先预览，再确认。
- 所有执行 URL 必须以 `lifeup://api/` 开头。

如果后续要突破这些限制，需要先单独做调研和备份策略。

## 7. 已知问题和风险

### 7.1 手机云人升服务不是永久后台

云人升依赖手机当前状态：

- 手机和电脑必须在同一局域网。
- 手机 IP 可能变化。
- 手机端云人升服务需要保持开启。
- 手机系统可能杀后台。

所以后续 UI 要继续增强连接状态提示。

### 7.2 成就读取慢

成就目前按分类逐个读取，速度慢但结果可用。

后续可以优化：

- 分类逐个显示加载进度。
- 某个分类失败时继续显示其他分类。
- 做短时间缓存，比如 5 分钟。

### 7.3 商品和成就写入能力未确认

目前只确认了任务新增可用。

商品、成就、图标的新增/编辑/删除都暂时不要做，除非确认有官方安全 API。

### 7.4 本地备份模式仍然保留危险操作

本地备份模式仍然支持原来的编辑、删除、批量修改。

这没问题，但 UI 上要让用户明确知道自己当前在什么模式：

- 本地备份模式：改的是备份文件。
- 手机云人升模式：读的是手机实时数据，当前只读。

## 8. 推荐下一步任务清单

### 第一优先级：把新增任务体验做好

1. 把新增任务里的 `category` 从数字 ID 改成下拉框。
2. 把 `skills` 从手填 ID 改成多选框。
3. 读取 `/tasks_categories` 和 `/skills` 自动填充选项。
4. 预览 URL 时同时显示人类可读的任务摘要。
5. 新增成功后自动刷新任务管理页。

### 第二优先级：完善批量导入

1. 增加“下载 CSV 模板”按钮。
2. 批量预览表格显示更多字段。
3. 错误行标红，并说明错误原因。
4. 有错误行时禁止执行。
5. 执行完成后显示每一行的成功或失败。
6. 增加“防重复点击”逻辑。

### 第三优先级：改善读取体验

1. 任务、商品、成就页增加更清楚的加载状态。
2. 成就页显示“正在读取第几个分类”。
3. 商品页隐藏或转换不可显示的 Android 图标地址。
4. 增加“最后刷新时间”。
5. 增加“重新测试连接”入口。

### 第四优先级：操作日志

1. 新增本地操作日志文件，例如 `lifeup_cloud_operations.jsonl`。
2. 记录每次新增任务：
   - 时间
   - 任务标题
   - URL 预览
   - 执行结果
3. 日志只用于查账，不作为同步依据。

### 第五优先级：调研商品、成就、图标写入

1. 查 LifeUp API 是否有官方商品新增接口。
2. 查 LifeUp API 是否有官方成就新增接口。
3. 查 `/files/upload` 是否能用于图标上传。
4. 不要用 `/data/import` 做第一方案。
5. 不要直接写手机数据库。

## 9. 给后续 Codex 的提醒

如果后续继续开发，请先做这些检查：

1. 查看 Git 状态：

```powershell
git -C C:\Users\M2TO\Documents\LifeUp\lifeup-dashboard status --short
```

2. 确认服务是否运行：

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen
```

3. 确认云人升连接：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/cloud/test -ContentType 'application/json' -Body '{}'
```

4. 确认任务云读取：

```powershell
(Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/tasks?source=cloud').Count
```

5. 修改代码后至少运行：

```powershell
python -m py_compile server.py
```

前端如果改了 JS，也要做 JS 语法检查。

## 10. 当前最推荐的下一步

最推荐下一步做：

1. 新增任务表单改成“分类下拉 + 技能多选”。
2. 批量导入增加 CSV 模板下载。
3. 成就页增加加载进度和缓存。

这三件事能明显提升可用性，同时不突破当前安全边界。
