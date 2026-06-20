import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Check all relevant history tables
for table in ['taskrecordmodel', 'coinrecordmodel', 'exprecordmodel', 'itemrecordmodel', 'feelingrecordmodel']:
    try:
        cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
        cnt = cur.fetchone()['cnt']
        cur.execute(f"PRAGMA table_info({table})")
        cols = [(r['name'], r['type']) for r in cur.fetchall()]
        print(f"\n{table}: {cnt} rows, {len(cols)} columns")
        for name, typ in cols:
            print(f"  {name}: {typ}")
    except Exception as e:
        print(f"{table}: {e}")

# Also check achievement record table
for table in ['userachievementrecordmodel']:
    try:
        cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
        cnt = cur.fetchone()['cnt']
        cur.execute(f"PRAGMA table_info({table})")
        cols = [(r['name'], r['type']) for r in cur.fetchall()]
        print(f"\n{table}: {cnt} rows, {len(cols)} columns")
        for name, typ in cols:
            print(f"  {name}: {typ}")
    except:
        print(f"{table}: not found")

# Gacha/card pool: check if there's a draw/lottery table
for table in ['drawmodel', 'lotterymodel', 'cardpoolmodel', 'boxmodel', 'chestmodel', 'gachamodel']:
    try:
        cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
        cnt = cur.fetchone()['cnt']
        print(f"\n{table}: {cnt} rows")
    except:
        pass

# Check item record model for card opening records
try:
    cur.execute("SELECT * FROM itemrecordmodel LIMIT 3")
    rows = cur.fetchall()
    print(f"\nitemrecordmodel sample ({len(rows)}):")
    for r in rows:
        print(f"  {dict(r)}")
except Exception as e:
    print(f"itemrecordmodel sample: {e}")

conn.close()
shutil.rmtree(tmp)
