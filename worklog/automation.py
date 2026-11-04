"""Durable, explicitly consented automatic capture and bounded summary scheduling."""
import json
import math
import threading
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .core import bounds
from .evidence import (
    EvidenceUnavailable,
    activity_digest,
    source_scope,
    summary_quality,
)
from .store import connect


def semantic_digest(rows):
    return activity_digest([row for row in rows if row['kind'] in ('user', 'assistant') and row['status'] != 'toolUse'])


def mark_covered(db, task_id, data, result=None):
    excluded = set()
    if result:
        from .conversation_upgrade import cites
        from .conversations import group_sessions
        excluded = set().union(*(cites(group) for group in result.get('work_items', []) if len(group_sessions(group, data)) != 1))
    batch = data.get('automatic_batch')
    if batch is None:
        batch = []
        for entry in data.get('evidence', []):
            if entry.get('context_only') or not entry.get('text') or entry['kind'] not in ('user', 'assistant'):
                continue
            row = db.execute('SELECT id,ts,status,kind FROM events WHERE id LIKE ? LIMIT 1', (entry['id'][2:] + '%',)).fetchone()
            if row:
                batch.append({'id': row['id'], 'digest': activity_digest([dict(row)])})
    db.executemany('INSERT OR REPLACE INTO automatic_coverage VALUES (?,?,?)',
                   [(task_id, item['id'], item['digest']) for item in batch if 'e_' + item['id'][:16] not in excluded])


def batch_material(db, task, rows):
    meaningful = [row for row in rows if row['kind'] in ('user', 'assistant') and row['status'] != 'toolUse']
    seen = {row['event_id']: row['digest'] for row in db.execute('SELECT event_id,digest FROM automatic_coverage WHERE task_id=?', (task['id'],))}
    pending = [row for row in meaningful if seen.get(row['id']) != activity_digest([row])]
    covered, accepted_sessions, blocked, users = [], set(), set(), 0
    for row in pending:
        session = row['session']
        if session in blocked or (session not in accepted_sessions and len(accepted_sessions) >= 8):
            continue
        if row['kind'] == 'user' and users >= 8:
            blocked.add(session)
            continue
        covered.append(row)
        accepted_sessions.add(session)
        users += row['kind'] == 'user'
        if len(covered) == 12:
            break
    if not covered:
        return [], []
    selected = {row['id']: row for row in covered}
    sessions = list(dict.fromkeys(row['session'] for row in covered))
    for kind in ('user', 'assistant'):
        for session in sessions:
            first = min(row['ts'] for row in covered if row['session'] == session)
            previous = next((row for row in reversed(meaningful) if row['session'] == session and row['ts'] < first and row['kind'] == kind), None)
            if previous and len(selected) < 24:
                selected[previous['id']] = previous
    for user in [row for row in covered if row['kind'] == 'user']:
        next_user = next((row['ts'] for row in meaningful if row['session'] == user['session'] and row['kind'] == 'user' and row['ts'] > user['ts']), float('inf'))
        replies = [row for row in meaningful if row['session'] == user['session'] and row['kind'] == 'assistant' and user['ts'] < row['ts'] < next_user]
        if replies and len(selected) < 24:
            selected[replies[-1]['id']] = replies[-1]
    ranges = {session: (min(row['ts'] for row in selected.values() if row['session'] == session),
                        max(row['ts'] for row in selected.values() if row['session'] == session)) for session in sessions}
    material = [row for row in rows if row['session'] in ranges and ranges[row['session']][0] <= row['ts'] <= ranges[row['session']][1]
                and (row['id'] in selected or row['kind'] not in ('user', 'assistant'))]
    return material, covered


def restore(service):
    service.automation_since = service.started
    service.automation_scope = 'now'
    service.automation_reason = ''
    service.automation_wake = threading.Event()
    service.automatic_retries = set()
    if service.auto:
        return  # Explicit CLI flags retain their exact process-scoped meaning.
    db = connect(service.path)
    try:
        row = db.execute('SELECT settings_json FROM automation_settings WHERE id=1').fetchone()
    finally:
        db.close()
    if not row:
        return
    try:
        state = json.loads(row[0])
        if not isinstance(state, dict) or type(state.get('enabled')) is not bool:
            raise ValueError('Invalid saved policy')
        if not state['enabled']:
            return
        if state['source_scope'] != source_scope(service.roots, service.key):
            service.automation_reason = '来源范围发生变化，请重新授权自动记录。'
            return
        if service.runner and (service.runner.provider, service.runner.model) != (state['provider'], state['model']):
            service.automation_reason = '模型发生变化，请重新授权自动总结。'
            return
        if service.runner is None:
            from .llm import create_runner
            try:
                service.runner = create_runner(state['provider'], state['model'])
            except ValueError:
                service.automation_reason = '模型 CLI 不可用；仍记录来源，等待恢复模型。'
        service.auto = True
        service.allow_excerpts = state['allow_excerpts'] is True
        service.automation_since = float(state['since'])
        if not math.isfinite(service.automation_since) or service.automation_since < 0:
            raise ValueError('Invalid consent boundary')
        service.automation_scope = state['scope']
        service.idle_seconds = max(60, min(3600, int(state['idle_seconds'])))
        service.daily_limit = None
        if state.get('summary_unit') != 'conversation' or state.get('daily_limit') is not None:
            db = connect(service.path)
            try:
                with db:
                    db.execute('DELETE FROM automation_scans')
                    db.execute("UPDATE task_automation SET attempt_digest=NULL,last_error=NULL WHERE last_error='daily_limit'")
            finally:
                db.close()
        persist(service)
    except (KeyError, ValueError, TypeError):
        service.auto = False
        service.allow_excerpts = False
        service.automation_reason = '自动设置无效，请在工作台重新授权。'


def persist(service):
    state = {'summary_unit': 'conversation', 'enabled': service.auto, 'allow_excerpts': service.allow_excerpts,
             'since': service.automation_since, 'scope': service.automation_scope,
             'source_scope': source_scope(service.roots, service.key),
             'provider': service.runner.provider if service.runner else 'codebuddy',
             'model': service.runner.model if service.runner else 'deepseek-v4.1-flash',
             'idle_seconds': service.idle_seconds, 'daily_limit': service.daily_limit}
    db = connect(service.path)
    try:
        with db:
            db.execute('INSERT OR REPLACE INTO automation_settings VALUES (1,?)', (json.dumps(state),))
    finally:
        db.close()


def configure(service, enabled, scope='now', consent=False, idle_minutes=3):
    if type(enabled) is not bool or scope not in ('today', 'now') or type(consent) is not bool:
        raise ValueError('Invalid automatic consent')
    if type(idle_minutes) is not int or not 1 <= idle_minutes <= 60:
        raise ValueError('Invalid idle threshold')
    with service.guard:
        if enabled:
            if not consent or not service.roots:
                raise ValueError('Automatic summaries require explicit excerpt consent and configured sources')
            if service.runner is None:
                from .llm import create_runner
                service.runner = create_runner('codebuddy')
            now = service.clock()
            today = datetime.fromtimestamp(now, ZoneInfo(service.zone)).date().isoformat()
            proposed = bounds(today, service.zone)[0] if scope == 'today' else now
            service.automation_since = min(service.automation_since, proposed) if service.auto else proposed
            service.automation_scope = scope
            service.allow_excerpts = True
            service.idle_seconds = idle_minutes * 60
            service.automation_reason = ''
        else:
            db = connect(service.path)
            try:
                with db:
                    db.execute("UPDATE summaries SET status='interrupted',error='automation_paused' WHERE status='queued' "
                               'AND task_id IN (SELECT task_id FROM task_automation)')
            finally:
                db.close()
        service.auto = enabled
        if enabled:
            db = connect(service.path)
            try:
                resumed = [row[0] for row in db.execute("SELECT DISTINCT task_id FROM summaries WHERE status='interrupted' AND error='automation_paused'")]
                with db:
                    for task_id in resumed:
                        db.execute('UPDATE task_automation SET attempt_digest=NULL WHERE task_id=?', (task_id,))
                service.automatic_retries.update(resumed)
            finally:
                db.close()
        persist(service)
        db = connect(service.path)
        try:
            with db:
                db.execute('DELETE FROM automation_scans')
        finally:
            db.close()
        service.automation_wake.set()
    return status(service)


def retry_failures(service):
    if not service.auto or not service.allow_excerpts or service.runner is None:
        raise ValueError('Enable authorized automatic summaries first')
    with service.guard:
        db = connect(service.path)
        try:
            failures = [row[0] for row in db.execute("SELECT task_id FROM task_automation WHERE last_error IS NOT NULL AND last_error!='daily_limit'")]
            with db:
                for task_id in failures:
                    db.execute('UPDATE task_automation SET attempt_digest=NULL,last_error=NULL WHERE task_id=?', (task_id,))
            service.automatic_retries.update(failures)
        finally:
            db.close()
        service.automation_wake.set()
    return status(service)


def status(service):
    db = connect(service.path)
    try:
        queued = db.execute("SELECT COUNT(*) FROM summaries WHERE status IN ('queued','running') "
                            'AND task_id IN (SELECT task_id FROM conversation_sources)').fetchone()[0]
        failures = [dict(row) for row in db.execute('SELECT last_error AS code,COUNT(*) AS count '
                    'FROM task_automation WHERE task_id IN (SELECT task_id FROM conversation_sources) AND last_error IS NOT NULL GROUP BY last_error')]
        sources = db.execute('SELECT COUNT(DISTINCT session) FROM conversation_sources').fetchone()[0]
        pending_events = 0
        for task in db.execute('SELECT t.* FROM tasks t JOIN conversation_sources a ON a.task_id=t.id'):
            sessions = json.loads(task['sessions'])
            if sessions:
                pending_events += db.execute('SELECT COUNT(*) FROM events e WHERE session IN (' + ','.join('?' for _ in sessions) + ') '
                    "AND ts>=? AND ts<? AND kind IN ('user','assistant') AND status!='toolUse' "
                    'AND NOT EXISTS (SELECT 1 FROM automatic_coverage c WHERE c.task_id=? AND c.event_id=e.id)',
                    [*sessions, task['start_ts'], min(task['end_ts'] or bounds(task['day'], service.zone)[1], service.clock()), task['id']]).fetchone()[0]
    finally:
        db.close()
    return {'enabled': service.auto, 'since': service.automation_since, 'scope': service.automation_scope,
            'allow_excerpts': service.allow_excerpts, 'model_ready': service.runner is not None,
            'provider': service.runner.provider if service.runner else None,
            'model': service.runner.model if service.runner else None,
            'idle_minutes': service.idle_seconds / 60, 'daily_limit': service.daily_limit,
            'queued': queued, 'sources': sources, 'pending_events': pending_events, 'issues': failures,
            'reason': service.automation_reason or ('正文授权已关闭，自动总结暂停。' if service.auto and not service.allow_excerpts else ''),
            'note': '自动整理与归档不等于人工验收；暂停会取消待执行自动任务，已发送的请求可能仍会完成。'}


TERMINAL_TURN = ('stop', 'length', 'error', 'aborted')
ABANDONED_SECONDS = 1800  # A turn Pi never finished still counts once the conversation goes quiet.


def session_settled(rows, now, idle_seconds):
    """Only record after the user leaves Pi: a finished turn plus quiet time.

    Pi appends events per message, so an in-flight turn keeps a conversation unsettled;
    a turn abandoned mid-answer is recorded once it has been silent long enough.
    """
    active = [row for row in rows if row['kind'] not in ('usage', 'auxiliary')]
    if not active:
        return False
    quiet = now - active[-1]['ts']
    if active[-1]['status'] in TERMINAL_TURN:
        return quiet >= idle_seconds
    return quiet >= max(idle_seconds, ABANDONED_SECONDS)


PLACEHOLDER_TITLE = '正在识别对话中的工作'


def rename_placeholder_tasks(db, zone):
    """One-off: replace old creation placeholders with the conversation's own name."""
    from .presentation import session_label
    renamed = 0
    for row in db.execute('SELECT * FROM tasks WHERE title=?', (PLACEHOLDER_TITLE,)):
        task = dict(row)
        titles = [item[0] for item in db.execute(
            'SELECT title FROM work_records WHERE source_task_id=? ORDER BY updated_at DESC,id', (task['id'],))]
        if titles:
            name = titles[0] + (f' 等{len(titles)}项工作' if len(titles) > 1 else '')
        else:
            sessions = json.loads(task['sessions'])
            events = ([dict(item) for item in db.execute('SELECT * FROM events WHERE session=? AND ts>=? ORDER BY ts,id',
                                                        (sessions[0], task['start_ts']))] if sessions else [])
            if not events:
                continue
            name, _ = session_label(db, sessions[0], [item['title'] for item in events if item['title']],
                                    events[0]['source'], task['start_ts'], zone)
        db.execute('UPDATE tasks SET title=? WHERE id=?', (name[:150], task['id']))
        renamed += 1
    return renamed


def tick(service):
    if not service.auto:
        return
    with service.guard:
        now = service.clock()
        db = connect(service.path)
        try:
            grouped = defaultdict(list)
            today = datetime.fromtimestamp(now, ZoneInfo(service.zone)).date().isoformat()
            first_day = datetime.fromtimestamp(service.automation_since, ZoneInfo(service.zone)).date().isoformat()
            markers = {}
            for imported_day in db.execute('SELECT DISTINCT day FROM import_days WHERE zone=? AND day>=? AND day<=?',
                                           (service.zone, first_day, today)):
                day = imported_day[0]
                revisions = [tuple(row) for row in db.execute('SELECT source,digest FROM activity_revisions WHERE day=? AND zone=? ORDER BY source', (day, service.zone))]
                digest = json.dumps([revisions, int(now // 60) if day == today else 0])
                previous = db.execute('SELECT digest FROM automation_scans WHERE day=? AND zone=?', (day, service.zone)).fetchone()
                if previous and previous[0] == digest:
                    continue
                markers[day] = digest
                start, end = bounds(day, service.zone)
                for row in db.execute("SELECT * FROM events WHERE ts>=? AND ts<=? AND ts<? AND source IN ('pi','codex') "
                                      "AND kind NOT IN ('usage','auxiliary') ORDER BY ts,id",
                                      (max(start, service.automation_since), now, end)):
                    item = dict(row)
                    grouped[(day, item['project'])].append(item)
            from .triage import deferred_sessions, ignored_sessions
            ignored = ignored_sessions(db)
            managed = set()
            for (day, project), rows in grouped.items():
                deferred = deferred_sessions(db, day, service.zone)
                sessions = list(dict.fromkeys(row['session'] for row in rows
                                              if row['session'] not in deferred
                                              and row['session'] not in ignored))
                existing = [dict(row) for row in db.execute('SELECT * FROM tasks WHERE day=? ORDER BY created_at', (day,))]
                for session in sessions:
                    first = min(row['ts'] for row in rows if row['session'] == session)
                    if first >= now:
                        continue
                    registered = db.execute('SELECT t.* FROM tasks t JOIN conversation_sources c ON c.task_id=t.id '
                                            'WHERE c.session=? AND c.day=?', (session, day)).fetchone()
                    if registered and (registered['start_ts'] < service.automation_since or registered['status'] != 'open'):
                        with db:
                            db.execute('DELETE FROM conversation_sources WHERE task_id=?', (registered['id'],))
                        registered = None
                    if registered:
                        task = dict(registered)
                    else:
                        task = next((item for item in existing if json.loads(item['sessions']) == [session]
                                     and item['status'] == 'open' and service.automation_since <= item['start_ts'] <= first), None)
                        created = task is None
                        if created:
                            if not session_settled([row for row in rows if row['session'] == session], now, service.idle_seconds):
                                continue  # Still working in Pi: record once the conversation settles.
                            from .presentation import session_label
                            title, _ = session_label(db, session,
                                                     [row['title'] for row in rows if row['session'] == session and row['title']],
                                                     next(row['source'] for row in rows if row['session'] == session),
                                                     first, service.zone)
                            task = service.create(title, [session], day, internal_start=first)
                        with db:
                            db.execute('INSERT OR IGNORE INTO task_automation VALUES (?,?,?,NULL,NULL,?)',
                                       (task['id'], project, int(created), now))
                            db.execute('INSERT INTO conversation_sources VALUES (?,?,?)', (task['id'], session, day))
                            # Existing approved coverage follows the conversation, not its former daily container.
                            db.execute('INSERT OR REPLACE INTO automatic_coverage SELECT ?,c.event_id,c.digest '
                                       'FROM automatic_coverage c JOIN events e ON e.id=c.event_id '
                                       'WHERE e.session=? AND e.ts>=? AND e.ts<?',
                                       (task['id'], session, task['start_ts'], bounds(day, service.zone)[1]))
                    managed.add(task['id'])
            with db:
                for day, digest in markers.items():
                    db.execute('INSERT OR REPLACE INTO automation_scans VALUES (?,?,?)', (day, service.zone, digest))
            # Old completed digests are not reread every tick; only recent or deferred sources.
            recent = datetime.fromtimestamp(now - 86400, ZoneInfo(service.zone)).date().isoformat()
            managed.update(row[0] for row in db.execute('SELECT a.task_id FROM task_automation a JOIN tasks t ON t.id=a.task_id '
                'JOIN conversation_sources c ON c.task_id=t.id WHERE t.day>=? OR a.attempt_digest IS NULL OR a.last_error IS NOT NULL', (recent,)))
            for task_id in sorted(managed):
                task = service._task(task_id)
                if task['status'] != 'open' or not service.allow_excerpts or service.runner is None:
                    continue
                rows = service._rows(task)
                if not rows:
                    continue
                if not session_settled(rows, now, service.idle_seconds):
                    continue  # Still working in Pi: wait for the conversation to settle.
                material_rows, covered_rows = batch_material(db, task, rows)
                if not covered_rows:
                    continue
                digest = semantic_digest(covered_rows)
                marker = db.execute('SELECT attempt_digest FROM task_automation WHERE task_id=?', (task_id,)).fetchone()
                if marker and marker[0] == digest and task_id not in service.automatic_retries:
                    continue
                if db.execute("SELECT 1 FROM summaries WHERE task_id=? AND status IN ('queued','running') LIMIT 1", (task_id,)).fetchone():
                    continue
                latest = db.execute("SELECT * FROM summaries WHERE task_id=? AND status='success' ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id,)).fetchone()
                # Reuse a valid unchanged saved summary locally instead of billing another call.
                if latest and latest['activity_digest'] == activity_digest(rows):
                    data, result = json.loads(latest['input_json']), json.loads(latest['output_json'])
                    if (summary_quality(data, result)['ready'] and data.get('summary_contract') == 3
                            and {row['id'] for row in covered_rows} <= {item['id'] for item in data.get('automatic_batch', [])}):
                        from .ledger import project_summary
                        with db:
                            project_summary(db, task, latest['id'], data, result, now)
                            mark_covered(db, task_id, data, result)
                            db.execute('UPDATE task_automation SET attempt_digest=NULL,last_error=NULL,updated_at=? WHERE task_id=?', (now, task_id))
                        continue
                # A settled conversation is recorded in batches; each batch is one bounded summary call.
                try:
                    requested = service.request(task_id, 'stage', retry=task_id in service.automatic_retries, automatic_rows=material_rows, covered_rows=covered_rows)
                    service.automatic_retries.discard(task_id)
                    error = 'summary_failed' if requested['status'] in ('error', 'interrupted') else None
                except EvidenceUnavailable as failure:
                    error = failure.code
                except ValueError:
                    error = 'request_rejected'
                with db:
                    db.execute('UPDATE task_automation SET attempt_digest=?,last_error=?,updated_at=? WHERE task_id=?',
                               (None if error == 'daily_limit' else digest, error, now, task_id))
        finally:
            db.close()
