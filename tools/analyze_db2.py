import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()

with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)

db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
cur = conn.cursor()

tables_to_check = [
    'attributemodel', 'levelmodel', 'attributelevelmodel', 
    'coinmodel', 'categorymodel', 'userachcategorymodel',
    'shopcategorymodel', 'taskmodelgroup', 'synthesismodel',
    'skillgroupmodel', 'tasktargetmodel', 'recordmodel'
]

for tbl in tables_to_check:
    try:
        cur.execute(f'PRAGMA table_info("{tbl}")')
        cols = cur.fetchall()
        cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
        count = cur.fetchone()[0]
        print(f'\n=== {tbl} ({len(cols)} cols, {count} rows) ===')
        for c in cols:
            print(f'  {c[1]:35s} {str(c[2]):15s}')
        if count > 0 and count <= 20:
            cur.execute(f'SELECT * FROM "{tbl}"')
            rows = cur.fetchall()
            col_names = [c[1] for c in cols]
            for row in rows:
                print(f'  -> {dict(zip(col_names, row))}')
    except Exception as e:
        print(f'\n=== {tbl}: ERROR - {e} ===')

conn.close()
shutil.rmtree(tmp)
