"""Compact, project-scoped timeline of automatic work records.

A node is one work record, not one conversation turn. The full problem/actions/
verification text stays in SQLite and is loaded only when a node is opened, so the
dashboard and delivery report no longer render one large card per tiny update.
"""
from collections import defaultdict

from .ledger import CATEGORIES, STATUS_LABELS, work_record_data

UNASSIGNED = '__unassigned__'
UNKNOWN_PROJECT = '未识别名称的项目'
SUMMARY_LIMIT = 160
NOTE = ('按项目归并同一天的新增与更新，每个圆点是一条工作记录，不是一次对话；'
        '跨项目记录只归入主项目，避免重复计数。时间为活动标记估计，不等于工时；'
        '自动归档不等于人工验收。')


def _clip(text):
    one_line = ' '.join(str(text or '').split())
    if not one_line:
        return ''
    return one_line[:SUMMARY_LIMIT] + ('…' if len(one_line) > SUMMARY_LIMIT else '')


def _project_names(db, ids):
    names = {}
    for project_id in ids:
        if project_id == UNASSIGNED:
            names[project_id] = '未识别项目'
            continue
        canonical = db.execute('SELECT project FROM project_aliases WHERE alias=?', (project_id,)).fetchone()
        key = canonical[0] if canonical else project_id
        row = db.execute('SELECT name FROM projects WHERE id=?', (key,)).fetchone()
        names[project_id] = row[0] if row and row[0] else UNKNOWN_PROJECT
    return names


def timeline_data(path, day, zone, period='day', category=None, state='all'):
    """Read-only projection of work_record_data: one line per record, grouped by project."""
    from .store import connect
    value = work_record_data(path, day, zone, period, category, state)
    db = connect(path)
    try:
        names = _project_names(db, {project for record in value['records'] for project in (record['projects'] or [UNASSIGNED])})
        nodes = []
        for record in value['records']:
            projects = record['projects'] or [UNASSIGNED]
            work = record['work']
            outcome = work.get('outcome') or {}
            problem = work.get('problem') or {}
            nodes.append({
                'id': record['id'], 'day': record['day'], 'title': record['title'],
                'category': record['category'], 'category_label': CATEGORIES.get(record['category'], CATEGORIES['other']),
                'status': record['status'], 'status_label': STATUS_LABELS.get(record['status'], record['status']),
                'archived': record['archived'], 'updates': record['updates'],
                'first_ts': record['first_ts'], 'last_ts': record['last_ts'], 'updated_at': record['updated_at'],
                'active_seconds': record['active_seconds'], 'span_seconds': record['span_seconds'],
                'summary': _clip(outcome.get('text') or problem.get('text')),
                'project_id': projects[0], 'project_name': names.get(projects[0], UNKNOWN_PROJECT),
                'other_projects': [names.get(item, UNKNOWN_PROJECT) for item in projects[1:]],
            })
    finally:
        db.close()
    nodes.sort(key=lambda node: (node['day'], node['first_ts'], node['updated_at']), reverse=True)
    grouped = defaultdict(list)
    for node in nodes:
        grouped[node['project_id']].append(node)
    projects = []
    for project_id, members in grouped.items():
        projects.append({
            'id': project_id, 'name': names.get(project_id, UNKNOWN_PROJECT),
            'count': len(members), 'active_seconds': sum(node['active_seconds'] for node in members),
            'archived': sum(node['archived'] for node in members),
            'blocked': sum(node['status'] == 'blocked' for node in members),
            'latest_ts': max((node['first_ts'] for node in members), default=0),
            'node_ids': [node['id'] for node in members],
        })
    projects.sort(key=lambda item: (-item['latest_ts'], item['name']))
    return {'first': value['first'], 'last': value['last'], 'nodes': nodes, 'projects': projects,
            'project_count': len(projects), 'stats': value['stats'], 'categories': value['categories'],
            'category_options': value['category_options'], 'truncated': value['truncated'],
            'time_note': value['time_note'], 'note': NOTE}
