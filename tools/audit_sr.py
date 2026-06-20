import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# All SR cards
cur.execute("SELECT id, itemname FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'SR-%' ORDER BY itemname")
all_sr = [(r['id'], r['itemname']) for r in cur.fetchall()]

# SR cards used as input in synthesis
cur.execute("""
    SELECT DISTINCT s.itemname
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
    WHERE sc.isdel=0 AND sm.isdel=0 AND sc.isoutput=0 AND s.itemname LIKE 'SR-%'
""")
used_sr = {r['itemname'] for r in cur.fetchall()}

# All SSR cards
cur.execute("SELECT id, itemname FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'SSR-%' ORDER BY itemname")
all_ssr = [(r['id'], r['itemname']) for r in cur.fetchall()]

# SSR used as input
cur.execute("""
    SELECT DISTINCT s.itemname
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
    WHERE sc.isdel=0 AND sm.isdel=0 AND sc.isoutput=0 AND s.itemname LIKE 'SSR-%'
""")
used_ssr = {r['itemname'] for r in cur.fetchall()}

print(f"=== SR卡回收覆盖 ===")
print(f"SR卡总数: {len(all_sr)}")
print(f"已在合成配方中: {len(used_sr)}")
missing_sr = [name for _, name in all_sr if name not in used_sr]
print(f"无回收通路: {len(missing_sr)}")

if missing_sr:
    print(f"\n❌ 无任何合成用途的 SR 卡:")
    for n in sorted(missing_sr)[:15]:
        print(f"   {n}")
    if len(missing_sr) > 15:
        print(f"   ... 及其他 {len(missing_sr)-15} 张")

# 万象归元 recipes - what SR cards are used in each
print(f"\n=== 万象归元 配方详情 ===")
cur.execute("""
    SELECT sm.id, sm.name, sm.description
    FROM synthesismodel sm
    WHERE sm.categoryid=1 AND sm.isdel=0
""")
for r in cur.fetchall():
    cur.execute("""
        SELECT s.itemname, sc.amount, sc.isoutput
        FROM synthesisconnmodel sc
        JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
        WHERE sc.synthesismodelid=? AND sc.isdel=0
        ORDER BY sc.isoutput, sc.id
    """, (r['id'],))
    items = cur.fetchall()
    ins = [f'{i["amount"]}x{i["itemname"]}' for i in items if i['isoutput']==0]
    outs = [f'{i["amount"]}x{i["itemname"]}' for i in items if i['isoutput']==1]
    print(f"  [{r['id']}] {r['name']}")
    print(f"      → {' + '.join(ins)} → {' + '.join(outs)}")

# SSR cards
print(f"\n=== SSR卡回收覆盖 ===")
print(f"SSR卡总数: {len(all_ssr)}")
print(f"已在合成配方中: {len(used_ssr)}")
missing_ssr = [name for _, name in all_ssr if name not in used_ssr]
if missing_ssr:
    print(f"❌ 无合成通路 SSR: {missing_ssr}")

conn.close()
shutil.rmtree(tmp)
