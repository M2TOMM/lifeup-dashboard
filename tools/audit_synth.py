import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# All non-deleted N cards in shop
cur.execute("SELECT id, itemname FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'N-%' ORDER BY itemname")
all_n = [(r['id'], r['itemname']) for r in cur.fetchall()]

# All non-deleted R cards
cur.execute("SELECT id, itemname FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'R-%' ORDER BY itemname")
all_r = [(r['id'], r['itemname']) for r in cur.fetchall()]

# Which N cards have a N→回收券 recipe?
cur.execute("""
    SELECT DISTINCT sc.shopitemmodelid, s.itemname
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
    WHERE sc.isdel=0 AND sm.isdel=0 AND sc.isoutput=0 AND sm.categoryid=6
    AND s.itemname LIKE 'N-%'
""")
synth_n = {r['itemname'] for r in cur.fetchall()}

# Which R cards have a R→回收券 recipe?
cur.execute("""
    SELECT DISTINCT sc.shopitemmodelid, s.itemname
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
    WHERE sc.isdel=0 AND sm.isdel=0 AND sc.isoutput=0 AND sm.categoryid=6
    AND s.itemname LIKE 'R-%'
""")
synth_r = {r['itemname'] for r in cur.fetchall()}

print(f"=== 统计 ===")
print(f"N卡总数(未删): {len(all_n)}")
print(f"N卡有合成配方: {len(synth_n)}")
print(f"R卡总数(未删): {len(all_r)}")
print(f"R卡有合成配方: {len(synth_r)}")

missing_n = [name for _, name in all_n if name not in synth_n]
missing_r = [name for _, name in all_r if name not in synth_r]

if missing_n:
    print(f"\n🚨 缺失N卡配方 ({len(missing_n)}张):")
    for n in sorted(missing_n):
        print(f"  ❌ {n}")

if missing_r:
    print(f"\n🚨 缺失R卡配方 ({len(missing_r)}张):")
    for r in sorted(missing_r):
        print(f"  ❌ {r}")

if not missing_n and not missing_r:
    print(f"\n✅ 所有卡牌均有合成配方（N卡 {len(all_n)}、R卡 {len(all_r)}）")

# Also check SR/SSR if any recipes exist
cur.execute("""
    SELECT DISTINCT s.itemname
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
    WHERE sc.isdel=0 AND sm.isdel=0 AND sc.isoutput=0
    AND (s.itemname LIKE 'SR-%' OR s.itemname LIKE 'SSR-%')
""")
sr_ssr = [r['itemname'] for r in cur.fetchall()]
if sr_ssr:
    print(f"\n注意: SR/SSR卡作为合成材料 ({len(sr_ssr)}张):")
    for c in sr_ssr[:5]:
        print(f"  {c}")
    if len(sr_ssr) > 5:
        print(f"  ... 及其他 {len(sr_ssr)-5} 张")

# 检查是否有SR卡没有回收通路
cur.execute("SELECT COUNT(*) FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'SR-%'")
sr_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'SSR-%'")
ssr_count = cur.fetchone()[0]
print(f"\n=== 回收覆盖分析 ===")
print(f"SR卡({sr_count}张): {'有回收通路(万象归元)' if len(sr_ssr)>0 else '❌ 无回收通路'}")
print(f"SSR卡({ssr_count}张): {'有回收通路(灵宠)' if len(sr_ssr)>0 else '无回收通路'}")
print(f"建议: SR卡需要通过万象归元分类的特殊配方(材料联动)或新增通用回收通路")

conn.close()
shutil.rmtree(tmp)
