import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# --- Full goodseffectmodel analysis ---
print("=== goodseffectmodel 效果类型分布 ===")
cur.execute("""
    SELECT goodseffecttype, COUNT(*) as cnt
    FROM goodseffectmodel WHERE isdel=0
    GROUP BY goodseffecttype
""")
for r in cur.fetchall():
    print(f"  type {r['goodseffecttype']}: {r['cnt']} 条")

# Type 7 = 随机物品奖励（抽卡池）
print("\n=== Type 7 随机奖励 (卡池) ===")
cur.execute("""
    SELECT ge.id, ge.shopitemid, s.itemname, ge.relatedinfos, ge.values_lpcolumn
    FROM goodseffectmodel ge
    JOIN shopitemmodel s ON ge.shopitemid = s.id
    WHERE ge.goodseffecttype = 7 AND ge.isdel = 0
    ORDER BY ge.id
""")
pools = cur.fetchall()
print(f"  共 {len(pools)} 个卡池")

for p in pools[:8]:
    json_str = p['relatedinfos']
    try:
        info = json.loads(json_str)
        items = info.get('itemsInfos', [])
        print(f"  [{p['id']}] {p['itemname']} (shopitemid={p['shopitemid']})")
        print(f"      物品数量: {len(items)}")
        for item in items[:5]:
            # Lookup item name
            sid = item.get('shopItemModelId', item.get('shopItemModelID', 0))
            cur2 = conn.cursor()
            cur2.execute("SELECT itemname FROM shopitemmodel WHERE id=?", (sid,))
            name_row = cur2.fetchone()
            name = name_row['itemname'] if name_row else f'ID:{sid}'
            prob = item.get('probability', 0)
            is_fixed = item.get('isFixedReward', False)
            amt = item.get('amount', 1)
            print(f"        {name}: 概率{prob}% (固定={is_fixed}, 数量={amt})")
        if len(items) > 5:
            print(f"        ... 及其他 {len(items)-5} 种物品")
    except Exception as e:
        print(f"  [{p['id']}] {p['itemname']} - JSON解析失败: {e}")

# Type 2 = 直接效果（如获得金币）
print("\n=== Type 2 直接效果 (sample) ===")
cur.execute("""
    SELECT ge.shopitemid, s.itemname, ge.values_lpcolumn
    FROM goodseffectmodel ge
    JOIN shopitemmodel s ON ge.shopitemid = s.id
    WHERE ge.goodseffecttype = 2 AND ge.isdel = 0
    LIMIT 5
""")
for r in cur.fetchall():
    print(f"  {r['itemname']}: value={r['values_lpcolumn']}")

# --- Task completion data ---
print("\n=== 已完成任务 (时间线数据) ===")
cur.execute("""
    SELECT id, content, taskstatus, finishtime, currenttimes, createdtime, lastresettime
    FROM taskmodel 
    WHERE taskstatus >= 1 AND isdeleterecord = 0
    ORDER BY finishtime DESC
    LIMIT 10
""")
for r in cur.fetchall():
    fin = r['finishtime']
    from datetime import datetime
    fin_str = datetime.fromtimestamp(fin/1000).strftime('%Y-%m-%d %H:%M') if fin else 'N/A'
    print(f"  [{r['id']}] {r['content']}: done={r['taskstatus']}, finish={fin_str}, count={r['currenttimes']}")

# --- inventoryrecordmodel 关键统计 ---
print("\n=== 物品变动历史 ===")
cur.execute("""
    SELECT 
        CASE WHEN ir.isdecrease = 1 THEN '消耗' ELSE '获得' END as action,
        COUNT(*) as cnt
    FROM inventoryrecordmodel ir
    WHERE ir.isdel = 0
    GROUP BY ir.isdecrease
""")
for r in cur.fetchall():
    print(f"  {r['action']}: {r['cnt']} 条")

cur.execute("""
    SELECT ir.createtime, ir.isdecrease, ir.changenumber, s.itemname, ir.desc_lpcolumn, ir.transactionid
    FROM inventoryrecordmodel ir
    JOIN shopitemmodel s ON ir.shopitemmodel_id = s.id
    WHERE ir.isdel = 0
    ORDER BY ir.createtime DESC
    LIMIT 10
""")
for r in cur.fetchall():
    from datetime import datetime
    ts = datetime.fromtimestamp(r['createtime']/1000).strftime('%m-%d %H:%M')
    action = '-' if r['isdecrease'] else '+'
    print(f"  {ts} {action}{r['changenumber']}x {r['itemname']} ({r['desc_lpcolumn']})")

conn.close()
shutil.rmtree(tmp)
