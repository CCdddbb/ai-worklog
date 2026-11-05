"""First-run guidance derived only from local, already-imported metadata.

This is a read-only view. It never imports sessions, invokes a model, changes
consent, or guesses which database the user meant to open.
"""
import json

from .adapters import GAP_KEYS
from .core import bounds
from .store import connect

# The browser cannot select a database path (source directories and the private
# SQLite file are fixed by the launch command). Guidance therefore explains how
# to relaunch with an existing database instead of offering a path picker.
DATABASE_HINT = {
    'command': 'python3 app.py start --db <已有数据库文件>',
    'note': '当前数据库由启动命令的 --db 决定，浏览器不能切换或指定路径。'
            '如果之前已经记录过工作，请用同一个数据库路径重新启动，不要新建空库。',
}


def coverage_for(db, day, zone):
    """Same day/timezone scoping as report_data; never trusts another day's scan."""
    result = {}
    for row in db.execute('SELECT source,coverage FROM import_days WHERE day=? AND zone=?', (day, zone)):
        result[row['source']] = json.loads(row['coverage'])
    for row in db.execute('SELECT source,coverage FROM imports'):
        item = json.loads(row['coverage'])
        if item.get('date') == day and item.get('timezone', zone) == zone:
            result.setdefault(row['source'], dict(item, imported_at=row['imported_at']))
    return result


def read_gaps(coverage):
    """Only structural read problems count as gaps, scoped to this day's imports."""
    gaps = {}
    for source, item in coverage.items():
        found = {key: item[key] for key in GAP_KEYS
                 if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)
                 and item[key] > 0}
        if found:
            gaps[source] = found
    return gaps


def onboarding_state(path, day, zone):
    start, end = bounds(day, zone)
    db = connect(path)
    try:
        coverage = coverage_for(db, day, zone)
        events = db.execute('SELECT COUNT(*) FROM events WHERE ts>=? AND ts<?', (start, end)).fetchone()[0]
        sessions = db.execute('SELECT COUNT(DISTINCT session) FROM events WHERE ts>=? AND ts<?',
                              (start, end)).fetchone()[0]
        tasks = db.execute('SELECT COUNT(*) FROM tasks WHERE day=?', (day,)).fetchone()[0]
        automatic = db.execute('SELECT COUNT(*) FROM work_records WHERE day=?', (day,)).fetchone()[0]
        reviews = db.execute('SELECT COUNT(*) FROM task_reviews r JOIN tasks t ON t.id=r.task_id '
                             'WHERE t.day=?', (day,)).fetchone()[0]
        lifetime_events = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        lifetime_imports = db.execute('SELECT COUNT(*) FROM imports').fetchone()[0]
    finally:
        db.close()

    gaps = read_gaps(coverage)
    facts = {'sources': sorted(coverage), 'events': events, 'sessions': sessions,
             'task_count': tasks, 'automatic_count': automatic, 'review_count': reviews,
             'lifetime_events': lifetime_events, 'lifetime_imports': lifetime_imports,
             'gaps': gaps}
    result = {'state': 'ready', 'title': '', 'message': '', 'actions': [],
              'facts': facts, 'database': DATABASE_HINT}

    if any(item.get('synthetic') for item in coverage.values()):
        result.update(state='demo', title='当前是演示数据',
                      message='页面中的记录全部为虚构数据，不代表你的真实工作；不会据此生成真实日报。',
                      actions=[{'label': '查看数据与隐私', 'action': 'sources'}])
    elif gaps:
        result.update(state='incomplete', title='采集有缺口，先确认覆盖再整理',
                      message='所选日期的导入存在读取缺口，记录可能不完整。缺口是扫描限制，不是「没有工作」；'
                              '手工记录无需等待修复，也不需要模型授权。',
                      actions=[{'label': '查看导入覆盖', 'action': 'sources'},
                               {'label': '直接手工记录', 'action': 'triage'}])
    elif not coverage and not events and (lifetime_events or lifetime_imports):
        # A quiet day in an established database is not an empty database.
        result.update(state='empty_day', title='所选日期还没有导入记录',
                      message='这个数据库里已有其他日期的记录，只是所选日期尚未导入。先运行 import 导入当天会话；'
                              '未记录不等于没有工作，也可以直接手工新建一条记录。',
                      actions=[{'label': '新建手工记录', 'action': 'creation'},
                               {'label': '查看导入覆盖', 'action': 'sources'}])
    elif not coverage and not events:
        result.update(state='empty', title='这个数据库还没有任何会话记录',
                      message='所选日期没有导入任何本地会话。如果你之前记录过工作，很可能是启动时打开了另一个数据库。'
                              '也可以不依赖 AI 会话，直接手工新建一条工作记录。',
                      actions=[{'label': '新建手工记录', 'action': 'creation'},
                               {'label': '查看导入覆盖', 'action': 'sources'}])
    elif not tasks and not automatic:
        result.update(state='unorganized', title=f'已导入 {sessions} 个会话，还没有整理成工作记录',
                      message='会话只是活动线索，不会自动变成成果。可以直接手工记录，无需启用模型或授权摘录；'
                              'AI 草稿需要另行确认。',
                      actions=[{'label': '开始整理', 'action': 'triage'},
                               {'label': '查看导入覆盖', 'action': 'sources'}])
    return result
