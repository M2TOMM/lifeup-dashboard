# 数据导出 API 修复报告

**时间**: 2026-06-19 14:30  
**问题**: `/api/export/<table>` 全部返回 500 错误

## 根因

导出 SQL 查询中使用了错误的列名和表名，与 LifeUp SQLite 实际 schema 不匹配：

| 表 | 错误列名 | 正确列名 |
|---|---|---|
| taskmodel | `frequency`, `targetcount`, `donecount`, `description` | `taskfrequency`, `currenttimes`, `remark` (targetcount 在 tasktargetmodel) |
| shopitemmodel | `purchasable`, `categoryid`, `count` | `isdisablepurchase`, `shopcategoryid`, `stocknumber` |
| 成就表 | `achievementvaluemodel`+`achievementmodel` | `userachievementmodel` 单表 |
| skillmodel | `name` | `content as name` |

同时 `/api/load` 路由使用了不存在的全局变量 `CURRENT_ZIP/CURRENT_DB_PATH`，已改为使用 `load_backup()` + `STATE` dict。

## 修复后的验证结果

| 表 | JSON行数 | CSV | 
|---|---|---|
| tasks | 47 | 4103 chars ✓ |
| items | 254 | ✓ |
| inventory | 272 | ✓ |
| achievements | 33 | ✓ |
| skills | 6 | ✓ |
| history | 32 | ✓ |
