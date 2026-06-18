"""
LifeUp 管理面板 - 后端服务
直接读写 LifeUp 备份存档(.zip)中的 SQLite 数据库
"""
import zipfile, sqlite3, os, tempfile, shutil, json, time, hashlib
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder='.')

# 全局状态
STATE = {
    'backup_path': None,
    'db_path': None,
    'tmpdir': None,
    'loaded': False
}

DB_INTERNAL = 'databases/LifeUpDB.db'

# ─── 存档读写 ───────────────────────────────────────────

def load_backup(path):
    """解压备份并连接数据库"""
    if STATE['tmpdir']:
        try: shutil.rmtree(STATE['tmpdir'])
        except: pass
        STATE['tmpdir'] = None
        STATE['db_path'] = None
        STATE['loaded'] = False

    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(path, 'r') as z:
        z.extractall(tmp)

    db = os.path.join(tmp, DB_INTERNAL)
    if not os.path.exists(db):
        raise FileNotFoundError(f'备份中未找到 {DB_INTERNAL}')

    STATE['backup_path'] = path
    STATE['tmpdir'] = tmp
    STATE['db_path'] = db
    STATE['loaded'] = True
    return True

def save_backup(output_path=None):
    """将修改后的数据库重新打包为备份"""
    if not STATE['loaded']:
        raise RuntimeError('未加载备份')

    if output_path is None:
        output_path = STATE['backup_path']

    # 先关闭数据库连接，确保所有写入已刷新
    import gc; gc.collect()

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        for root, dirs, files in os.walk(STATE['tmpdir']):
            for fname in files:
                file_path = os.path.join(root, fname)
                # 使用正斜杠路径（LifeUp 要求）
                arcname = os.path.relpath(file_path, STATE['tmpdir']).replace('\\', '/')
                zout.write(file_path, arcname)

    return output_path

def get_db():
    """获取数据库连接，每次调用都创建新连接"""
    if not STATE['loaded']:
        raise RuntimeError('未加载备份')
    conn = sqlite3.connect(STATE['db_path'])
    conn.row_factory = sqlite3.Row
    return conn

def now_ms():
    return int(time.time() * 1000)

# ─── API 路由 ────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/status')
def status():
    return jsonify({
        'loaded': STATE['loaded'],
        'backup_path': STATE['backup_path'],
        'filename': os.path.basename(STATE['backup_path']) if STATE['backup_path'] else None
    })

@app.route('/api/open', methods=['POST'])
def open_backup():
    data = request.get_json()
    path = data.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'error': f'文件不存在: {path}'}), 400
    try:
        load_backup(path)
        return jsonify({'ok': True, 'path': path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/save', methods=['POST'])
def save():
    data = request.get_json() or {}
    output = data.get('path') or STATE['backup_path']
    if not output:
        return jsonify({'error': '未指定保存路径'}), 400
    try:
        save_backup(output)
        return jsonify({'ok': True, 'path': output})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── 总览 ───────────────────────────────────────────────

@app.route('/api/dashboard')
def dashboard():
    conn = get_db()
    try:
        cur = conn.cursor()

        # 用户信息
        cur.execute("SELECT nickname, userhead, userid FROM usermodel WHERE id=1")
        user = dict(cur.fetchone() or {})

        # 金币余额（从 coinmodel 取最新 savingbalance）
        cur.execute("SELECT savingbalance FROM coinmodel ORDER BY id DESC LIMIT 1")
        coin_row = cur.fetchone()
        coins = coin_row['savingbalance'] if coin_row else 0

        # 使用天数
        cur.execute("SELECT usingdays, currentusingdaystreak, longestusingdaystreak FROM recordmodel WHERE id=1")
        record = dict(cur.fetchone() or {})

        # 任务统计（去重：同名任务只计最新一条）
        cur.execute("""
            SELECT COUNT(*) as total FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_total = cur.fetchone()['total']
        cur.execute("""
            SELECT COUNT(*) as active FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0 AND t1.taskstatus=0
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_active = cur.fetchone()['active']
        cur.execute("""
            SELECT COUNT(*) as done FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.taskstatus=1
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_done = cur.fetchone()['done']

        # 商品统计
        cur.execute("SELECT COUNT(*) as total FROM shopitemmodel WHERE isdel=0")
        item_total = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as inv FROM inventorymodel WHERE stocknumber>0")
        inv_count = cur.fetchone()['inv']

        # 成就统计
        cur.execute("SELECT COUNT(*) as total FROM userachievementmodel WHERE isdelete=0")
        ach_total = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as done FROM userachievementmodel WHERE isdelete=0 AND achievementstatus>=1")
        ach_done = cur.fetchone()['done']

        # 系统成就
        cur.execute("SELECT COUNT(*) as total FROM achievementinfomodel")
        sys_ach = cur.fetchone()['total']

        # 技能/属性
        cur.execute("SELECT id, content as name, description, experience, type, color, icon, status FROM skillmodel WHERE isdel=0 ORDER BY orderincategory")
        skills = [dict(r) for r in cur.fetchall()]

        # 属性等级
        cur.execute("SELECT * FROM attributemodel WHERE id=1")
        attrs = dict(cur.fetchone() or {})

        # 等级计算 (简单估算)
        cur.execute("SELECT perlevelexp FROM levelmodel WHERE id=1")
        level_row = cur.fetchone()
        per_level_exp = level_row['perlevelexp'] if level_row else 100

        for s in skills:
            exp = s.get('experience', 0) or 0
            s['level'] = max(1, exp // per_level_exp + 1)
            s['current_exp'] = exp
            s['next_exp'] = s['level'] * per_level_exp
            s['progress'] = round((exp % per_level_exp) / per_level_exp * 100, 1)

        total_exp = sum(s['experience'] or 0 for s in skills)
        estimated_level = max(1, total_exp // per_level_exp)
        next_exp = (estimated_level) * per_level_exp
        level_progress = ((total_exp % per_level_exp) / per_level_exp * 100) if per_level_exp else 0

        return jsonify({
            'user': user,
            'coins': coins,
            'level': {'current': estimated_level, 'total_exp': total_exp, 'next_exp': next_exp, 'progress': round(level_progress, 1)},
            'record': record,
            'tasks': {'total': task_total, 'active': task_active, 'done': task_done},
            'items': {'total': item_total, 'inventory': inv_count},
            'achievements': {'total': ach_total, 'done': ach_done, 'system': sys_ach},
            'skills': skills,
            'attributes': attrs,
            'per_level_exp': per_level_exp
        })
    finally:
        conn.close()

# ─── 任务 CRUD ──────────────────────────────────────────

@app.route('/api/tasks')
def list_tasks():
    conn = get_db()
    try:
        cur = conn.cursor()
        filter_type = request.args.get('filter', 'all')  # all, active, done
        status_cond = {'all': '1=1', 'active': 'taskstatus=0', 'done': 'taskstatus>=1'}.get(filter_type, '1=1')

        # 去重策略：进行中/全部 → 同名只保留最新；已完成 → 保留全部历史
        dedup_sql = """
              AND t1.id = (
                SELECT MAX(t2.id) FROM taskmodel t2
                WHERE t2.content = t1.content
                  AND t2.isdeleterecord=0 AND t2.isfrozen=0
              )
        """ if filter_type != 'done' else ""

        cur.execute(f"""
            SELECT t1.id, t1.content as title, t1.taskfrequency as frequency, t1.rewardcoin as coin,
                   t1.expreward as exp, t1.remark as note, t1.taskstatus as done,
                   t1.currenttimes as done_count, t1.categoryid, t1.createdtime,
                   t1.updatedtime, t1.tagcolor, t1.taskdifficultydegree as difficulty,
                   t1.isfrozen, t1.priority, t1.groupid, t1.tasktype
            FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0 AND {status_cond}
            {dedup_sql}
            ORDER BY t1.priority DESC, t1.createdtime DESC
        """)
        tasks = [dict(r) for r in cur.fetchall()]

        # 获取分类名称
        cur.execute("SELECT id, categoryname FROM categorymodel")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}

        # 获取 tasktarget（目标次数）
        cur.execute("SELECT id, targettimes FROM tasktargetmodel")
        targets = {r['id']: r['targettimes'] for r in cur.fetchall()}

        for t in tasks:
            t['category_name'] = cats.get(t.get('categoryid'), '-')
            t['target_count'] = targets.get(t.get('id'), 1)

        return jsonify(tasks)
    finally:
        conn.close()

@app.route('/api/tasks/add', methods=['POST'])
def add_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        # 先插入 tasktarget
        target_times = data.get('target_count', 1)
        cur.execute("INSERT INTO tasktargetmodel (targettimes, extraexpreward, repeatendinclusive, repeatendmode, repeatendbehavior) VALUES (?, 0, 1, 0, 0)",
                    (target_times,))
        target_id = cur.lastrowid

        # 生成 extrainfo JSON（LifeUp 任务元数据，缺了会被跳过）
        extrainfo = json.dumps({
            "autoUseItems": False,
            "coinPunishmentFactor": 0.0,
            "expPunishmentFactor": 0.0,
            "t_f_m": 1,
            "writeFeelings": False
        })

        cur.execute("""
            INSERT INTO taskmodel (
                content, taskfrequency, rewardcoin, expreward, remark,
                taskstatus, currenttimes, categoryid, createdtime, updatedtime, isdeleterecord,
                isfrozen, tagcolor, taskdifficultydegree, priority, tasktargetid,
                userid, isshared, tasktype, isneedtoremake, enableebbinghausmode,
                taskurgencydegree, ishandleoverdue, rewardcoinvariable,
                relatedattribute1, relatedattribute2, relatedattribute3,
                teamrecordid, teamid, taskid, taskcountextraid,
                lasttaskid, nexttaskid, groupid, orderincategory,
                isusespecificexpiretime, isuserinputstarttime, starttime,
                extrainfo, completereward
            ) VALUES (
                ?, ?, ?, ?, ?,
                0, 0, ?, ?, ?, 0,
                0, ?, ?, 0, ?,
                0, 0, ?, 0, 0,
                ?, 0, 0,
                '', '', '',
                -1, -1, 0, -1,
                0, 0, 0, 0,
                0, 0, ?,
                ?, ''
            )
        """, (
            data.get('title', '新任务'),
            data.get('frequency', 1),
            data.get('coin', 0),
            data.get('exp', 0),
            data.get('note', ''),
            data.get('category_id', 0),
            now, now,
            data.get('tagcolor', 0),
            data.get('difficulty', 1),
            target_id,
            data.get('tasktype', 0),
            data.get('urgency', 1),
            now,
            extrainfo
        ))
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/update', methods=['POST'])
def update_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            UPDATE taskmodel SET content=?, taskfrequency=?, rewardcoin=?, expreward=?,
                remark=?, categoryid=?, updatedtime=?, taskdifficultydegree=?, tagcolor=?, priority=?
            WHERE id=?
        """, (
            data.get('title'),
            data.get('frequency', 1),
            data.get('coin', 0),
            data.get('exp', 0),
            data.get('note', ''),
            data.get('category_id', 0),
            now,
            data.get('difficulty', 1),
            data.get('tagcolor', 0),
            data.get('priority', 0),
            data['id']
        ))
        # 更新 tasktarget
        if 'target_count' in data:
            cur.execute("UPDATE tasktargetmodel SET targettimes=? WHERE id=(SELECT tasktargetid FROM taskmodel WHERE id=?)",
                        (data['target_count'], data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/delete', methods=['POST'])
def delete_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE taskmodel SET isdeleterecord=1, updatedtime=? WHERE id=?", (now_ms(), data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 商店 CRUD ──────────────────────────────────────────

@app.route('/api/items')
def list_items():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.itemname as name, s.price, s.icon, s.description,
                   s.stocknumber as count, s.shopcategoryid, s.createtime, s.isdisablepurchase,
                   i.stocknumber as inventory_count, i.id as inventory_id, i.isstarred
            FROM shopitemmodel s
            LEFT JOIN inventorymodel i ON s.inventorymodel_id = i.id
            WHERE s.isdel = 0
            ORDER BY s.shopcategoryid, s.orderincategory, s.id
        """)
        items = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, categoryname FROM shopcategorymodel WHERE isdelete=0")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}
        for i in items:
            i['category_name'] = cats.get(i.get('shopcategoryid'), '-')

        return jsonify(items)
    finally:
        conn.close()

@app.route('/api/items/add', methods=['POST'])
def add_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        # 先创建 inventory 记录
        cur.execute("INSERT INTO inventorymodel (createtime, stocknumber, updatetime, isstarred) VALUES (?, ?, ?, 0)",
                    (now, data.get('count', 0), now))
        inv_id = cur.lastrowid

        cur.execute("""
            INSERT INTO shopitemmodel (itemname, price, icon, description, stocknumber,
                shopcategoryid, createtime, isdel, isdisablepurchase, inventorymodel_id, remoteismine)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0)
        """, (
            data.get('name', '新商品'),
            data.get('price', 0),
            data.get('icon', ''),
            data.get('description', ''),
            data.get('count', -1),  # -1 = unlimited
            data.get('category_id', 0),
            now, inv_id
        ))
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/items/update', methods=['POST'])
def update_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE shopitemmodel SET itemname=?, price=?, icon=?, description=?,
                stocknumber=?, shopcategoryid=?, isdisablepurchase=?
            WHERE id=?
        """, (
            data.get('name'),
            data.get('price', 0),
            data.get('icon', ''),
            data.get('description', ''),
            data.get('count', -1),
            data.get('category_id', 0),
            data.get('isdisablepurchase', 0),
            data['id']
        ))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/items/delete', methods=['POST'])
def delete_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        # 先获取 inventorymodel_id
        cur.execute("SELECT inventorymodel_id FROM shopitemmodel WHERE id=?", (data['id'],))
        row = cur.fetchone()
        cur.execute("UPDATE shopitemmodel SET isdel=1 WHERE id=?", (data['id'],))
        if row and row['inventorymodel_id']:
            cur.execute("DELETE FROM inventorymodel WHERE id=?", (row['inventorymodel_id'],))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 成就 CRUD ──────────────────────────────────────────

@app.route('/api/achievements')
def list_achievements():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, content as name, description, type, categoryid, rewardcoin as coin,
                   icon, achievementstatus, currentvalue, progress,
                   createtime, finishtime, updatetime, isgotreward, targetcompletetime
            FROM userachievementmodel
            WHERE isdelete = 0
            ORDER BY categoryid, orderincategory, id
        """)
        ach_list = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, categoryname FROM userachcategorymodel WHERE isdelete=0")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}
        for a in ach_list:
            a['category_name'] = cats.get(a.get('categoryid'), '-')

        return jsonify(ach_list)
    finally:
        conn.close()

@app.route('/api/achievements/add', methods=['POST'])
def add_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            INSERT INTO userachievementmodel (content, description, type, categoryid, rewardcoin,
                icon, achievementstatus, currentvalue, progress, createtime, updatetime,
                isdelete, isgotreward, rewardcoinvariable, orderincategory, expreward)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0, 0, 0, 0, 0)
        """, (
            data.get('name', '新成就'),
            data.get('description', ''),
            data.get('type', 0),
            data.get('category_id', 0),
            data.get('coin', 0),
            data.get('icon', ''),
            now, now
        ))
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/update', methods=['POST'])
def update_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            UPDATE userachievementmodel SET content=?, description=?, type=?, categoryid=?,
                rewardcoin=?, icon=?, updatetime=?
            WHERE id=?
        """, (
            data.get('name'),
            data.get('description', ''),
            data.get('type', 0),
            data.get('category_id', 0),
            data.get('coin', 0),
            data.get('icon', ''),
            now,
            data['id']
        ))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/delete', methods=['POST'])
def delete_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE userachievementmodel SET isdelete=1, updatetime=? WHERE id=?",
                    (now_ms(), data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/system')
def system_achievements():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM achievementinfomodel ORDER BY achievementtype, levelnumber")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 技能/属性 ──────────────────────────────────────────

@app.route('/api/skills')
def list_skills():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, content as name, description, experience, type, color, icon, status,
                   groupid, orderincategory
            FROM skillmodel WHERE isdel=0 ORDER BY orderincategory
        """)
        skills = [dict(r) for r in cur.fetchall()]

        # 计算每个技能的经验进度
        cur.execute("SELECT perlevelexp FROM levelmodel WHERE id=1")
        ple = cur.fetchone()
        per_level = ple['perlevelexp'] if ple else 100

        for s in skills:
            exp = s.get('experience', 0) or 0
            s['level'] = max(1, exp // per_level + 1)
            s['current_exp'] = exp
            s['next_exp'] = (s['level']) * per_level
            s['progress'] = round((exp % per_level) / per_level * 100, 1)

        return jsonify(skills)
    finally:
        conn.close()

# ─── 分类列表 ───────────────────────────────────────────

@app.route('/api/categories/tasks')
def task_categories():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM categorymodel WHERE isdelete=0 AND categorytype=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

@app.route('/api/categories/shop')
def shop_categories():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM shopcategorymodel WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

@app.route('/api/categories/achievements')
def ach_categories():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM userachcategorymodel WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 背包 ───────────────────────────────────────────────

@app.route('/api/inventory')
def inventory():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT inv.id, inv.stocknumber, inv.isstarred, inv.extrainfo, inv.updatetime,
                   s.itemname as item_name, s.icon as item_icon, s.price, s.id as shop_id
            FROM inventorymodel inv
            JOIN shopitemmodel s ON inv.shopitemmodel_id = s.id
            WHERE inv.stocknumber > 0
            ORDER BY inv.isstarred DESC, inv.updatetime DESC
        """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 启动 ───────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    # 可选：启动时自动加载指定备份
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.exists(path):
            print(f'加载备份: {path}')
            load_backup(path)
        else:
            print(f'备份文件不存在: {path}')

    print('LifeUp 管理面板后端已启动 → http://localhost:5000')
    app.run(host='127.0.0.1', port=5000, debug=False)
