"""Private local metadata storage. Source sessions are never modified."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .core import TIMEOUT, bounds, summarize

FIELDS = ['id', 'source', 'session', 'project', 'ts', 'kind', 'provider', 'model', 'status',
          'title', 'file_id', 'error_code', 'input', 'output', 'cache_read', 'cache_write', 'tokens', 'cost']


def connect(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError('Database symlinks are not supported')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(path), timeout=10)
    existing = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    application_id = db.execute('PRAGMA application_id').fetchone()[0]
    if existing and application_id != 0x41574C47:
        db.close()
        raise ValueError('Refusing to modify a database not owned by AI Worklog')
    # Avoid a header write/lock on every read-only dashboard connection.
    if application_id != 0x41574C47:
        db.execute('PRAGMA application_id=1096240199')
    db.execute('PRAGMA secure_delete=ON')
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, source TEXT, session TEXT, project TEXT, ts REAL,
        kind TEXT, provider TEXT, model TEXT, status TEXT, title TEXT, file_id TEXT,
        error_code TEXT, input INTEGER, output INTEGER, cache_read INTEGER,
        cache_write INTEGER, tokens INTEGER, cost REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS events_time ON events(ts)')
    db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY,name TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS project_aliases (alias TEXT PRIMARY KEY,project TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_sources (
        task_id TEXT PRIMARY KEY,session TEXT NOT NULL,day TEXT NOT NULL,UNIQUE(session,day))''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversations (
        session TEXT PRIMARY KEY,project TEXT NOT NULL,title TEXT NOT NULL,
        first_ts REAL NOT NULL,last_ts REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_parts (
        id TEXT PRIMARY KEY,session TEXT NOT NULL,title TEXT NOT NULL,category TEXT NOT NULL,
        status TEXT NOT NULL,group_json TEXT NOT NULL,first_ts REAL NOT NULL,last_ts REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_part_records (
        part_id TEXT NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(part_id,record_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_progress (
        part_id TEXT NOT NULL,job_id TEXT NOT NULL,day TEXT NOT NULL,work_json TEXT NOT NULL,
        recorded_at REAL NOT NULL,PRIMARY KEY(part_id,job_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS conversation_progress_day ON conversation_progress(day)')
    db.execute('CREATE TABLE IF NOT EXISTS conversation_projection_jobs (job_id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS local_migrations (name TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS legacy_shared_records (record_id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS legacy_context_jobs (job_id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS work_record_sessions (record_id TEXT,session TEXT,PRIMARY KEY(record_id,session))')
    db.execute('CREATE TABLE IF NOT EXISTS imports (source TEXT, imported_at TEXT, coverage TEXT)')
    db.execute('''CREATE TABLE IF NOT EXISTS import_days (
        source TEXT, day TEXT, zone TEXT, coverage TEXT,
        PRIMARY KEY(source,day,zone))''')
    db.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, title TEXT, day TEXT, sessions TEXT, start_ts REAL,
        end_ts REAL, status TEXT, created_at REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS summaries (
        id TEXT PRIMARY KEY, task_id TEXT, mode TEXT, fingerprint TEXT,
        activity_digest TEXT, status TEXT, input_json TEXT, output_json TEXT,
        error TEXT, provider TEXT, model TEXT, created_at REAL, finished_at REAL,
        usage_json TEXT, started_at REAL, UNIQUE(task_id, mode, fingerprint))''')
    if 'started_at' not in {row[1] for row in db.execute('PRAGMA table_info(summaries)')}:
        db.execute('ALTER TABLE summaries ADD COLUMN started_at REAL')
    db.execute('CREATE INDEX IF NOT EXISTS summaries_task ON summaries(task_id, created_at)')
    db.execute('''CREATE TABLE IF NOT EXISTS task_reviews (
        task_id TEXT PRIMARY KEY, status TEXT NOT NULL, outcome TEXT NOT NULL,
        learnings TEXT NOT NULL, next_steps TEXT NOT NULL, verification TEXT NOT NULL,
        updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS git_commits (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, ts REAL NOT NULL,
        subject TEXT NOT NULL, files_changed INTEGER NOT NULL,
        additions INTEGER NOT NULL, deletions INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS git_commits_time ON git_commits(ts)')
    db.execute('''CREATE TABLE IF NOT EXISTS task_git (
        task_id TEXT NOT NULL, commit_id TEXT NOT NULL,
        PRIMARY KEY(task_id, commit_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_state (
        scope TEXT PRIMARY KEY, start_ts REAL, cursor_ts REAL, excerpts INTEGER)''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_seen (
        scope TEXT, event_id TEXT, digest TEXT, review_id TEXT,
        PRIMARY KEY(scope, event_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_cache (
        fingerprint TEXT PRIMARY KEY, output_json TEXT, usage_json TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, scope TEXT, start_ts REAL, end_ts REAL,
        created_at REAL, output TEXT, coverage_json TEXT, provider TEXT, model TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS session_names (
        session TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS session_triage (
        session TEXT, day TEXT, zone TEXT, digest TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(session,day,zone))''')
    db.execute('''CREATE TABLE IF NOT EXISTS ignored_sessions (
        session TEXT PRIMARY KEY, ignored_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS activity_revisions (
        source TEXT, day TEXT, zone TEXT, digest TEXT NOT NULL, PRIMARY KEY(source,day,zone))''')
    db.execute('''CREATE TABLE IF NOT EXISTS automation_scans (
        day TEXT, zone TEXT, digest TEXT NOT NULL, PRIMARY KEY(day,zone))''')
    db.execute('''CREATE TABLE IF NOT EXISTS automation_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), settings_json TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS automatic_coverage (
        task_id TEXT, event_id TEXT, digest TEXT NOT NULL, PRIMARY KEY(task_id,event_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS task_automation (
        task_id TEXT PRIMARY KEY, project TEXT NOT NULL, created_automatically INTEGER NOT NULL,
        attempt_digest TEXT, last_error TEXT, updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS work_records (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, source_task_id TEXT NOT NULL, source_job_id TEXT NOT NULL,
        title TEXT NOT NULL, category TEXT NOT NULL, category_override INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, group_json TEXT NOT NULL, projects_json TEXT NOT NULL,
        anchors_json TEXT NOT NULL, intervals_json TEXT NOT NULL,
        first_ts REAL NOT NULL, last_ts REAL NOT NULL, active_seconds REAL NOT NULL,
        archived_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS work_records_day ON work_records(day,category,status)')
    db.execute('''CREATE TABLE IF NOT EXISTS work_record_history (
        record_id TEXT, job_id TEXT, group_json TEXT NOT NULL, recorded_at REAL NOT NULL,
        PRIMARY KEY(record_id,job_id))''')
    return db


def salt(path):
    key = Path(path).expanduser().parent / 'identity.key'
    if key.is_symlink():
        raise ValueError('Identity key symlinks are not supported')
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not key.exists() and Path(path).expanduser().exists():
        db = connect(path)
        try:
            if (db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM git_commits').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM review_state').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]):
                raise ValueError('Identity key missing for existing events; restore it instead of re-keying')
        finally:
            db.close()
    try:
        with key.open('xb') as stream:
            os.chmod(key, 0o600)
            stream.write(os.urandom(32))
    except FileExistsError:
        pass
    raw = key.read_bytes()
    if len(raw) != 32:
        raise ValueError('Invalid local identity key; do not silently change event identities')
    os.chmod(key, 0o600)
    return raw


def save(path, events, coverage, day, zone):
    start, end = bounds(day, zone)
    selected = [e for e in events if start - TIMEOUT <= e['ts'] < end + TIMEOUT]
    placeholders = ','.join('?' for _ in FIELDS)
    updates = ','.join(f'{k}=excluded.{k}' for k in FIELDS if k != 'id')
    db = connect(path)
    try:
        before = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with db:
            from .evidence import redact
            project_metadata = {(item['project'], item['project_alias']): item for item in events if item.get('project_name')}
            for item in project_metadata.values():
                db.execute('INSERT OR REPLACE INTO projects VALUES (?,?)', (item['project'], redact(item['project_name'], 80)))
                db.execute('INSERT OR REPLACE INTO project_aliases VALUES (?,?)', (item['project_alias'], item['project']))
            db.executemany(f'INSERT INTO events ({",".join(FIELDS)}) VALUES ({placeholders}) '
                           f'ON CONFLICT(id) DO UPDATE SET {updates}', [[e[k] for k in FIELDS] for e in selected])
            from .projects import migrate
            migrate(db)
            # Keep only latest coverage per source; no source paths or raw errors.
            coverage = dict(coverage, date=day, timezone=zone, selected_events=len(selected))
            db.execute('DELETE FROM imports WHERE source=?', (coverage['source'],))
            db.execute("INSERT INTO imports VALUES (?,datetime('now'),?)", (coverage['source'], json.dumps(coverage)))
            db.execute('INSERT OR REPLACE INTO import_days VALUES (?,?,?,?)',
                       (coverage['source'], day, zone, json.dumps(coverage)))
            meaningful = sorted((e['id'], e['status']) for e in selected if start <= e['ts'] < end
                                and e['kind'] in ('user', 'assistant') and e['status'] != 'toolUse')
            revision = hashlib.sha256(json.dumps(meaningful).encode()).hexdigest()
            db.execute('INSERT OR REPLACE INTO activity_revisions VALUES (?,?,?,?)',
                       (coverage['source'], day, zone, revision))
        after = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        return {'selected_events': len(selected), 'new_events': after - before, 'coverage': coverage}
    finally:
        db.close()


def report_data(path, day, zone):
    start, end = bounds(day, zone)
    db = connect(path)
    try:
        events = [dict(r) for r in db.execute('SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts',
                                            (start - TIMEOUT, end + TIMEOUT))]
        coverage = {r['source']: json.loads(r['coverage']) for r in db.execute(
            'SELECT source,coverage FROM import_days WHERE day=? AND zone=?', (day, zone))}
        for row in db.execute('SELECT * FROM imports'):
            item = json.loads(row['coverage'])
            if item.get('date') == day and item.get('timezone', zone) == zone:
                coverage.setdefault(row['source'], dict(item, imported_at=row['imported_at']))
        from .presentation import decorate_sessions
        data = decorate_sessions(db, summarize(events, day, zone, coverage))
        data['incremental_reviews'] = [dict(row) for row in db.execute(
            'SELECT output,created_at,provider,model FROM reviews WHERE created_at>=? AND created_at<? '
            'ORDER BY created_at DESC LIMIT 10', (start, end))]
        drafts = []
        records = []
        for task in db.execute('SELECT * FROM tasks WHERE day=? ORDER BY created_at', (day,)):
            records.append({'id': task['id'], 'title': task['title'], 'day': task['day']})
            job = db.execute("SELECT * FROM summaries WHERE task_id=? AND status='success' ORDER BY created_at DESC,rowid DESC LIMIT 1", (task['id'],)).fetchone()
            if job:
                captured = json.loads(job['input_json'])
                drafts.append({'task_id': task['id'], 'title': task['title'], 'mode': job['mode'],
                               'status': task['status'], 'provider': job['provider'], 'model': job['model'],
                               'coverage': captured['coverage'], 'result': json.loads(job['output_json']),
                               'draft_at': job['finished_at'] or job['created_at'],
                               'evidence': captured.get('evidence', []), 'job_id': job['id']})
                from .evidence import summary_quality
                drafts[-1]['quality'] = summary_quality(captured, drafts[-1]['result'])
        data['task_summaries'] = drafts
        data['task_records'] = records
        reviews = []
        for row in db.execute('''SELECT t.id, t.title, r.status, r.outcome, r.learnings,
                                      r.next_steps, r.verification, r.updated_at
                               FROM tasks t JOIN task_reviews r ON r.task_id=t.id
                               WHERE t.day=? ORDER BY t.start_ts, t.id''', (day,)):
            item = dict(row)
            item['commits'] = [dict(commit) for commit in db.execute(
                '''SELECT c.id, c.subject, c.files_changed, c.additions, c.deletions
                   FROM task_git l JOIN git_commits c ON c.id=l.commit_id
                   WHERE l.task_id=? ORDER BY c.ts, c.id''', (row['id'],))]
            reviews.append(item)
        data['task_reviews'] = reviews
        if db.execute('SELECT 1 FROM work_records WHERE day=? LIMIT 1', (day,)).fetchone():
            from .ledger import work_record_data
            data['automatic_work'] = work_record_data(path, day, zone)
        if drafts:
            data['limitations'][-1] = 'LLM总结为证据范围内的待审草稿，引用有效不等于成果已验证。'
        return data
    finally:
        db.close()


def markdown(data, detail='brief'):
    from .documents import build_document, document_markdown
    if detail not in ('brief', 'full'):
        raise ValueError('Invalid report detail')
    automatic = data.get('automatic_work')
    if automatic:
        represented = {record['source_task_id'] for record in automatic['records']}
        manual = {review['id'] for review in data.get('task_reviews', [])}
        data = dict(data,
            task_records=[task for task in data.get('task_records', []) if task['id'] not in represented or task['id'] in manual],
            task_summaries=[draft for draft in data.get('task_summaries', []) if draft['task_id'] not in represented or draft['task_id'] in manual])
    document = build_document([data], data['date'], data['date'], data['timezone'], 'day')
    if automatic:
        document['automatic_work'] = automatic
        stats = automatic['stats']
        document['overview'] = (f'自动整理 {stats["total"]} 项任务记录，归档 {stats["archived"]} 项，受阻 {stats["blocked"]} 项；'
                                f'活跃时长估计 {stats["active_seconds"] / 60:.1f} 分钟。自动记录无需人工重填，归档不等于人工验收。')
    document['detail'] = detail
    return document_markdown(document)


def period_markdown(path, day, zone, period, detail='brief'):
    from .documents import report_document
    if period not in ('week', 'month'):
        raise ValueError('Invalid report period')
    return report_document(path, day, zone, period, detail)['markdown']
