import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
cur = conn.cursor()

tables = ['shopitemmodel', 'userachievementmodel', 'skillmodel', 'attributelevelmodel']
for t in tables:
    cur.execute(f"PRAGMA table_info({t})")
    cols = cur.fetchall()
    print(f"\n=== {t} ({len(cols)} cols) ===")
    for c in cols:
        print(f"  {c[1]} ({c[2]})")

conn.close()
shutil.rmtree(tmp)
