"""Seven local calendar days of work records, not inferred working hours."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .adapters import GAP_KEYS
from .core import bounds
from .store import connect


def week_data(path, day, zone):
    selected = date.fromisoformat(day)
    first = selected - timedelta(days=selected.weekday())
    last = first + timedelta(days=6)
    start = bounds(first.isoformat(), zone)[0]
    end = bounds(last.isoformat(), zone)[1]
    days = {}
    for index in range(7):
        current = (first + timedelta(days=index)).isoformat()
        days[current] = {'date': current, 'events': 0, 'requests': 0, 'tokens': 0,
                         'sessions': set(), 'tasks': [], 'coverage': [],
                         'states': {'confirmed': 0, 'in_progress': 0, 'blocked': 0, 'unreviewed': 0}}
    db = connect(path)
    try:
        for row in db.execute('SELECT ts,session,kind,tokens FROM events WHERE ts>=? AND ts<?', (start, end)):
            key = datetime.fromtimestamp(row['ts'], ZoneInfo(zone)).date().isoformat()
            item = days[key]
            item['events'] += 1
            item['requests'] += row['kind'] == 'assistant'
            item['tokens'] += row['tokens']
            item['sessions'].add(row['session'])
        for row in db.execute('''SELECT t.id,t.title,t.day,r.status,r.outcome,r.next_steps,r.verification
                                 FROM tasks t LEFT JOIN task_reviews r ON r.task_id=t.id
                                 WHERE t.day>=? AND t.day<=? ORDER BY t.day,t.start_ts,t.id''',
                              (first.isoformat(), last.isoformat())):
            item = dict(row)
            item['status'] = item['status'] or 'unreviewed'
            days[item.pop('day')]['tasks'].append(item)
        # Coverage is keyed by local day AND timezone. Never borrow another day's scan.
        history = list(db.execute('SELECT day,zone,source,coverage FROM import_days WHERE day>=? AND day<=?',
                                  (first.isoformat(), last.isoformat())))
        for row in history:
            if row['zone'] == zone:
                days[row['day']]['coverage'].append(json.loads(row['coverage']))
        # Older databases only retain the latest scan. Recover exactly its stated day, not history.
        for row in db.execute('SELECT coverage FROM imports'):
            coverage = json.loads(row['coverage'])
            current = coverage.get('date')
            if (current in days and coverage.get('timezone', zone) == zone
                    and not any(c['source'] == coverage['source'] for c in days[current]['coverage'])):
                days[current]['coverage'].append(coverage)
    finally:
        db.close()
    from .ledger import work_record_data
    automatic = work_record_data(path, day, zone, 'week')
    for item in days.values():
        auto_records = [record for record in automatic['records'] if record['day'] == item['date']]
        item['automatic_records'] = auto_records
        if auto_records:
            sources = {record['source_task_id'] for record in auto_records}
            item['manual_tasks'] = item['tasks']
            item['tasks'] = [task for task in item['tasks'] if task['id'] not in sources]
            item['tasks'].extend({'id': record['id'], 'title': record['title'], 'status': record['status'],
                'outcome': record['work']['outcome']['text'] if record['work']['outcome'] else record['work']['problem']['text'],
                'next_steps': '；'.join(entry['text'] for entry in record['work']['remaining']),
                'verification': record['work']['verification']['text'] if record['work']['verification'] else '',
                'active_seconds': record['active_seconds'], 'category': record['category_label'], 'automatic': True}
                for record in auto_records)
        item['states'].update(reported_resolved=0, discussed=0)
    all_sessions = set()
    totals = {'events': 0, 'requests': 0, 'tokens': 0, 'tasks': 0, 'confirmed': 0,
              'blocked': 0, 'in_progress': 0, 'unreviewed': 0, 'active_days': 0, 'recorded_days': 0,
              'reported_resolved': 0, 'discussed': 0}
    for item in days.values():
        all_sessions.update(item['sessions'])
        item['sessions'] = len(item['sessions'])
        for task in item['tasks']:
            item['states'][task['status']] += 1
        item['recorded'] = bool(item['events'] or item['tasks'] or item['coverage'])
        item['active'] = bool(item['events'] or item['tasks'])
        item['coverage_gaps'] = any(any(c.get(k, 0) for k in GAP_KEYS) for c in item['coverage'])
        for field in ('events', 'requests', 'tokens'):
            totals[field] += item[field]
        for state, count in item['states'].items():
            totals[state] += count
        totals['tasks'] += len(item['tasks'])
        totals['active_days'] += item['active']
        totals['recorded_days'] += item['recorded']
    totals['sessions'] = len(all_sessions)
    return {'first': first.isoformat(), 'last': last.isoformat(), 'timezone': zone,
            'days': list(days.values()), 'totals': totals, 'automatic_work': automatic,
            'limitations': '按任务所属日期展示当前审阅状态，并非历史完成时间或真实工时。未记录不等于没有工作；跨日同名任务不会自动合并。'}
