import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
cur = conn.cursor()

# 1. 看 taskmodel 完整列结构
cur.execute('PRAGMA table_info(taskmodel)')
cols = cur.fetchall()
print('=== taskmodel 全部列 ===')
for c in cols:
    print(f'  {c[1]:40s} {str(c[2]):15s} nullable={not c[3]} default={c[4]}')

# 2. 看一个正常运行的任务的全部字段
cur.execute('SELECT * FROM taskmodel WHERE id=577')
row = cur.fetchone()
col_names = [c[1] for c in cols]
print('\n=== ID=577 (子时入定) 完整数据 ===')
for k, v in zip(col_names, row):
    print(f'  {k}: {v}')

# 3. 看我们创建的测试任务（如果有）
cur.execute("SELECT id, content FROM taskmodel WHERE content LIKE '%Test%' OR content LIKE '%测试%'")
tests = cur.fetchall()
print(f'\n=== 测试任务: {len(tests)} ===')
for t in tests:
    print(f'  ID={t[0]} content={t[1]}')

# 4. 看 ZIP 内部结构
print('\n=== ZIP 内部结构 ===')
with zipfile.ZipFile(backup, 'r') as z:
    for name in sorted(z.namelist()):
        print(f'  {name}')

conn.close()
shutil.rmtree(tmp)
