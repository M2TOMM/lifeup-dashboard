import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)

# 看看 backup_infos.json 和 WAL 相关
for fname in ['backup_infos.json']:
    p = os.path.join(tmp, fname)
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            print(f'=== {fname} ===')
            print(f.read()[:500])
            print()

# 检查 WAL 文件
db_path = os.path.join(tmp, 'databases', 'LifeUpDB.db')
wal_path = db_path + '-wal'
shm_path = db_path + '-shm'
for p in [wal_path, shm_path]:
    if os.path.exists(p):
        print(f'{os.path.basename(p)}: {os.path.getsize(p)} bytes')
    else:
        print(f'{os.path.basename(p)}: NOT EXISTS')

shutil.rmtree(tmp)
