import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 合成分类
cur.execute("SELECT * FROM synthesiscategory WHERE isdelete=0 ORDER BY id")
cats = cur.fetchall()
print("=== 合成分类 ===")
for c in cats:
    print(f"  [{c['id']}] {c['categoryname']} (order={c['orderincategory']})")

# 随机看 5 个合成配方，包含输入和输出
cur.execute("SELECT * FROM synthesismodel WHERE isdel=0 ORDER BY id LIMIT 5")
recipes = cur.fetchall()
for r in recipes:
    print(f"\n=== 配方 [{r['id']}] {r['name']} (cat={r['categoryid']}) ===")
    print(f"  描述: {r['description']}")
    # 输入材料 (isoutput=0)
    cur.execute("""
        SELECT sc.amount, s.itemname
        FROM synthesisconnmodel sc
        JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
        WHERE sc.synthesismodelid=? AND sc.isoutput=0 AND sc.isdel=0
    """, (r['id'],))
    inputs = cur.fetchall()
    if inputs:
        print(f"  输入: {', '.join(f'{x['amount']}x {x['itemname']}' for x in inputs)}")
    # 输出 (isoutput=1)
    cur.execute("""
        SELECT sc.amount, s.itemname
        FROM synthesisconnmodel sc
        JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
        WHERE sc.synthesismodelid=? AND sc.isoutput=1 AND sc.isdel=0
    """, (r['id'],))
    outputs = cur.fetchall()
    if outputs:
        print(f"  输出: {', '.join(f'{x['amount']}x {x['itemname']}' for x in outputs)}")

# 统计
cur.execute("SELECT COUNT(*) as cnt FROM synthesismodel WHERE isdel=0")
total = cur.fetchone()['cnt']
cur.execute("""
    SELECT COUNT(DISTINCT sm.id) FROM synthesismodel sm
    JOIN synthesisconnmodel sc ON sc.synthesismodelid=sm.id
    WHERE sm.isdel=0 AND sc.isdel=0 AND sc.isoutput=0
""")
has_inputs = cur.fetchone()['cnt']
cur.execute("""
    SELECT COUNT(DISTINCT sm.id) FROM synthesismodel sm
    JOIN synthesisconnmodel sc ON sc.synthesismodelid=sm.id
    WHERE sm.isdel=0 AND sc.isdel=0 AND sc.isoutput=1
""")
has_outputs = cur.fetchone()['cnt']
print(f"\n统计: {total} 配方, {has_inputs} 有输入, {has_outputs} 有输出")

conn.close()
shutil.rmtree(tmp)
