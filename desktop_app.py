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

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'last_backup_path': ''}

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

# ─── 暴露给前端的原生 API ──────────────────────────────────

class Api:
    def open_file_dialog(self):
        result = window.create_file_dialog(webview.OPEN_DIALOG, directory='',
                                           file_types=('LifeUp 存档 (*.zip)', '全部文件 (*.*)'))
        if result:
            p = result[0] if isinstance(result, (list, tuple)) else result
            if p:
                config['last_backup_path'] = p
                save_config(config)
                return p
        return ''

    def get_last_path(self):
        return config.get('last_backup_path', '')

    def get_version(self):
        return '1.0.0'


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
