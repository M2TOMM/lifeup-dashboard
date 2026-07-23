import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DemoPageLabelTests(unittest.TestCase):
    def test_review_and_goals_no_longer_render_demo_data(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js runtime is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
let source = '';
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) source += match[1] + '\n';
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
const content = { innerHTML: '', classList, style: {} };
const document = {
  body: { classList }, getElementById: (id) => id === 'content' ? content : null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => 'local', setItem: noop, removeItem: noop };
const unavailable = (label) => ({ label, available: false, value: null, previous_value: null, current_records: [], previous_records: [], missing_reason: '暂无真实流水' });
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: async (url) => ({ ok: true, json: async () => url.startsWith('/api/review') ? ({
    meta: { source: 'local', source_label: '本地备份数据' },
    window: { label: '本周', comparison_label: '上周', start: '2026-07-13', end: '2026-07-19', previous_start: '2026-07-06', previous_end: '2026-07-12' },
    metrics: {
      focus_minutes: unavailable('番茄专注时长'), tasks_completed: unavailable('完成任务数'),
      coin_change: unavailable('金币净变化'), exp_change: unavailable('经验净变化'),
      achievements_completed: unavailable('完成成就数')
    }, series: [], insights: [], gaps: ['暂无真实流水']
  }) : ({
    meta: { source: 'local', config_source: 'lifeup_goal_mappings.json' },
    configured: false, config: { version: 1, goals: [] }, config_error: '',
    category_options: { tasks: [], achievements: [] }, goals: []
  }) }), confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  await sandbox.loadReview();
  if (!content.innerHTML.includes('本地备份数据') || !content.innerHTML.includes('暂无真实流水')) {
    throw new Error('review page lacks real-source and data-gap labels');
  }
  if (content.innerHTML.includes('演示数据')) {
    throw new Error('review page still renders demo data');
  }
  await sandbox.loadGoals();
  if (!content.innerHTML.includes('尚未配置真实宏愿') || !content.innerHTML.includes('不会生成随机数字')) {
    throw new Error('goals page lacks the real-data empty-state guide');
  }
  if (content.innerHTML.includes('演示数据')) {
    throw new Error('goals page still renders demo data');
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
