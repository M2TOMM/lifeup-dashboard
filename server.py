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

    # 1. WAL checkpoint: 将所有未刷新的 WAL 写入主 DB 文件
    conn = sqlite3.connect(STATE['db_path'])
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        conn.close()

    # 2. 强制垃圾回收，关闭所有未关闭的连接
    import gc; gc.collect()

    # 3. 打包：包含 WAL/SHM 文件（如果 checkpoint 后仍存在）
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
        search = request.args.get('search', '').strip()
        cat_id = request.args.get('category_id', '')
        status_cond = {'all': '1=1', 'active': 'taskstatus=0', 'done': 'taskstatus>=1'}.get(filter_type, '1=1')
        search_cond = 'AND (t1.content LIKE ? OR t1.remark LIKE ?)' if search else ''
        search_params = [f'%{search}%', f'%{search}%'] if search else []
        cat_cond = f'AND t1.categoryid={int(cat_id)}' if cat_id else ''

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
                   t1.isfrozen, t1.taskurgencydegree as priority, t1.groupid, t1.tasktype
            FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0 AND {status_cond} {search_cond} {cat_cond}
            {dedup_sql}
            ORDER BY t1.taskurgencydegree DESC, t1.createdtime DESC
        """, search_params)
        tasks = [dict(r) for r in cur.fetchall()]

        # 获取分类名称
        cur.execute("SELECT id, categoryname FROM categorymodel")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}

        # 获取 tasktarget（目标次数）
        cur.execute("SELECT id, targettimes FROM tasktargetmodel")
        targets = {r['id']: r['targettimes'] for r in cur.fetchall()}

        # 获取技能关联
        cur.execute("SELECT taskmodel_id, skillids FROM taskmodel_skillids")
        skill_links = {}
        for r in cur.fetchall():
            skill_links.setdefault(r['taskmodel_id'], []).append(r['skillids'])
        # 获取技能名称
        cur.execute("SELECT id, content FROM skillmodel WHERE isdel=0")
        skill_names = {r['id']: r['content'] for r in cur.fetchall()}

        # 获取商品奖励
        cur.execute("SELECT id, taskmodelid, shopitemmodelid, amount FROM taskrewardmodel WHERE taskmodelid IS NOT NULL")
        reward_links = {}
        for r in cur.fetchall():
            reward_links.setdefault(r['taskmodelid'], []).append({'id': r['id'], 'item_id': r['shopitemmodelid'], 'amount': r['amount']})

        for t in tasks:
            t['category_name'] = cats.get(t.get('categoryid'), '-')
            t['target_count'] = targets.get(t.get('id'), 1)
            # 技能关联
            linked_skills = skill_links.get(t['id'], [])
            t['skill_ids'] = linked_skills
            t['skill_names'] = [skill_names.get(sid, '?') for sid in linked_skills]
            # 商品奖励
            t['item_rewards'] = reward_links.get(t['id'], [])

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
                0, ?, ?, ?, ?,
                0, 0, ?, 0, 0,
                ?, 0, 0,
                ?, ?, ?,
                -1, -1, 0, -1,
                0, 0, 0, 0,
                0, 0, ?,
                ?, ?
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
            0,
            target_id,
            data.get('tasktype', 0),
            data.get('priority', 1),
            data.get('attr1', ''),
            data.get('attr2', ''),
            data.get('attr3', ''),
            now,
            extrainfo,
            ''
        ))
        new_id = cur.lastrowid

        # 插入技能关联
        import json as _json
        skill_ids = data.get('skill_ids', [])
        if isinstance(skill_ids, str):
            skill_ids = _json.loads(skill_ids)
        for sid in skill_ids:
            cur.execute("INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)", (new_id, int(sid)))

        # 插入商品奖励
        item_rewards = data.get('item_rewards', [])
        if isinstance(item_rewards, str):
            item_rewards = _json.loads(item_rewards)
        for rw in item_rewards:
            cur.execute("INSERT INTO taskrewardmodel (taskmodelid, shopitemmodelid, amount, createtime, updatetime) VALUES (?, ?, ?, ?, ?)",
                        (new_id, int(rw['item_id']), int(rw.get('amount', 1)), now, now))

        conn.commit()
        return jsonify({'ok': True, 'id': new_id})
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
                remark=?, categoryid=?, updatedtime=?, taskdifficultydegree=?, tagcolor=?, taskurgencydegree=?
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

        # 更新属性关联
        if any(k in data for k in ('attr1', 'attr2', 'attr3')):
            cur.execute("UPDATE taskmodel SET relatedattribute1=?, relatedattribute2=?, relatedattribute3=? WHERE id=?",
                       (data.get('attr1', ''), data.get('attr2', ''), data.get('attr3', ''), data['id']))

        # 更新技能关联（先删后插）
        if 'skill_ids' in data:
            cur.execute("DELETE FROM taskmodel_skillids WHERE taskmodel_id=?", (data['id'],))
            import json as _json2
            skill_ids = data['skill_ids']
            if isinstance(skill_ids, str):
                skill_ids = _json2.loads(skill_ids)
            for sid in skill_ids:
                cur.execute("INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)", (data['id'], int(sid)))

        # 更新商品奖励（先删后插）
        if 'item_rewards' in data:
            cur.execute("DELETE FROM taskrewardmodel WHERE taskmodelid=?", (data['id'],))
            import json as _json3
            item_rewards = data['item_rewards']
            if isinstance(item_rewards, str):
                item_rewards = _json3.loads(item_rewards)
            for rw in item_rewards:
                cur.execute("INSERT INTO taskrewardmodel (taskmodelid, shopitemmodelid, amount, createtime, updatetime) VALUES (?, ?, ?, ?, ?)",
                            (data['id'], int(rw['item_id']), int(rw.get('amount', 1)), now, now))

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
        search = request.args.get('search', '').strip()
        category_id = request.args.get('category_id', '')
        cat_cond = 'AND s.shopcategoryid = ?' if category_id else ''
        search_cond = 'AND (s.itemname LIKE ? OR s.description LIKE ?)' if search else ''
        where_params = []
        if category_id:
            where_params.append(int(category_id))
        if search:
            where_params.extend([f'%{search}%', f'%{search}%'])
        cur.execute(f"""
            SELECT s.id, s.itemname as name, s.price, s.icon, s.description,
                   s.stocknumber as count, s.shopcategoryid, s.createtime, s.isdisablepurchase,
                   i.stocknumber as inventory_count, i.id as inventory_id, i.isstarred
            FROM shopitemmodel s
            LEFT JOIN inventorymodel i ON s.inventorymodel_id = i.id
            WHERE s.isdel = 0 {cat_cond} {search_cond}
            ORDER BY s.shopcategoryid, s.orderincategory, s.id
        """, where_params)
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
        search = request.args.get('search', '').strip()
        cat_id = request.args.get('category_id', '')
        search_cond = 'AND (content LIKE ? OR description LIKE ?)' if search else ''
        search_params = [f'%{search}%', f'%{search}%'] if search else []
        cat_cond = f'AND categoryid={int(cat_id)}' if cat_id else ''
        cur.execute(f"""
            SELECT id, content as name, description, type, categoryid, rewardcoin as coin,
                   icon, achievementstatus, currentvalue, progress,
                   createtime, finishtime, updatetime, isgotreward, targetcompletetime
            FROM userachievementmodel
            WHERE isdelete = 0 {search_cond} {cat_cond}
            ORDER BY categoryid, orderincategory, id
        """, search_params)
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
@app.route('/api/categories/items')
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
            ORDER BY inv.isstarred DESC, inv.updatetime DESC
        """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 合成配方 ───────────────────────────────────────────

@app.route('/api/synthesis')
def list_synthesis():
    conn = get_db()
    try:
        cur = conn.cursor()
        cat_id = request.args.get('category_id', '')
        search = request.args.get('search', '').strip()
        cat_cond = f'AND sm.categoryid={int(cat_id)}' if cat_id else ''
        search_params = []
        if search:
            search_params = [f'%{search}%', f'%{search}%']
            search_cond = 'AND (sm.name LIKE ? OR sm.id IN (SELECT sc2.synthesismodelid FROM synthesisconnmodel sc2 JOIN shopitemmodel sh ON sc2.shopitemmodelid = sh.id WHERE sh.itemname LIKE ? AND sc2.isdel=0))'
        else:
            search_cond = ''

        cur.execute(f"""
            SELECT sm.id, sm.name as title, sm.description as note,
                   sm.categoryid, sm.orderincategory, sm.createtime, sm.updatetime,
                   sc.categoryname
            FROM synthesismodel sm
            LEFT JOIN synthesiscategory sc ON sm.categoryid = sc.id
            WHERE sm.isdel=0 {cat_cond} {search_cond}
            ORDER BY sm.categoryid, sm.orderincategory, sm.id
        """, search_params)
        recipes = [dict(r) for r in cur.fetchall()]

        # 批量查所有配方关联
        if recipes:
            ids = ','.join(str(r['id']) for r in recipes)
            cur.execute(f"""
                SELECT sc.synthesismodelid, sc.isoutput, sc.amount, sc.shopitemmodelid,
                       s.itemname, s.icon
                FROM synthesisconnmodel sc
                JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
                WHERE sc.synthesismodelid IN ({ids}) AND sc.isdel=0
                ORDER BY sc.isoutput, sc.id
            """)
            conns = cur.fetchall()
            for recipe in recipes:
                rid = recipe['id']
                recipe['inputs'] = [{'item_id': c['shopitemmodelid'], 'item_name': c['itemname'],
                                     'icon': c['icon'], 'amount': c['amount']}
                                    for c in conns if c['synthesismodelid'] == rid and c['isoutput'] == 0]
                recipe['outputs'] = [{'item_id': c['shopitemmodelid'], 'item_name': c['itemname'],
                                      'icon': c['icon'], 'amount': c['amount']}
                                     for c in conns if c['synthesismodelid'] == rid and c['isoutput'] == 1]

        return jsonify({'recipes': recipes, 'total': len(recipes)})
    finally:
        conn.close()


@app.route('/api/synthesis/categories')
def synth_categories():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM synthesiscategory WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()


@app.route('/api/synthesis/add', methods=['POST'])
def add_synthesis():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        # 创建配方
        cur.execute("""
            INSERT INTO synthesismodel (name, description, categoryid, createtime, updatetime, isdel, orderincategory)
            VALUES (?, ?, ?, ?, ?, 0, 0)
        """, (data.get('title', '新配方'), data.get('note', ''), data.get('category_id', 1), now, now))
        recipe_id = cur.lastrowid

        # 输入材料
        for inp in data.get('inputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 0, ?, ?, 0, ?)
            """, (inp.get('amount', 1), now, inp['item_id'], recipe_id, now))

        # 输出产物
        for out in data.get('outputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 1, ?, ?, 0, ?)
            """, (out.get('amount', 1), now, out['item_id'], recipe_id, now))

        conn.commit()
        return jsonify({'ok': True, 'id': recipe_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/synthesis/update', methods=['POST'])
def update_synthesis():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        rid = data['id']

        # 更新配方基本信息
        cur.execute("""
            UPDATE synthesismodel SET name=?, description=?, categoryid=?, updatetime=?
            WHERE id=?
        """, (data.get('title'), data.get('note', ''), data.get('category_id', 1), now, rid))

        # 重新构建材料关联：先软删旧的全部
        cur.execute("UPDATE synthesisconnmodel SET isdel=1, updatetime=? WHERE synthesismodelid=? AND isdel=0",
                    (now, rid))

        for inp in data.get('inputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 0, ?, ?, 0, ?)
            """, (inp.get('amount', 1), now, inp['item_id'], rid, now))

        for out in data.get('outputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 1, ?, ?, 0, ?)
            """, (out.get('amount', 1), now, out['item_id'], rid, now))

        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/synthesis/delete', methods=['POST'])
def delete_synthesis():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("UPDATE synthesismodel SET isdel=1, updatetime=? WHERE id=?", (now, data['id']))
        cur.execute("UPDATE synthesisconnmodel SET isdel=1, updatetime=? WHERE synthesismodelid=?", (now, data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 合成计算器 ──────────────────────────────────────

@app.route('/api/synthesis/calculate')
def synthesis_calculate():
    """计算当前库存能合成哪些配方。返回：可合成 / 缺1件 / 缺多件"""
    conn = get_db()
    try:
        cur = conn.cursor()
        # 1. 从 inventoryrecordmodel 汇总实际库存（聚合增减记录）
        cur.execute("""
            SELECT shopitemmodel_id as item_id,
                   SUM(CASE WHEN isdecrease=0 THEN changenumber ELSE -changenumber END) as stock
            FROM inventoryrecordmodel
            WHERE isdel = 0
            GROUP BY shopitemmodel_id
            HAVING stock > 0
        """)
        inventory = {r['item_id']: r['stock'] for r in cur.fetchall()}

        # Fallback: inventorymodel 中有 stocknumber>0 的也合并
        cur.execute("""
            SELECT shopitemmodel_id as item_id, stocknumber
            FROM inventorymodel
            WHERE stocknumber > 0
        """)
        for r in cur.fetchall():
            if r['item_id'] not in inventory:
                inventory[r['item_id']] = r['stocknumber']
            else:
                inventory[r['item_id']] += r['stocknumber']

        # 2. 获取所有配方输入 (isoutput=0)
        cur.execute("""
            SELECT sc.synthesismodelid as recipe_id, sc.shopitemmodelid as item_id,
                   sc.amount as count
            FROM synthesisconnmodel sc
            JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
            WHERE sc.isoutput = 0 AND sc.isdel = 0 AND sm.isdel = 0
        """)
        recipe_inputs = {}
        for r in cur.fetchall():
            recipe_inputs.setdefault(r['recipe_id'], []).append({
                'item_id': r['item_id'], 'count': r['count']
            })

        # 3. 计算每个配方的完成度
        can_synth = []  # 可合成
        miss_one = []   # 缺1种
        miss_more = []  # 缺多种
        no_match = []   # 无库存

        for rid, inputs in recipe_inputs.items():
            owned_count = 0
            total_needed = len(inputs)
            for inp in inputs:
                stock = inventory.get(inp['item_id'], 0)
                if stock >= inp['count']:
                    owned_count += 1
            if total_needed == 0:
                continue
            ratio = owned_count / total_needed
            entry = {'recipe_id': rid, 'owned': owned_count, 'needed': total_needed, 'ratio': round(ratio, 2)}
            if ratio >= 1.0:
                can_synth.append(entry)
            elif owned_count == total_needed - 1 and total_needed > 1:
                miss_one.append(entry)
            elif owned_count > 0:
                miss_more.append(entry)
            else:
                no_match.append(entry)

        return jsonify({
            'inventory_count': len(inventory),
            'total_recipes': len(recipe_inputs),
            'can_synth': sorted(can_synth, key=lambda x: -x['ratio']),
            'miss_one': sorted(miss_one, key=lambda x: -x['ratio']),
            'miss_more': sorted(miss_more, key=lambda x: -x['ratio']),
            'no_match': sorted(no_match, key=lambda x: -x['ratio'])
        })
    finally:
        conn.close()


# ─── 活动时间线 ────────────────────────────────────────

@app.route('/api/history')
def activity_history():
    """合并任务完成历史 + 物品变动记录，按时间倒序"""
    conn = get_db()
    try:
        cur = conn.cursor()
        events = []
        from datetime import datetime

        # 1. 任务完成记录
        cur.execute("""
            SELECT id, content, taskstatus, updatedtime, currenttimes, categoryid,
                   rewardcoin, expreward, endtime, createdtime
            FROM taskmodel
            WHERE taskstatus >= 1 AND isdeleterecord = 0
            ORDER BY updatedtime DESC
            LIMIT 200
        """)
        for r in cur.fetchall():
            ts = r['updatedtime'] or r['endtime'] or r['createdtime']
            if ts and ts > 0:
                dt = datetime.fromtimestamp(ts / 1000)
                events.append({
                    'type': 'task',
                    'id': r['id'],
                    'name': r['content'],
                    'status': r['taskstatus'],
                    'count': r['currenttimes'],
                    'coin': r['rewardcoin'] or 0,
                    'exp': r['expreward'] or 0,
                    'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'ts': ts
                })

        # 2. 物品变动记录
        cur.execute("""
            SELECT ir.id, ir.createtime, ir.isdecrease, ir.changenumber, ir.desc_lpcolumn,
                   s.itemname, s.id as shop_id
            FROM inventoryrecordmodel ir
            JOIN shopitemmodel s ON ir.shopitemmodel_id = s.id
            WHERE 1=1
            ORDER BY ir.createtime DESC
            LIMIT 200
        """)
        for r in cur.fetchall():
            ts = r['createtime']
            if ts and ts > 0:
                dt = datetime.fromtimestamp(ts / 1000)
                events.append({
                    'type': 'item',
                    'id': r['id'],
                    'name': r['itemname'],
                    'shop_id': r['shop_id'],
                    'change': r['changenumber'],
                    'is_decrease': bool(r['isdecrease']),
                    'desc': r['desc_lpcolumn'] or '',
                    'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'ts': ts
                })

        events.sort(key=lambda e: e['ts'], reverse=True)

        return jsonify({
            'events': events,
            'task_count': sum(1 for e in events if e['type'] == 'task'),
            'item_count': sum(1 for e in events if e['type'] == 'item'),
            'total': len(events)
        })
    finally:
        conn.close()


# ─── 卡池管理 ──────────────────────────────────────────

@app.route('/api/pools')
def list_pools():
    """列出所有随机奖励型卡池（goodseffectmodel type=7）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ge.id, ge.shopitemid, s.itemname, s.icon, ge.relatedinfos,
                   ge.goodseffecttype, ge.values_lpcolumn
            FROM goodseffectmodel ge
            JOIN shopitemmodel s ON ge.shopitemid = s.id
            WHERE ge.goodseffecttype = 7 AND ge.isdel = 0
            ORDER BY ge.id
        """)
        pools = []
        for r in cur.fetchall():
            pool = dict(r)
            pool['entries'] = []
            try:
                info = json.loads(r['relatedinfos'] or '{}')
                items = info.get('itemsInfos', [])
                for item in items:
                    sid = item.get('shopItemModelId', item.get('shopItemModelID', 0))
                    cur2 = conn.cursor()
                    cur2.execute("SELECT itemname, icon FROM shopitemmodel WHERE id=? AND isdel=0", (sid,))
                    srow = cur2.fetchone()
                    pool['entries'].append({
                        'item_id': sid,
                        'item_name': srow['itemname'] if srow else f'(已删除#{sid})',
                        'icon': srow['icon'] if srow else '',
                        'probability': item.get('probability', 0),
                        'is_fixed': item.get('isFixedReward', False),
                        'amount': item.get('amount', 1)
                    })
            except Exception as e:
                pool['parse_error'] = str(e)
            pools.append(pool)

        cur.execute("""
            SELECT ge.id, ge.shopitemid, s.itemname, s.icon, ge.relatedinfos,
                   ge.goodseffecttype, ge.values_lpcolumn
            FROM goodseffectmodel ge
            JOIN shopitemmodel s ON ge.shopitemid = s.id
            WHERE ge.goodseffecttype IN (2, 4, 5, 6, 9) AND ge.isdel = 0
            ORDER BY ge.goodseffecttype, ge.id
        """)
        simple_effects = []
        for r in cur.fetchall():
            ef = dict(r)
            ef['entries'] = []
            ef['effect_label'] = {1: '属性', 2: '资源', 4: '经验', 5: '解锁', 6: '成就', 9: '其他'}.get(r['goodseffecttype'], f'type{r["goodseffecttype"]}')
            simple_effects.append(ef)

        return jsonify({
            'pools': pools,
            'simple_effects': simple_effects,
            'pool_count': len(pools),
            'effect_count': len(simple_effects)
        })
    finally:
        conn.close()


@app.route('/api/pools/update', methods=['POST'])
def update_pool():
    """更新卡池内容（概率/物品/固定奖励等）"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        effect_id = data['id']

        entries = data.get('entries', [])
        items_infos = []
        for e in entries:
            items_infos.append({
                'amount': e.get('amount', 1),
                'isFixedReward': e.get('is_fixed', False),
                'probability': e.get('probability', 100),
                'shopItemModelId': e.get('item_id', 0)
            })
        new_json = json.dumps({'itemsInfos': items_infos})

        cur.execute("""
            UPDATE goodseffectmodel
            SET relatedinfos = ?, updatetime = ?
            WHERE id = ?
        """, (new_json, now, effect_id))

        conn.commit()
        return jsonify({'ok': True, 'entries': len(items_infos)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/pools/add', methods=['POST'])
def add_pool():
    """创建新的卡池效果"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()

        entries = data.get('entries', [])
        items_infos = []
        for e in entries:
            items_infos.append({
                'amount': e.get('amount', 1),
                'isFixedReward': e.get('is_fixed', False),
                'probability': e.get('probability', 100),
                'shopItemModelId': e.get('item_id', 0)
            })
        relatedinfos = json.dumps({'itemsInfos': items_infos})

        cur.execute("""
            INSERT INTO goodseffectmodel (createtime, shopitemid, goodseffecttype, relatedinfos, isdel, updatetime, relatedid, values_lpcolumn)
            VALUES (?, ?, 7, ?, 0, ?, 0, 0)
        """, (now, data['shopitemid'], relatedinfos, now))

        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/pools/search_cards')
def search_cards_for_pool():
    """搜索可加入卡池的物品"""
    q = request.args.get('q', '')
    conn = get_db()
    try:
        cur = conn.cursor()
        if q:
            cur.execute("""
                SELECT id, itemname, icon FROM shopitemmodel
                WHERE isdel = 0 AND itemname LIKE ?
                ORDER BY itemname
                LIMIT 50
            """, (f'%{q}%',))
        else:
            cur.execute("""
                SELECT id, itemname, icon FROM shopitemmodel
                WHERE isdel = 0 AND (itemname LIKE 'N-%' OR itemname LIKE 'R-%'
                       OR itemname LIKE 'SR-%' OR itemname LIKE 'SSR-%')
                ORDER BY
                    CASE
                        WHEN itemname LIKE 'N-%' THEN 1
                        WHEN itemname LIKE 'R-%' THEN 2
                        WHEN itemname LIKE 'SR-%' THEN 3
                        WHEN itemname LIKE 'SSR-%' THEN 4
                        ELSE 5
                    END, itemname
                LIMIT 200
            """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()


# ─── 成就进度一览 ──────────────────────────────────────

@app.route('/api/achievements/progress')
def achievements_progress():
    """用户成就进度一览（含系统成就条件对照）"""
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT ua.id, ua.content, ua.description, ua.type, ua.categoryid,
                   ua.rewardcoin, ua.icon, ua.achievementstatus, ua.currentvalue,
                   ua.progress, ua.createtime, ua.finishtime,
                   uac.categoryname
            FROM userachievementmodel ua
            LEFT JOIN userachcategorymodel uac ON ua.categoryid = uac.id
            WHERE ua.isdelete = 0
            ORDER BY uac.orderincategory, ua.orderincategory, ua.id
        """)
        user_achs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, title, desc_lpcolumn, achievementid, hasfinished, isgotreward
            FROM achievementmodel
        """)
        sys_achs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, userachievementid, currentvalue, targetvalues, conditiontype,
                   progress, relatedinfos, relatedids
            FROM unlockconditionmodel
            WHERE isdel = 0
        """)
        conditions = {}
        for r in cur.fetchall():
            cond = dict(r)
            ua_id = cond['userachievementid']
            if ua_id not in conditions:
                conditions[ua_id] = []
            conditions[ua_id].append(cond)

        for a in user_achs:
            a['conditions'] = conditions.get(a['id'], [])
            a['status_label'] = {0: '未完成', 1: '已完成', 2: '已领取奖励'}.get(a['achievementstatus'], '未知')
            a['total_progress'] = 0
            if a['conditions']:
                total_target = sum(c.get('targetvalues', 0) or 0 for c in a['conditions'])
                total_current = sum(c.get('currentvalue', 0) or 0 for c in a['conditions'])
                if total_target > 0:
                    a['total_progress'] = round(total_current / total_target * 100, 1)
                    a['current_total'] = total_current
                    a['target_total'] = total_target

        return jsonify({
            'achievements': user_achs,
            'system_achievements': sys_achs,
            'total': len(user_achs),
            'completed': sum(1 for a in user_achs if a['achievementstatus'] >= 1)
        })
    finally:
        conn.close()


# ─── 卡牌图鉴 ───────────────────────────────────────────

@app.route('/api/collection')
def card_collection():
    """卡牌收集进度（按稀有度分组）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        search = request.args.get('search', '').strip()
        rarity = request.args.get('rarity', '').strip().upper()
        search_cond = 'AND s.itemname LIKE ?' if search else ''
        rarity_cond = ''
        where_params = []
        if search:
            where_params.append(f'%{search}%')
        if rarity in ('N', 'R', 'SR', 'SSR'):
            rarity_cond = f'AND s.itemname LIKE ?'
            where_params.append(f'{rarity}-%')
        cur.execute(f"""
            SELECT s.id, s.itemname, s.icon, s.price, s.description,
                   COALESCE(inv.stocknumber, 0) as owned
            FROM shopitemmodel s
            LEFT JOIN inventorymodel inv ON s.id = inv.shopitemmodel_id
            WHERE s.isdel = 0 AND (
                s.itemname LIKE 'N-%' OR s.itemname LIKE 'R-%' OR
                s.itemname LIKE 'SR-%' OR s.itemname LIKE 'SSR-%'
            ) {search_cond} {rarity_cond}
            ORDER BY
                CASE
                    WHEN s.itemname LIKE 'SSR-%' THEN 1
                    WHEN s.itemname LIKE 'SR-%' THEN 2
                    WHEN s.itemname LIKE 'R-%' THEN 3
                    WHEN s.itemname LIKE 'N-%' THEN 4
                    ELSE 5
                END, s.itemname
        """, where_params)
        cards = [dict(r) for r in cur.fetchall()]

        groups = {'SSR': [], 'SR': [], 'R': [], 'N': []}
        for c in cards:
            name = c['itemname']
            for r in ['SSR', 'SR', 'R', 'N']:
                if name.startswith(r + '-'):
                    groups[r].append(c)
                    break

        stats = {r: {'total': len(groups[r]), 'owned': sum(1 for c in groups[r] if c['owned'] > 0)} for r in groups}
        total = sum(s['total'] for s in stats.values())
        total_owned = sum(s['owned'] for s in stats.values())

        return jsonify({
            'groups': groups,
            'stats': stats,
            'total': total,
            'total_owned': total_owned,
            'completion_rate': round(total_owned / total * 100, 1) if total > 0 else 0
        })
    finally:
        conn.close()


# ─── 批量操作 ──────────────────────────────────────────

@app.route('/api/tasks/batch', methods=['POST'])
def batch_tasks():
    data = request.get_json()
    ids = data.get('ids', [])
    action = data.get('action', 'disable')
    if not ids:
        return jsonify({'error': '请选择至少一个任务'}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        ph = ','.join('?' * len(ids))
        if action == 'disable':
            cur.execute(f"UPDATE taskmodel SET taskstatus=0, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'enable':
            cur.execute(f"UPDATE taskmodel SET taskstatus=1, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'delete':
            cur.execute(f"UPDATE taskmodel SET isdeleterecord=1, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        conn.commit()
        return jsonify({'ok': True, 'affected': cur.rowcount})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/items/batch', methods=['POST'])
def batch_items():
    data = request.get_json()
    ids = data.get('ids', [])
    action = data.get('action', 'disable')
    price = data.get('price')
    if not ids:
        return jsonify({'error': '请选择至少一个商品'}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        ph = ','.join('?' * len(ids))
        if action == 'disable':
            cur.execute(f"UPDATE shopitemmodel SET purchasable=0, updatetime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'enable':
            cur.execute(f"UPDATE shopitemmodel SET purchasable=1, updatetime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'price' and price is not None:
            cur.execute(f"UPDATE shopitemmodel SET price=?, updatetime=? WHERE id IN ({ph})", [price, now] + ids)
        elif action == 'delete':
            cur.execute(f"UPDATE shopitemmodel SET isdel=1, updatetime=? WHERE id IN ({ph})", [now] + ids)
        conn.commit()
        return jsonify({'ok': True, 'affected': cur.rowcount})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ─── 经济可视化 ────────────────────────────────────────

@app.route('/api/economy')
def economy_stats():
    """经济数据聚合（按日汇总 coin/exp）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        from datetime import datetime, timedelta

        cur.execute("""
            SELECT updatedtime, rewardcoin, expreward, content
            FROM taskmodel
            WHERE taskstatus >= 1 AND isdeleterecord = 0 AND (rewardcoin > 0 OR expreward > 0)
            ORDER BY updatedtime
        """)
        daily = {}
        for r in cur.fetchall():
            ts = r['updatedtime']
            if not ts or ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000)
            day = dt.strftime('%Y-%m-%d')
            if day not in daily:
                daily[day] = {'coin': 0, 'exp': 0, 'count': 0}
            daily[day]['coin'] += (r['rewardcoin'] or 0)
            daily[day]['exp'] += (r['expreward'] or 0)
            daily[day]['count'] += 1

        cur.execute("""
            SELECT ir.createtime, ir.isdecrease, ir.changenumber, ir.desc_lpcolumn,
                   s.itemname, s.price
            FROM inventoryrecordmodel ir
            JOIN shopitemmodel s ON ir.shopitemmodel_id = s.id
            ORDER BY ir.createtime
        """)
        for r in cur.fetchall():
            ts = r['createtime']
            if not ts or ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000)
            day = dt.strftime('%Y-%m-%d')
            if day not in daily:
                daily[day] = {'coin': 0, 'exp': 0, 'count': 0}
            if r['isdecrease'] == 0:
                item_value = (r['price'] or 0) * (r['changenumber'] or 1)
                daily[day]['coin'] += item_value

        days = [{'date': d, **v} for d, v in sorted(daily.items())]
        total_coin = sum(d['coin'] for d in days)
        total_exp = sum(d['exp'] for d in days)
        avg_daily_coin = round(total_coin / max(len(days), 1), 1)

        today = datetime.now()
        last_14 = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            if d in daily:
                last_14.append({'date': d, **daily[d]})
            else:
                last_14.append({'date': d, 'coin': 0, 'exp': 0, 'count': 0})

        return jsonify({
            'daily': days,
            'last_14': last_14,
            'total_coin': total_coin,
            'total_exp': total_exp,
            'avg_daily_coin': avg_daily_coin,
            'days': len(days)
        })
    finally:
        conn.close()


# ─── 数据导出 ─────────────────────────────────────────

@app.route('/api/load', methods=['POST'])
def api_load():
    """通过路径重新加载备份"""
    data = request.get_json()
    path = data.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'ok': False, 'error': '文件不存在'}), 404
    try:
        load_backup(path)
        STATE['backup_path'] = path
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT goldcoin FROM usermodel LIMIT 1")
            coin = cur.fetchone()
            return jsonify({'ok': True, 'tables': len(tables), 'coin': coin[0] if coin else 0})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/export/<table>')
def export_table(table):
    """导出表格数据为 JSON/CSV"""
    allowed = {
        'tasks': 'SELECT id, content as title, taskfrequency as frequency, rewardcoin as coin, expreward as exp, taskstatus as done, currenttimes as done_count, categoryid, remark as note, createdtime, updatedtime FROM taskmodel WHERE isdeleterecord=0',
        'items': 'SELECT id, itemname as name, price, description, isdisablepurchase, icon, shopcategoryid, stocknumber as count, createtime FROM shopitemmodel WHERE isdel=0',
        'inventory': 'SELECT inv.id, inv.shopitemmodel_id, s.itemname, inv.stocknumber as count FROM inventorymodel inv JOIN shopitemmodel s ON inv.shopitemmodel_id=s.id',
        'achievements': 'SELECT id, content as name, description, type, categoryid, rewardcoin as coin, icon, achievementstatus, currentvalue, progress, createtime, finishtime, updatetime FROM userachievementmodel WHERE isdelete=0',
        'skills': 'SELECT id, content as name, description, experience as exp, color, icon, groupid FROM skillmodel WHERE isdel=0',
        'history': 'SELECT updatedtime as time, content as title, rewardcoin as coin, expreward as exp FROM taskmodel WHERE taskstatus>=1 AND isdeleterecord=0 ORDER BY updatedtime DESC',
    }
    if table not in allowed:
        return jsonify({'error': 'Unknown table'}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(allowed[table])
        rows = [dict(r) for r in cur.fetchall()]
        fmt = request.args.get('format', 'json')
        if fmt == 'csv':
            if not rows:
                return '', 200, {'Content-Type': 'text/csv'}
            import io, csv
            out = io.StringIO()
            keys = list(rows[0].keys())
            w = csv.DictWriter(out, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
            resp = out.getvalue()
            return resp, 200, {'Content-Type': 'text/csv; charset=utf-8'}
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
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
