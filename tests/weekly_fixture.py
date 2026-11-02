"""Multi-day synthetic workbench for visual acceptance. No source scans or models."""
import tempfile
from pathlib import Path

from worklog.adapters import event
from worklog.core import bounds
from worklog.store import connect, save
from worklog.web import make_server


def populate(path):
    for offset, day in enumerate(['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-30']):
        start, _ = bounds(day, 'Asia/Shanghai')
        rows = [event(b'v' * 32, 'demo', f'week-{offset}', 'synthetic', f'{day}-{i}',
                      start + 9 * 3600 + i * 60, 'assistant', 'demo', 'Model-A',
                      values={'tokens': (offset + 1) * 125}) for i in range((offset + 1) * 4)]
        save(path, rows, {'source': 'demo', 'synthetic': True, 'selected': 0}, day, 'Asia/Shanghai')
        db = connect(path)
        with db:
            for index, (title, status, outcome, next_steps) in enumerate([
                ('虚构：登录回调修复', 'confirmed', '已补齐状态参数并完成本地验证。', '生产回归待安排。'),
                ('虚构：筛选条件回归', 'blocked', '仍有两个回归用例失败。', '先修复清空筛选的边界行为。'),
                ('虚构：桌面版验证', 'in_progress', '原生窗口与本地服务已打通。', '继续检查退出与升级。'),
            ][:offset + 1]):
                task_id = f'{offset * 10 + index:032x}'
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                           (task_id, title, day, '[]', start, None, 'open', start))
                db.execute('INSERT INTO task_reviews VALUES (?,?,?,?,?,?,?)',
                           (task_id, status, outcome, '', next_steps, '仅用于合成界面验收。', start))
        db.close()
    save(path, [], {'source': 'demo', 'synthetic': True, 'selected': 0}, '2026-10-29', 'Asia/Shanghai')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='worklog-week-visual-') as directory:
        path = Path(directory) / 'week.sqlite3'
        populate(path)
        server = make_server(path, '2026-10-28', 'Asia/Shanghai', 0)
        print(f'http://127.0.0.1:{server.server_port}/#token={server.access_token}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
