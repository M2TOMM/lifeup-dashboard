# -*- coding: utf-8 -*-
"""
LifeUp 存档管理器 - 桌面版
双击运行，原生窗口，无需浏览器。
"""
import sys, os, json, threading, time, logging
from pathlib import Path

logging.getLogger('werkzeug').setLevel(logging.ERROR)

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

import webview

# 切换工作目录到资源目录，确保 Flask 能找到 index.html
os.chdir(BASE_DIR)
import server as server_module

app = server_module.app
STATE = server_module.STATE

# ─── 配置持久化 ────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.expanduser('~'), '.lifeup_dashboard_config.json')
HISTORY_MAX = 10

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        # 确保必要字段存在
        cfg.setdefault('last_backup_path', '')
        cfg.setdefault('last_dir', '')
        cfg.setdefault('history', [])
        return cfg
    return {'last_backup_path': '', 'last_dir': '', 'history': []}

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

def _add_to_history(paths):
    """将路径添加到历史记录最前面，去重，限制数量"""
    history = config.get('history', [])
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        # 移除旧记录
        history = [h for h in history if h.get('path') != p]
        # 插入最前
        history.insert(0, {
            'path': p,
            'name': os.path.basename(p),
            'time': int(time.time())
        })
    config['history'] = history[:HISTORY_MAX]
    if paths:
        config['last_backup_path'] = paths[0]
        config['last_dir'] = os.path.dirname(paths[0])
    save_config(config)

# ─── 暴露给前端的原生 API ──────────────────────────────────

class Api:
    def open_file_dialog(self, allow_multiple=False):
        """打开文件选择对话框，支持多选"""
        last_dir = config.get('last_dir', '')
        if last_dir and not os.path.isdir(last_dir):
            last_dir = ''
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=bool(allow_multiple),
            directory=last_dir,
            file_types=('LifeUp 存档 (*.zip)', '全部文件 (*.*)')
        )
        if result:
            paths = result if isinstance(result, (list, tuple)) else [result]
            paths = [p for p in paths if p]
            if paths:
                _add_to_history(paths)
                # 返回第一个（单文件模式）或全部（多文件模式）
                if allow_multiple:
                    return paths
                return paths[0]
        return '' if not allow_multiple else []

    def get_last_path(self):
        return config.get('last_backup_path', '')

    def get_last_dir(self):
        return config.get('last_dir', '')

    def get_history(self):
        """获取最近的历史记录，过滤掉不存在的文件"""
        history = config.get('history', [])
        return [h for h in history if os.path.exists(h.get('path', ''))]

    def clear_history(self):
        config['history'] = []
        save_config(config)
        return True

    def remove_history_item(self, path):
        """从历史中移除一条记录"""
        config['history'] = [h for h in config.get('history', []) if h.get('path') != path]
        save_config(config)
        return True

    def save_last_path(self, path):
        """保存最近使用的路径"""
        if path and os.path.exists(path):
            config['last_backup_path'] = path
            config['last_dir'] = os.path.dirname(path)
            save_config(config)
        return True

    def get_version(self):
        return '1.1.0'


def start_flask():
    app.run(host='127.0.0.1', port=55678, debug=False, use_reloader=False)


def main():
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.5)

    window = webview.create_window(
        title='LifeUp 存档管理器',
        url='http://127.0.0.1:55678',
        width=1200,
        height=800,
        min_size=(900, 600),
        js_api=Api(),
        text_select=True,
        confirm_close=True
    )

    webview.start(debug=False)
    sys.exit(0)


if __name__ == '__main__':
    main()
