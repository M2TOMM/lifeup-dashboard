import zipfile, sqlite3, os, tempfile, shutil
backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
import zipfile as zf
zf.ZipFile(backup, 'r').extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Task completion-related columns
print("=== taskmodel completion columns ===")
cur.execute("PRAGMA table_info(taskmodel)")
task_cols = [r['name'] for r in cur.fetchall()]
print(f"Total columns: {len(task_cols)}")
for c in task_cols:
    if any(kw in c.lower() for kw in ['finish', 'complet', 'done', 'time', 'date', 'status', 'current', 'count', 'reset', 'end']):
        print(f"  ★ {c}")

# Check column content for time-related
for col in ['updatedtime', 'createdtime', 'lastresettime', 'endtime']:
    if col in task_cols:
        cur.execute(f"SELECT id, content, taskstatus, currenttimes, {col} FROM taskmodel WHERE taskstatus>=1 AND isdeleterecord=0 ORDER BY {col} DESC LIMIT 5")
        for r in cur.fetchall():
            from datetime import datetime
            ts = r[col]
            if ts and ts > 0:
                ts_str = datetime.fromtimestamp(ts/1000).strftime('%Y-%m-%d %H:%M')
                print(f"  [{r['id']}] {r['content'][:30]} status={r['taskstatus']} count={r['currenttimes']} {col}={ts_str}")

# All time-related columns in taskmodel
print("\n=== All time columns ===") 
for c in task_cols:
    if 'time' in c.lower() or 'date' in c.lower():
        print(f"  {c}")

conn.close()
shutil.rmtree(tmp)
