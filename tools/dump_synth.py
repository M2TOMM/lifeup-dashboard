import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 所有配方 + 分类
cur.execute("""
    SELECT sm.id, sm.name, sm.description, sm.categoryid, sc.categoryname
    FROM synthesismodel sm
    LEFT JOIN synthesiscategory sc ON sm.categoryid = sc.id
    WHERE sm.isdel = 0
    ORDER BY sm.categoryid, sm.orderincategory, sm.id
""")
recipes = cur.fetchall()

# 所有关联
cur.execute("""
    SELECT sc.synthesismodelid, sc.isoutput, sc.amount, sc.shopitemmodelid,
           s.itemname, s.price
    FROM synthesisconnmodel sc
    JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
    WHERE sc.isdel = 0
    ORDER BY sc.synthesismodelid, sc.isoutput, sc.id
""")
conns = cur.fetchall()

conn_by_recipe = {}
for c in conns:
    rid = c['synthesismodelid']
    if rid not in conn_by_recipe:
        conn_by_recipe[rid] = {'inputs': [], 'outputs': []}
    key = 'outputs' if c['isoutput'] else 'inputs'
    conn_by_recipe[rid][key].append({
        'item_id': c['shopitemmodelid'],
        'item_name': c['itemname'],
        'price': c['price'],
        'amount': c['amount']
    })

# 统计
out = []
for r in recipes:
    rid = r['id']
    io = conn_by_recipe.get(rid, {'inputs': [], 'outputs': []})
    out.append({
        'id': r['id'],
        'name': r['name'],
        'desc': r['description'],
        'category': r['categoryname'],
        'cat_id': r['categoryid'],
        'inputs': io['inputs'],
        'outputs': io['outputs']
    })

with open(r'C:\Users\USER\.qclaw\workspace\lifeup-dashboard\all_synth.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# 分类统计
cats = {}
for r in out:
    cats[r['category']] = cats.get(r['category'], 0) + 1

print(f"总配方: {len(out)}")
for k, v in cats.items():
    print(f"  {k}: {v} 条")
print(f"\n无输入: {sum(1 for r in out if not r['inputs'])}")
print(f"无输出: {sum(1 for r in out if not r['outputs'])}")
print(f"输入/输出都有: {sum(1 for r in out if r['inputs'] and r['outputs'])}")
print(f"有描述: {sum(1 for r in out if r['desc'])}")
print(f"\n输出到: all_synth.json")

conn.close()
shutil.rmtree(tmp)
