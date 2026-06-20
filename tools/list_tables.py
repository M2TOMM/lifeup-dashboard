import zipfile, sqlite3, os, tempfile, shutil
backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z: z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print(f"Total tables: {len(tables)}")
for t in tables: print(f"  {t}")
conn.close()
shutil.rmtree(tmp)
