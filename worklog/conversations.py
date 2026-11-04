"""Cumulative project/conversation tree; daily ledger remains a view of daily changes."""
import hashlib
import json
from collections import defaultdict
from difflib import SequenceMatcher

from .core import bounds
from .ledger import CATEGORIES, STATUS_LABELS, _title_key
from .store import connect


def group_sessions(group, data):
    entries = [group['problem'], *group['actions'], *group['remaining'],
               *[item for item in (group['outcome'], group['verification']) if item]]
    ids = {cite for entry in entries for cite in entry['evidence_ids']}
    return sorted({entry['session'] for entry in data['evidence'] if entry['id'] in ids})


def context(db, session, since=0):
    result = []
    for row in db.execute('SELECT * FROM conversation_parts WHERE session=? AND first_ts>=? ORDER BY last_ts DESC,id LIMIT 24', (session, since)):
        group = json.loads(row['group_json'])
        item = {'id': row['id'], 'title': row['title'], 'status': row['status'], 'as_of_ts': row['last_ts'], 'problem': group['problem']['text'][:200],
                'outcome': group['outcome']['text'][:200] if group['outcome'] else None}
        if len(json.dumps([*result, item], ensure_ascii=False).encode()) > 4096:
            break
        result.append(item)
    return result


def project(db, task, job_id, data, result, now):
    if db.execute('SELECT 1 FROM conversation_projection_jobs WHERE job_id=?', (job_id,)).fetchone():
        return
    used = set()
    for index, group in enumerate(result.get('work_items', [])):
        sessions = group_sessions(group, data)
        if len(sessions) != 1:
            continue  # Keep mixed legacy prose in its original audit record, not in a conversation's memory.
        records = [row for row in db.execute('SELECT w.*,h.group_json AS historical_group FROM work_records w '
                                            'JOIN work_record_history h ON h.record_id=w.id WHERE h.job_id=?', (job_id,))
                   if json.loads(row['historical_group'])['title'] == group['title']]
        items = [group['problem'], *group['actions'], *group['remaining'],
                 *[item for item in (group['outcome'], group['verification']) if item]]
        cited = {cite for item in items for cite in item['evidence_ids']}
        for session in sessions:
            evidence = [entry for entry in data['evidence'] if entry['session'] == session and entry['id'] in cited]
            if not evidence:
                continue
            stamps = [entry['ts'] for entry in evidence]
            metadata = db.execute('SELECT project,MIN(ts) AS first,MAX(ts) AS last FROM events WHERE session=?', (session,)).fetchone()
            if not metadata or metadata['project'] is None:
                continue
            canonical = db.execute('SELECT project FROM project_aliases WHERE alias=?', (metadata['project'],)).fetchone()
            project_id = canonical[0] if canonical else metadata['project']
            db.execute('INSERT INTO conversations VALUES (?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET '
                       'project=excluded.project,first_ts=MIN(first_ts,excluded.first_ts),last_ts=MAX(last_ts,excluded.last_ts)',
                       (session, project_id, group['title'], metadata['first'], metadata['last']))
            old = None
            candidates = []
            for part in db.execute('SELECT * FROM conversation_parts WHERE session=?', (session,)):
                if part['id'] in used:
                    continue
                linked = any(db.execute('SELECT 1 FROM conversation_part_records WHERE part_id=? AND record_id=?',
                                        (part['id'], record['id'])).fetchone() for record in records)
                score = SequenceMatcher(None, _title_key(part['title']), _title_key(group['title'])).ratio()
                continued = group.get('continuation_id') == part['id']
                if continued or linked or score >= .78:
                    candidates.append((2 * int(continued) + int(linked) + score, part))
            if candidates:
                old = max(candidates, key=lambda item: item[0])[1]
            part_id = old['id'] if old else hashlib.sha256(f'{session}:{job_id}:{index}'.encode()).hexdigest()[:32]
            used.add(part_id)
            historical = old is not None and max(stamps) < old['last_ts']
            current = json.loads(old['group_json']) if historical else group
            category = old['category'] if historical else records[0]['category'] if records else group.get('category', 'other')
            db.execute('INSERT OR REPLACE INTO conversation_parts VALUES (?,?,?,?,?,?,?,?)',
                       (part_id, session, current['title'], category, current['status'], json.dumps(current, ensure_ascii=False),
                        min(old['first_ts'], min(stamps)) if old else min(stamps), max(old['last_ts'], max(stamps)) if old else max(stamps)))
            db.execute('INSERT OR IGNORE INTO conversation_progress VALUES (?,?,?,?,?)',
                       (part_id, job_id, task['day'], json.dumps(group, ensure_ascii=False), now))
            for record in records:
                db.execute('INSERT OR IGNORE INTO work_record_sessions VALUES (?,?)', (record['id'], session))
                db.execute('INSERT OR IGNORE INTO conversation_part_records VALUES (?,?)', (part_id, record['id']))
    db.execute('INSERT OR IGNORE INTO conversation_projection_jobs VALUES (?)', (job_id,))


def backfill(db):
    """Replay previously approved successful snapshots locally; never invoke a model."""
    from .evidence import summary_quality
    for job in list(db.execute("SELECT s.*,t.day,t.sessions,t.start_ts FROM summaries s JOIN tasks t ON t.id=s.task_id "
                              "WHERE s.status='success' AND s.id NOT IN (SELECT job_id FROM legacy_context_jobs) "
                              'AND NOT EXISTS (SELECT 1 FROM conversation_projection_jobs p WHERE p.job_id=s.id) '
                              'ORDER BY s.created_at,s.rowid')):
        data, result = json.loads(job['input_json']), json.loads(job['output_json'])
        if summary_quality(data, result)['ready']:
            project(db, dict(job), job['id'], data, result, job['finished_at'] or job['created_at'])
            from .automation import mark_covered
            mark_covered(db, job['task_id'], data, result)
        else:
            db.execute('INSERT OR IGNORE INTO conversation_projection_jobs VALUES (?)', (job['id'],))


def tree_data(path, day, zone):
    bounds(day, zone)
    db = connect(path)
    try:
        projects = defaultdict(list)
        parts_count = 0
        known = {row['session']: dict(row) for row in db.execute('SELECT * FROM conversations')}
        for row in db.execute("SELECT session,project,MIN(ts) AS first_ts,MAX(ts) AS last_ts FROM events WHERE source IN ('pi','codex') GROUP BY session"):
            if row['session'] not in known:
                known[row['session']] = dict(row, title='等待自动总结的对话')
            else:
                known[row['session']]['project'] = row['project']
                known[row['session']]['last_ts'] = max(known[row['session']]['last_ts'], row['last_ts'])
        for conversation in sorted(known.values(), key=lambda item: (-item['last_ts'], item['session'])):
            value = dict(conversation)
            name = db.execute("SELECT name FROM session_names WHERE session=? AND origin='manual'", (value['session'],)).fetchone()
            value['title'] = name[0] if name else value['title']
            value['processing'] = bool(db.execute("SELECT 1 FROM summaries s JOIN conversation_sources c ON c.task_id=s.task_id "
                                                 "WHERE c.session=? AND s.status IN ('queued','running') LIMIT 1", (value['session'],)).fetchone())
            parts = []
            for part in db.execute('SELECT * FROM conversation_parts WHERE session=? ORDER BY first_ts,id', (value['session'],)):
                item = dict(part)
                item['work'] = json.loads(item.pop('group_json'))
                item['category_label'] = CATEGORIES.get(item['category'], CATEGORIES['other'])
                item['status_label'] = STATUS_LABELS[item['status']]
                item['updates'] = db.execute('SELECT COUNT(*) FROM conversation_progress WHERE part_id=?', (item['id'],)).fetchone()[0]
                item['changed_today'] = bool(db.execute('SELECT 1 FROM conversation_progress WHERE part_id=? AND day=?', (item['id'], day)).fetchone())
                item['active_seconds'] = db.execute('SELECT COALESCE(SUM(w.active_seconds / MAX(1,(SELECT COUNT(*) FROM work_record_sessions s WHERE s.record_id=w.id))),0) FROM work_records w '
                    'WHERE w.id IN (SELECT record_id FROM conversation_part_records WHERE part_id=?) '
                    'AND w.id NOT IN (SELECT record_id FROM legacy_shared_records)', (item['id'],)).fetchone()[0]
                parts.append(item)
            value['parts'] = parts
            value['changed_today'] = any(part['changed_today'] for part in parts)
            value['active_seconds'] = sum(part['active_seconds'] for part in parts)
            parts_count += len(parts)
            project_id = value.pop('project')
            canonical = db.execute('SELECT project FROM project_aliases WHERE alias=?', (project_id,)).fetchone()
            projects[canonical[0] if canonical else project_id].append(value)
        result = []
        for project_id, conversations in projects.items():
            name = db.execute('SELECT name FROM projects WHERE id=?', (project_id,)).fetchone()
            result.append({'id': project_id, 'name': name[0] if name else '未识别名称的项目',
                           'conversations': conversations, 'changed_today': any(item['changed_today'] for item in conversations)})
        return {'projects': result, 'project_count': len(result), 'conversation_count': sum(map(len, projects.values())),
                'part_count': parts_count, 'day': day,
                'note': '按项目归类，每条对话跨日累计；今日标记只代表当天的新进展，不重复计为新任务。'}
    finally:
        db.close()


def progress_data(path, session):
    import re
    if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{16}', session):
        raise ValueError('Invalid conversation')
    db = connect(path)
    try:
        return {'progress': [{'day': row['day'], 'recorded_at': row['recorded_at'], 'work': json.loads(row['work_json'])}
                             for row in db.execute('SELECT p.* FROM conversation_progress p JOIN conversation_parts c ON c.id=p.part_id '
                                                   'WHERE c.session=? ORDER BY recorded_at DESC,p.rowid DESC LIMIT 100', (session,))]}
    finally:
        db.close()
