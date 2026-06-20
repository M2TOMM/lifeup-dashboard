import zipfile, sqlite3, os, tempfile, shutil, json

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# --- Task History: feelingsmodel (completion records) ---
print("=== feelingsmodel (任务完成记录) ===")
cur.execute("SELECT COUNT(*) as cnt FROM feelingsmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(feelingsmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")
cur.execute("SELECT * FROM feelingsmodel LIMIT 3")
for r in cur.fetchall():
    d = dict(r)
    # truncate long fields
    for k in list(d.keys()):
        if isinstance(d[k], str) and len(d[k]) > 80:
            d[k] = d[k][:80] + '...'
    print(f"  {d}")

# --- goodseffectmodel (物品使用效果 = 抽卡池) ---
print("\n=== goodseffectmodel (物品效果/卡池) ===")
cur.execute("SELECT COUNT(*) as cnt FROM goodseffectmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(goodseffectmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")
cur.execute("SELECT * FROM goodseffectmodel LIMIT 5")
for r in cur.fetchall():
    d = dict(r)
    for k in list(d.keys()):
        if isinstance(d[k], str) and len(d[k]) > 80:
            d[k] = d[k][:80] + '...'
    print(f"  {d}")

# --- unlockconditionmodel (解锁条件) ---
print("\n=== unlockconditionmodel ===")
cur.execute("SELECT COUNT(*) as cnt FROM unlockconditionmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(unlockconditionmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")

# --- subtaskmodel (子任务) ---
print("\n=== subtaskmodel ===")
cur.execute("SELECT COUNT(*) as cnt FROM subtaskmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(subtaskmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")

# --- tomatomodel (番茄钟) ---
print("\n=== tomatomodel ===")
cur.execute("SELECT COUNT(*) as cnt FROM tomatomodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(tomatomodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")

# --- achievementrecordmodel ---
print("\n=== achievementrecordmodel ===")
cur.execute("SELECT COUNT(*) as cnt FROM achievementrecordmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(achievementrecordmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")

# --- inventoryrecordmodel ---
print("\n=== inventoryrecordmodel ===")
cur.execute("SELECT COUNT(*) as cnt FROM inventoryrecordmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(inventoryrecordmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")
cur.execute("SELECT * FROM inventoryrecordmodel LIMIT 3")
for r in cur.fetchall():
    d = dict(r)
    for k in list(d.keys()):
        if isinstance(d[k], str) and len(d[k]) > 80:
            d[k] = d[k][:80] + '...'
    print(f"  {d}")

# --- feelingsmodel_attachments ---
print("\n=== feelingsmodel_attachments ===")
cur.execute("SELECT COUNT(*) as cnt FROM feelingsmodel_attachments")
print(f"  行数: {cur.fetchone()['cnt']}")

# --- achievementmodel (system) ---
print("\n=== achievementmodel (系统成就定义) ===")
cur.execute("SELECT COUNT(*) as cnt FROM achievementmodel")
print(f"  行数: {cur.fetchone()['cnt']}")
cur.execute("PRAGMA table_info(achievementmodel)")
for r in cur.fetchall():
    print(f"  {r['name']}: {r['type']}")

conn.close()
shutil.rmtree(tmp)
