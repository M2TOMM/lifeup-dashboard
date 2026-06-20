import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()

with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)

# Find the DB
db_path = None
for root, dirs, files in os.walk(tmp):
    for f in files:
        if f.endswith('.db'):
            db_path = os.path.join(root, f)
            break

if not db_path:
    print('No DB found!')
    for root, dirs, files in os.walk(tmp):
        for f in files:
            print(os.path.join(root, f))
else:
    print(f'DB: {db_path}')
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # List all tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print(f'\n=== Tables ({len(tables)}) ===')
    for t in tables:
        print(f'  {t}')
    
    # Key tables schema
    for tbl in ['TaskModel', 'shopitemmodel', 'achievementinfomodel', 'userachievementmodel', 'SkillModel', 'inventorymodel', 'ItemModel', 'UserModel']:
        matches = [t for t in tables if t.lower() == tbl.lower()]
        if matches:
            actual = matches[0]
            cur.execute(f'PRAGMA table_info("{actual}")')
            cols = cur.fetchall()
            print(f'\n--- {actual} ({len(cols)} cols) ---')
            for c in cols:
                print(f'  {c[1]:35s} {str(c[2]):15s} nullable={not c[3]} default={str(c[4])}')
            cur.execute(f'SELECT COUNT(*) FROM "{actual}"')
            print(f'  Rows: {cur.fetchone()[0]}')
            # Show first row
            cur.execute(f'SELECT * FROM "{actual}" LIMIT 1')
            row = cur.fetchone()
            if row:
                print(f'  Sample: {dict(zip([c[1] for c in cols], row))}')
    
    conn.close()

shutil.rmtree(tmp)
