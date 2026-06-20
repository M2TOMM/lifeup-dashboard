# LifeUp Dashboard v5 构建 + 端到端验证

**时间**: 2026-06-19 14:38  
**目标**: 打包最新修复，验证全功能

## 构建

- PyInstaller onefile + windowed 打包 `desktop_app.py`
- 包含 `index.html` + `server.py` 作为 data files
- 输出: `dist/LifeUp-v5.exe` (15.7 MB)
- webview + Flask 内嵌，无需浏览器

## 端到端验证（14 模块全部通过）

| 模块 | 数据 | 
|---|---|
| 📊 修行总览 | coin/Lv/achievements ✓ |
| 📋 任务管理 | 16 项 ✓ |
| 🏪 商店管理 | 254 件 ✓ |
| 🏆 成就 | 33 条 ✓ |
| ⚔️ 属性 | 6 项 ✓ |
| 🎒 背包 | JOIN inventory+shop ✓ |
| 🔮 合成配方 | 100 条 ✓ |
| 📜 活动记录 | 87 事件 ✓ |
| 🎰 卡池管理 | 22 池 / 133 效果 ✓ |
| 📈 成就进度 | 33 成就 / 0 完成 ✓ |
| 🃏 卡牌图鉴 | 151 张 ✓ |
| 📊 经济看板 | 11天 / 💰3690 ✓ |
| 📸 快照管理 | localStorage ✓ |
| 🌌 位面切换 | localStorage ✓ |

## 6 表数据导出

全部 JSON + CSV 验证通过: tasks(47), items(254), inventory(272), achievements(33), skills(6), history(32)
