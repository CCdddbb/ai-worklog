"""Automatic business-task ledger. AI text supplies meaning; timestamps supply time."""
import json
import re
import uuid
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .core import TIMEOUT, bounds, union_seconds

CATEGORIES = {'feature': '功能开发', 'bugfix': '问题修复', 'refactor': '重构优化', 'testing': '测试验证',
              'docs': '文档整理', 'design': '方案讨论', 'ops': '运行运维', 'other': '其他工作'}
STATUS_LABELS = {'reported_resolved': '已报告解决', 'in_progress': '进行中', 'blocked': '受阻', 'discussed': '方案与讨论'}
TIME_NOTE = '活跃时长是相邻日志标记间隔不超过5分钟的区间估计，并发区间去重后分摊；不是精确工时。跨度含等待，不等于投入。'


def classify(group):
    if group.get('category') in CATEGORIES:
        return group['category']
    text = group['title'] + ' ' + group['problem']['text']
    if group['status'] == 'discussed':
        return 'design'
    for category, pattern in [('bugfix', r'修复|故障|报错|异常|丢失|失效|失败|bug'),
                              ('testing', r'测试|回归|验收|用例'), ('docs', r'文档|说明书|手册|报告编写'),
                              ('ops', r'部署|安装|启动|运维|环境配置|打包'),
                              ('refactor', r'重构|性能|优化|清理|拆分'), ('design', r'方案|架构|可行性|评估')]:
        if re.search(pattern, text, re.IGNORECASE):
            return category
    return 'feature' if group['actions'] else 'other'


def _title_key(title):
    return re.sub(r'[\W_]|修复|实现|完善|解决|问题|功能|助手报告', '', title).lower()


def _match(existing, group, projects, anchors, used):
    candidates = []
    for record in existing:
        if record['id'] in used or json.loads(record['projects_json']) != projects:
            continue
        similarity = SequenceMatcher(None, _title_key(record['title']), _title_key(group['title'])).ratio()
        overlap = bool(set(json.loads(record['anchors_json'])) & set(anchors))
        if similarity >= .78 or (overlap and similarity >= .35):
            candidates.append((similarity + int(overlap), record))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _source_rows(db, task, data):
    sessions = json.loads(task['sessions']) if isinstance(task['sessions'], str) else task['sessions']
    if not sessions:
        return []
    scope = data.get('scope', {})
    ranges = scope.get('sessions', {})
    if ranges:
        sessions = [session for session in sessions if session in ranges]
    if not sessions:
        return []
    first = scope.get('first_ts', task['start_ts'])
    last = scope.get('last_ts', max((e['ts'] for e in data['evidence']), default=first))
    return [dict(row) for row in db.execute('SELECT * FROM events WHERE session IN (' +
        ','.join('?' for _ in sessions) + ') AND ts>=? AND ts<=? ORDER BY ts,id', [*sessions, first, last])
        if row['kind'] not in ('usage', 'auxiliary') and (not ranges or
            ranges[row['session']]['first_ts'] <= row['ts'] <= ranges[row['session']]['last_ts'])]


def _time_ranges(groups, rows, evidence):
    """Assign observed intervals by cited problem anchors; do not let the model invent time."""
    by_session = defaultdict(list)
    for row in rows:
        by_session[row['session']].append(row)
    full_ids = {'e_' + row['id'][:16]: row for row in rows}
    entries = []
    for group in groups:
        cites = list(dict.fromkeys(cite for item in [group['problem'], *group['actions'], *group['remaining'],
            *[value for value in (group['outcome'], group['verification']) if value]] for cite in item['evidence_ids']))
        cited = [full_ids[cite] for cite in cites if cite in full_ids]
        anchors = [cite for cite in group['problem']['evidence_ids'] if cite in full_ids]
        sessions = {row['session'] for row in cited}
        starts = {}
        for session in sessions:
            problem_rows = [full_ids[cite] for cite in anchors if full_ids[cite]['session'] == session]
            anchor = min(row['ts'] for row in problem_rows or [r for r in cited if r['session'] == session])
            if not any(row['kind'] == 'user' for row in problem_rows):
                prior_users = [row['ts'] for row in by_session[session] if row['kind'] == 'user' and row['ts'] <= anchor]
                if prior_users:
                    anchor = max(prior_users)
            starts[session] = anchor
        stamps = [row['ts'] for row in cited] or [e['ts'] for e in evidence]
        entries.append({'starts': starts, 'anchors': anchors, 'projects': sorted({r['project'] for r in cited}),
                        'first': min([*stamps, *starts.values()]), 'last': max(stamps), 'intervals': []})
    for session, events in by_session.items():
        for index, row in enumerate(events):
            applicable = [(i, entry['starts'][session]) for i, entry in enumerate(entries)
                          if session in entry['starts'] and entry['starts'][session] <= row['ts']]
            if not applicable:
                continue
            latest = max(start for _, start in applicable)
            owners = [i for i, start in applicable if start == latest]
            for owner in owners:
                entries[owner]['last'] = max(entries[owner]['last'], row['ts'])
            if not index:
                continue
            previous = events[index - 1]
            if 0 < row['ts'] - previous['ts'] <= TIMEOUT:
                applicable_before = [(i, entry['starts'][session]) for i, entry in enumerate(entries)
                                     if session in entry['starts'] and entry['starts'][session] <= previous['ts']]
                if applicable_before:
                    latest_before = max(start for _, start in applicable_before)
                    for owner, anchor in applicable_before:
                        if anchor == latest_before:
                            entries[owner]['intervals'].append([previous['ts'], row['ts']])
                            entries[owner]['last'] = max(entries[owner]['last'], row['ts'])
    return entries


def recompute_times(db, day):
    """Sweep overlapping records globally: category totals never double-count parallel work."""
    events = defaultdict(list)
    records = list(db.execute('SELECT id,intervals_json FROM work_records WHERE day=? '
                              'AND id NOT IN (SELECT record_id FROM legacy_shared_records)', (day,)))
    db.execute('UPDATE work_records SET active_seconds=0 WHERE day=?', (day,))
    for row in records:
        # Merge each record's own overlapping intervals first.
        merged = []
        for first, last in sorted(json.loads(row['intervals_json'])):
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            elif last > first:
                merged.append([first, last])
        for first, last in merged:
            events[first].append((row['id'], 1))
            events[last].append((row['id'], -1))
    active, totals, previous = Counter(), defaultdict(float), None
    for stamp in sorted(events):
        if previous is not None and active:
            share = (stamp - previous) / len(active)
            for record_id in active:
                totals[record_id] += share
        for record_id, delta in events[stamp]:
            active[record_id] += delta
            if active[record_id] <= 0:
                del active[record_id]
        previous = stamp
    for record in records:
        db.execute('UPDATE work_records SET active_seconds=? WHERE id=?', (totals[record['id']], record['id']))


def project_summary(db, task, job_id, data, result, now):
    groups = result.get('work_items', [])
    if not groups:
        return
    rows = _source_rows(db, task, data)
    timings = _time_ranges(groups, rows, data['evidence'])
    existing = [dict(row) for row in db.execute('SELECT * FROM work_records WHERE day=? '
                'AND id NOT IN (SELECT record_id FROM legacy_shared_records)', (task['day'],))]
    used, matched = set(), []
    from .conversations import group_sessions
    for group, timing in zip(groups, timings):
        sessions = set(group_sessions(group, data))
        eligible = [record for record in existing if sessions == {row[0] for row in db.execute(
            'SELECT session FROM work_record_sessions WHERE record_id=?', (record['id'],))}]
        continued = next((record for record in eligible if record['id'] not in used and group.get('continuation_id') and db.execute(
            'SELECT 1 FROM conversation_part_records WHERE part_id=? AND record_id=?',
            (group['continuation_id'], record['id'])).fetchone()), None)
        old = continued or _match(eligible, group, timing['projects'], timing['anchors'], used)
        matched.append(old)
        if old:
            used.add(old['id'])
    row_ids = {'e_' + row['id'][:16] for row in rows}
    ghosts = [json.loads(record['group_json']) for record in existing if record['id'] not in used
              and record['source_task_id'] == task['id'] and set(json.loads(record['anchors_json'])) & row_ids]
    if ghosts:
        timings = _time_ranges([*groups, *ghosts], rows, data['evidence'])[:len(groups)]
    for group, timing, old in zip(groups, timings, matched):
        record_id = old['id'] if old else uuid.uuid4().hex
        status = group['status']
        verification = group['verification']['text'] if group['verification'] else ''
        if status == 'reported_resolved' and re.search(r'仍有.{0,12}失败|依然.{0,12}失败|尚未解决', verification):
            status = 'blocked'
        stored_group = dict(group, status=status)
        if status != group['status']:
            stored_group['outcome'] = None
        historical = old is not None and timing['last'] < old['last_ts']
        if historical:
            status = old['status']
            stored_group = json.loads(old['group_json'])
        if old and (data.get('automatic_batch') is not None or historical):
            timing['intervals'] = json.loads(old['intervals_json']) + timing['intervals']
            timing['anchors'] = list(dict.fromkeys(json.loads(old['anchors_json']) + timing['anchors']))
            timing['first'] = min(old['first_ts'], timing['first'])
            timing['last'] = max(old['last_ts'], timing['last'])
        archived = status in ('reported_resolved', 'discussed')
        archived_at = (old['archived_at'] if old and old['archived_at'] else now) if archived else None
        category = old['category'] if old and (old['category_override'] or historical) else classify(group)
        if historical:
            archived_at = old['archived_at']
        # A later snapshot updates the task instead of creating another copy of the same work.
        values = (record_id, task['day'], old['source_task_id'] if historical else task['id'],
                  old['source_job_id'] if historical else job_id, old['title'] if historical else group['title'], category,
                  old['category_override'] if old else 0, status, json.dumps(stored_group, ensure_ascii=False),
                  json.dumps(timing['projects']), json.dumps(timing['anchors']), json.dumps(timing['intervals']),
                  timing['first'], timing['last'], 0, archived_at, old['created_at'] if old else now, now)
        db.execute('INSERT OR REPLACE INTO work_records VALUES (' + ','.join('?' for _ in values) + ')', values)
        if len(group_sessions(group, data)) != 1:
            db.execute('INSERT OR IGNORE INTO legacy_shared_records VALUES (?)', (record_id,))
        db.executemany('INSERT OR IGNORE INTO work_record_sessions VALUES (?,?)',
                       [(record_id, session) for session in group_sessions(group, data)])
        db.execute('INSERT OR IGNORE INTO work_record_history VALUES (?,?,?,?)',
                   (record_id, job_id, json.dumps(group, ensure_ascii=False), now))
    recompute_times(db, task['day'])
    managed = db.execute('SELECT created_automatically FROM task_automation WHERE task_id=?', (task['id'],)).fetchone()
    if managed and managed[0]:
        titles = [row[0] for row in db.execute('SELECT title FROM work_records WHERE source_task_id=? ORDER BY updated_at DESC,id', (task['id'],))]
        title = titles[0] + (f' 等{len(titles)}项工作' if len(titles) > 1 else '')
        db.execute('UPDATE tasks SET title=? WHERE id=?', (title[:150], task['id']))
    from .conversations import project
    project(db, task, job_id, data, result, now)


def work_record_data(path, day, zone, period='day', category=None, state='all'):
    from .documents import period_days
    from .store import connect
    first, last = period_days(day, period)
    if category is not None and category not in CATEGORIES:
        raise ValueError('Unknown category')
    if state not in ('all', 'active', 'archived', 'blocked'):
        raise ValueError('Unknown archive state')
    db = connect(path)
    try:
        clauses = ['day>=?', 'day<=?', 'id NOT IN (SELECT record_id FROM legacy_shared_records)']
        params = [first.isoformat(), last.isoformat()]
        if category:
            clauses.append('category=?')
            params.append(category)
        if state in ('active', 'archived'):
            clauses.append('archived_at IS ' + ('NULL' if state == 'active' else 'NOT NULL'))
        if state == 'blocked':
            clauses.append("status='blocked'")
        where = ' AND '.join(clauses)
        aggregate = db.execute('SELECT COUNT(*) AS total,COALESCE(SUM(active_seconds),0) AS active_seconds,'
            "SUM(archived_at IS NOT NULL) AS archived,SUM(status='blocked') AS blocked FROM work_records WHERE " + where, params).fetchone()
        categories = [dict(row) for row in db.execute('SELECT category,COUNT(*) AS count,SUM(active_seconds) AS active_seconds '
            'FROM work_records WHERE ' + where + ' GROUP BY category ORDER BY count DESC,category', params)]
        records = []
        for row in db.execute('SELECT * FROM work_records WHERE ' + where + ' ORDER BY day DESC,updated_at DESC,id LIMIT 500', params):
            record = dict(row)
            record['work'] = json.loads(record.pop('group_json'))
            record['projects'] = json.loads(record.pop('projects_json'))
            record.pop('intervals_json')
            record.pop('anchors_json')
            record['span_seconds'] = max(0, record['last_ts'] - record['first_ts'])
            record['category_label'] = CATEGORIES[record['category']]
            record['status_label'] = STATUS_LABELS[record['status']]
            record['archived'] = record['archived_at'] is not None
            record['updates'] = db.execute('SELECT COUNT(*) FROM work_record_history WHERE record_id=?', (record['id'],)).fetchone()[0]
            records.append(record)
        for item in categories:
            item['label'] = CATEGORIES[item['category']]
        stats = dict(aggregate)
        stats['archived'] = stats['archived'] or 0
        stats['blocked'] = stats['blocked'] or 0
        stats['active'] = stats['total'] - stats['archived']
        if category is None and state == 'all':
            previous, intervals = {}, []
            for event in db.execute("SELECT source,session,ts FROM events WHERE ts>=? AND ts<? "
                                    "AND kind NOT IN ('usage','auxiliary') ORDER BY ts,id",
                                    (bounds(first.isoformat(), zone)[0], bounds(last.isoformat(), zone)[1])):
                key = (event['source'], event['session'])
                if key in previous and 0 < event['ts'] - previous[key] <= TIMEOUT:
                    intervals.append((previous[key], event['ts']))
                previous[key] = event['ts']
            stats['observed_active_seconds'] = union_seconds(intervals)
            stats['unattributed_seconds'] = max(0, stats['observed_active_seconds'] - stats['active_seconds'])
        return {'first': first.isoformat(), 'last': last.isoformat(), 'records': records, 'stats': stats,
                'categories': categories, 'category_options': CATEGORIES, 'truncated': stats['total'] > len(records),
                'time_note': TIME_NOTE}
    finally:
        db.close()


def record_history(path, record_id):
    from .store import connect
    if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
        raise ValueError('Invalid record')
    db = connect(path)
    try:
        if not db.execute('SELECT 1 FROM work_records WHERE id=?', (record_id,)).fetchone():
            raise ValueError('Unknown record')
        return {'history': [{'job_id': row['job_id'], 'work': json.loads(row['group_json']), 'recorded_at': row['recorded_at']}
                            for row in db.execute('SELECT * FROM work_record_history WHERE record_id=? ORDER BY recorded_at DESC,rowid DESC LIMIT 50', (record_id,))]}
    finally:
        db.close()


def revise_record(db, record_id, category=None, archived=None, now=0):
    if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
        raise ValueError('Invalid record')
    if category is not None and category not in CATEGORIES:
        raise ValueError('Invalid category')
    if archived is not None and type(archived) is not bool:
        raise ValueError('Invalid archive state')
    if not db.execute('SELECT 1 FROM work_records WHERE id=?', (record_id,)).fetchone():
        raise ValueError('Unknown work record')
    with db:
        if category is not None:
            db.execute('UPDATE work_records SET category=?,category_override=1,updated_at=? WHERE id=?', (category, now, record_id))
        if archived is not None:
            db.execute('UPDATE work_records SET archived_at=?,updated_at=? WHERE id=?', (now if archived else None, now, record_id))
    return {'updated': True}
