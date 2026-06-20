import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
cur = conn.cursor()

# 列出所有表
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print(f"=== 共 {len(tables)} 张表 ===")

# 找合成/合成配方/合成表相关
synth_tables = [t for t in tables if any(k in t.lower() for k in ['synthesi', 'craft', 'combin', 'recipe', 'fusion', 'mix', 'merg'])]
print(f"\n合成相关表: {synth_tables}")

# 不确定的话，把所有表名列出来找
for t in synth_tables:
    cur.execute(f"PRAGMA table_info({t})")
    cols = cur.fetchall()
    print(f"\n=== {t} ({len(cols)} cols) ===")
    for c in cols:
        print(f"  {c[1]} ({c[2]})")
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"  → {cur.fetchone()[0]} rows")

# 如果没找到合成相关表，看看有没有 inventory/合成/商城相关
if not synth_tables:
    print("\n没有找到合成相关表，检查 inventory/craft 相关...")
    related = [t for t in tables if any(k in t.lower() for k in ['inventor', 'shop', 'item', 'craft', 'recipe', 'material'])]
    for t in related:
        print(f"  {t}")

conn.close()
shutil.rmtree(tmp)
