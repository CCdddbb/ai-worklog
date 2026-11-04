"""Persistent tasks, immutable evidence snapshots, and one bounded summary worker."""
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import bounds
from .evidence import (
    EvidenceUnavailable,
    activity_digest,
    redact,
    require_evidence,
    snapshot,
    source_scope,
    summary_quality,
)
from .llm import SummaryError, validate_business_result, validate_result
from .store import connect, salt

LOGGER = logging.getLogger(__name__)


class TaskService:
    def __init__(self, db_path, runner, roots=None, allow_excerpts=False, auto=False,
                 idle_seconds=600, daily_limit=10, zone='Asia/Shanghai', clock=time.time):
        self.path = Path(db_path)
        self.runner = runner
        self.roots = roots or {}
        self.allow_excerpts = allow_excerpts
        self.auto = auto
        self.idle_seconds = idle_seconds
        self.daily_limit = None  # Retained as a compatibility field; summaries have no daily cap.
        self.zone = zone
        self.clock = clock
        self.started = clock()
        self.stop = threading.Event()
        self.guard = threading.RLock()
        if allow_excerpts and not self.roots:
            raise ValueError('Excerpts require explicitly configured source directories')
        if any(not Path(root).expanduser().is_dir() for root in self.roots.values()):
            raise ValueError('Source directories must exist')
        if idle_seconds < 60:
            raise ValueError('Invalid idle threshold')
        connect(self.path).close()
        self.key = salt(self.path)
        from .automation import restore
        restore(self)
        lock_path = str(self.path) + '.summary.lock'
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise ValueError('Another summary worker owns this database') from None
        db = connect(self.path)
        try:
            with db:
                db.execute("UPDATE summaries SET status='interrupted',error='process_interrupted' WHERE status='running'")
                from .conversation_upgrade import upgrade
                self.automatic_retries.update(upgrade(db))
                from .conversations import backfill
                backfill(db)
                for row in db.execute('SELECT t.id,t.sessions FROM tasks t JOIN task_automation a ON a.task_id=t.id'):
                    if len(json.loads(row['sessions'])) != 1:
                        db.execute("UPDATE summaries SET status='interrupted',error='conversation_unit_migrated' WHERE task_id=? AND status='queued'", (row['id'],))
                if self.auto:
                    resumed = {row[0] for row in db.execute("SELECT s.task_id FROM summaries s WHERE s.status IN ('error','interrupted') "
                        "AND s.error IN ('cancelled','process_interrupted') AND s.task_id IN (SELECT task_id FROM conversation_sources) "
                        'AND NOT EXISTS (SELECT 1 FROM summaries newer WHERE newer.task_id=s.task_id AND newer.rowid>s.rowid)')}
                    self.automatic_retries.update(resumed)
                    for task_id in resumed:
                        db.execute('UPDATE task_automation SET attempt_digest=NULL,last_error=NULL WHERE task_id=?', (task_id,))
        finally:
            db.close()
        self.thread = threading.Thread(target=self._loop, daemon=True, name='worklog-summary')
        self.thread.start()

    def close(self):
        if self.lock_fd is None:
            return
        self.stop.set()
        self.thread.join(timeout=8)
        # Don't release ownership while an uncancelled model call is still running.
        if not self.thread.is_alive():
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    def _task(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('Invalid task ID')
        db = connect(self.path)
        try:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise ValueError('Task not found')
            return dict(row)
        finally:
            db.close()

    def _rows(self, task, at=None):
        _, day_end = bounds(task['day'], self.zone)
        end = min(at if at is not None else self.clock(), day_end)
        if task['end_ts'] is not None:
            end = min(end, task['end_ts'])
        sessions = json.loads(task['sessions'])
        if not sessions:
            return []
        marks = ','.join('?' for _ in sessions)
        db = connect(self.path)
        try:
            return [dict(r) for r in db.execute(
                f'SELECT * FROM events WHERE session IN ({marks}) AND ts>=? AND ts<? ORDER BY ts,id',
                sessions + [task['start_ts'], end])]
        finally:
            db.close()

    def create(self, title, sessions, day, start_time='00:00', internal_start=None):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 150:
            raise ValueError('Task title required')
        if not isinstance(sessions, list) or not 0 <= len(sessions) <= 12 or any(
                not isinstance(s, str) or not re.fullmatch(r'[a-f0-9]{16}', s) for s in sessions):
            raise ValueError('Select up to 12 known sessions')
        start, end = bounds(day, self.zone)
        if not isinstance(start_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', start_time):
            raise ValueError('Invalid task start time')
        local = datetime.fromisoformat(day + 'T' + start_time).replace(tzinfo=ZoneInfo(self.zone))
        start = internal_start if internal_start is not None else local.timestamp()
        if start >= min(end, self.clock()):
            raise ValueError('Task scope cannot start in the future')
        db = connect(self.path)
        try:
            known = {r[0] for r in db.execute('SELECT DISTINCT session FROM events WHERE ts>=? AND ts<?', bounds(day, self.zone))}
            if not set(sessions) <= known:
                raise ValueError('Session outside selected day')
            task_id = uuid.uuid4().hex
            with db:
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                           (task_id, redact(title, 150), day, json.dumps(list(dict.fromkeys(sessions))),
                            start, None, 'open', self.clock()))
            return self._task(task_id)
        finally:
            db.close()

    def preview(self, task_id, job_id=None):
        with self.guard:
            task = self._task(task_id)
            if job_id is not None:
                db = connect(self.path)
                try:
                    row = db.execute('SELECT input_json FROM summaries WHERE task_id=? AND id=?', (task_id, job_id)).fetchone()
                    if not row:
                        raise ValueError('Summary snapshot not found')
                    return json.loads(row['input_json'])
                finally:
                    db.close()
            return snapshot(task, self._rows(task), self.roots, self.key, self.allow_excerpts)

    def request(self, task_id, mode='stage', retry=False, automatic_rows=None, covered_rows=None):
        if self.runner is None:
            raise ValueError('Summary model is not enabled')
        if not json.loads(self._task(task_id)['sessions']):
            raise ValueError('Manual task has no AI evidence to summarize')
        if mode not in ['stage', 'final']:
            raise ValueError('Invalid summary mode')
        with self.guard:
            task = self._task(task_id)
            if mode == 'stage' and task['status'] != 'open':
                raise ValueError('Task already ended; retry its final summary instead')
            now = self.clock()
            if mode == 'final' and task['end_ts'] is None:
                task['end_ts'] = min(now, bounds(task['day'], self.zone)[1])
            data = snapshot(task, self._rows(task) if automatic_rows is None else automatic_rows, self.roots, self.key, self.allow_excerpts)
            if covered_rows is not None:
                data['summary_contract'] = 3
                data['automatic_batch'] = [{'id': row['id'], 'digest': activity_digest([row])} for row in covered_rows]
                fresh = {'e_' + row['id'][:16] for row in covered_rows}
                for entry in data['evidence']:
                    entry['context_only'] = entry['id'] not in fresh
                from .evidence import readable
                if not any(not entry['context_only'] and readable(entry['text']) for entry in data['evidence']):
                    raise EvidenceUnavailable('no_new_business_evidence', '本批新记录没有可读正文，不会仅用旧上下文重复总结收费。', data['coverage'])
            require_evidence(data)
            sessions = json.loads(task['sessions'])
            if len(sessions) == 1 and automatic_rows is not None:
                from .conversations import context
                db = connect(self.path)
                try:
                    data['conversation_context'] = context(db, sessions[0], self.automation_since)
                    while data['conversation_context'] and len(json.dumps(data, ensure_ascii=False).encode()) > 48000:
                        data['conversation_context'].pop()
                finally:
                    db.close()
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256((encoded + self.runner.provider + self.runner.model).encode()).hexdigest()
            db = connect(self.path)
            try:
                old = db.execute('SELECT * FROM summaries WHERE task_id=? AND mode=? AND '
                                 '(fingerprint=? OR fingerprint LIKE ?) ORDER BY rowid DESC LIMIT 1',
                                 (task_id, mode, fingerprint, fingerprint + ':retry:%')).fetchone()
                if old and (old['status'] in ['queued', 'running', 'success'] or not retry):
                    return {'id': old['id'], 'status': old['status'], 'cached': True}
                job_id = uuid.uuid4().hex
                with db:
                    if old:
                        # Keep an auditable failed attempt; a new fingerprint permits only explicit retries.
                        fingerprint += ':retry:' + job_id
                    db.execute('INSERT INTO summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (job_id, task_id, mode, fingerprint, data['activity_digest'], 'queued', encoded,
                         None, None, self.runner.provider, self.runner.model, now, None, None, None))
                    if mode == 'final':
                        db.execute("UPDATE tasks SET status='completed',end_ts=? WHERE id=?", (task['end_ts'], task_id))
                return {'id': job_id, 'status': 'queued', 'cached': False}
            finally:
                db.close()

    def list(self, day):
        bounds(day, self.zone)
        db = connect(self.path)
        try:
            tasks = []
            start, end = bounds(day, self.zone)
            commits = [dict(row) for row in db.execute(
                'SELECT id,subject,files_changed,additions,deletions FROM git_commits WHERE ts>=? AND ts<? ORDER BY ts DESC,id',
                (start, end))]
            for row in db.execute('SELECT * FROM tasks WHERE day=? ORDER BY created_at DESC LIMIT 100', (day,)):
                task = dict(row)
                task['sessions'] = json.loads(task['sessions'])
                task['automatically_managed'] = bool(db.execute('SELECT 1 FROM task_automation WHERE task_id=?', (task['id'],)).fetchone())
                reviewed = db.execute('SELECT * FROM task_reviews WHERE task_id=?', (task['id'],)).fetchone()
                task['review'] = dict(reviewed) if reviewed else None
                task['commit_ids'] = [linked[0] for linked in db.execute(
                    'SELECT commit_id FROM task_git WHERE task_id=?', (task['id'],))]
                task['archived'] = task['status'] == 'completed'
                if task['sessions']:
                    marks = ','.join('?' for _ in task['sessions'])
                    task['project_names'] = [row[0] for row in db.execute(
                        'SELECT DISTINCT p.name FROM events e JOIN projects p ON p.id=e.project '
                        f'WHERE e.session IN ({marks}) ORDER BY p.name', task['sessions'])][:3]
                else:
                    task['project_names'] = []
                task['evidence_count'] = db.execute(
                    'SELECT COUNT(*) FROM automatic_coverage WHERE task_id=?', (task['id'],)).fetchone()[0]
                task['record_count'] = db.execute(
                    'SELECT COUNT(*) FROM work_records WHERE source_task_id=?', (task['id'],)).fetchone()[0]
                task['archived_records'] = db.execute(
                    'SELECT COUNT(*) FROM work_records WHERE source_task_id=? AND archived_at IS NOT NULL',
                    (task['id'],)).fetchone()[0]
                job = db.execute('SELECT id,mode,status,error,provider,model,created_at,finished_at,output_json,usage_json '
                                 'FROM summaries WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (task['id'],)).fetchone()
                saved = db.execute("SELECT id,output_json,input_json,created_at,finished_at FROM summaries WHERE task_id=? AND status='success' "
                    'ORDER BY created_at DESC,rowid DESC LIMIT 1', (task['id'],)).fetchone()
                task['saved_summary'] = json.loads(saved['output_json']) if saved else None
                task['saved_summary_job_id'] = saved['id'] if saved else None
                task['saved_summary_quality'] = summary_quality(json.loads(saved['input_json']), task['saved_summary']) if saved else None
                from .review_focus import review_focus
                task['review_focus'] = review_focus(task['review'], task['saved_summary'], task['saved_summary_quality'],
                    (saved['finished_at'] or saved['created_at']) if saved else None,
                    json.loads(saved['input_json']).get('coverage') if saved else None)
                task['summary'] = dict(job) if job else None
                if job:
                    task['summary']['result'] = json.loads(task['summary'].pop('output_json') or 'null')
                    task['summary']['usage'] = json.loads(task['summary'].pop('usage_json') or 'null')
                tasks.append(task)
            today = datetime.fromtimestamp(self.clock(), ZoneInfo(self.zone)).date().isoformat()
            runs_today = db.execute('SELECT COUNT(*) FROM summaries WHERE started_at>=? AND started_at<?', bounds(today, self.zone)).fetchone()[0]
            return {'tasks': tasks, 'total_tasks': db.execute('SELECT COUNT(*) FROM tasks WHERE day=?', (day,)).fetchone()[0],
                    'commits': commits, 'enabled': True, 'llm_enabled': self.runner is not None,
                    'auto': self.auto, 'allow_excerpts': self.allow_excerpts,
                    'runs_today': runs_today,
                    'idle_minutes': self.idle_seconds / 60, 'daily_limit': self.daily_limit,
                    'provider': self.runner.provider if self.runner else None,
                    'model': self.runner.model if self.runner else None,
                    'automation': self.automation_status()}
        finally:
            db.close()

    def review(self, task_id, status, outcome, learnings='', next_steps='', verification='', commit_ids=None, archive=None):
        task = self._task(task_id)
        if status not in ('confirmed', 'in_progress', 'blocked'):
            raise ValueError('Invalid review status')
        if archive is not None and type(archive) is not bool:
            raise ValueError('Invalid archive state')
        fields = [outcome, learnings, next_steps, verification]
        if any(not isinstance(value, str) or len(value) > 2000 for value in fields):
            raise ValueError('Invalid review text')
        if status == 'confirmed' and not outcome.strip():
            raise ValueError('Confirmed outcome required')
        commit_ids = commit_ids if commit_ids is not None else []
        if not isinstance(commit_ids, list) or len(commit_ids) > 20 or len(set(map(str, commit_ids))) != len(commit_ids):
            raise ValueError('Invalid commit selection')
        if any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{40,64}', item) for item in commit_ids):
            raise ValueError('Invalid commit selection')
        db = connect(self.path)
        try:
            if commit_ids:
                start, end = bounds(task['day'], self.zone)
                known = {row[0] for row in db.execute(
                    'SELECT id FROM git_commits WHERE ts>=? AND ts<? AND id IN (' +
                    ','.join('?' for _ in commit_ids) + ')', [start, end, *commit_ids])}
                if known != set(commit_ids):
                    raise ValueError('Unknown commit')
            with db:
                db.execute('''INSERT INTO task_reviews VALUES (?,?,?,?,?,?,?)
                              ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,
                              outcome=excluded.outcome,learnings=excluded.learnings,
                              next_steps=excluded.next_steps,verification=excluded.verification,
                              updated_at=excluded.updated_at''',
                           (task_id, status, outcome.strip(), learnings.strip(), next_steps.strip(),
                            verification.strip(), self.clock()))
                db.execute('DELETE FROM task_git WHERE task_id=?', (task_id,))
                db.executemany('INSERT INTO task_git VALUES (?,?)', [(task_id, item) for item in commit_ids])
                if archive is True:
                    self._set_archived(db, task, True)
                elif archive is False:
                    self._set_archived(db, task, False)
            current = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
            return {'task_id': task_id, 'status': status, 'commit_ids': commit_ids,
                    'archived': current == 'completed'}
        finally:
            db.close()

    def _set_archived(self, db, task, archived):
        """Keep the human review, the task and the automatic ledger in one state."""
        now = self.clock()
        if archived:
            _, day_end = bounds(task['day'], self.zone)
            end = task['end_ts'] if task['end_ts'] is not None else min(now, day_end)
            db.execute("UPDATE tasks SET status='completed',end_ts=? WHERE id=?", (end, task['id']))
            db.execute('UPDATE work_records SET archived_at=COALESCE(archived_at,?),updated_at=? '
                       'WHERE source_task_id=?', (now, now, task['id']))
        else:
            db.execute("UPDATE tasks SET status='open' WHERE id=?", (task['id'],))
            db.execute('UPDATE work_records SET archived_at=NULL,updated_at=? '
                       'WHERE source_task_id=? AND archived_at IS NOT NULL', (now, task['id']))

    def archive(self, task_ids, archived):
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 50:
            raise ValueError('Select 1 to 50 tasks')
        if type(archived) is not bool:
            raise ValueError('Invalid archive state')
        if len(set(task_ids)) != len(task_ids) or any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{32}', item) for item in task_ids):
            raise ValueError('Invalid task IDs')
        db = connect(self.path)
        try:
            tasks = []
            for task_id in task_ids:
                row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not row:
                    raise ValueError('Task not found')
                tasks.append(dict(row))
            with db:
                for task in tasks:
                    self._set_archived(db, task, archived)
            return {'archived': archived, 'count': len(tasks)}
        finally:
            db.close()

    def configure_summary(self, allow_excerpts, backend=None):
        if type(allow_excerpts) is not bool:
            raise ValueError('Explicit excerpt consent required')
        if allow_excerpts and not self.roots:
            raise ValueError('No configured sources for excerpts')
        if backend not in (None, 'pi', 'codebuddy'):
            raise ValueError('Unsupported summary backend')
        with self.guard:
            db = connect(self.path)
            try:
                if self.runner is not None and db.execute("SELECT 1 FROM summaries WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise ValueError('Wait for pending summaries before changing model settings')
            finally:
                db.close()
            current = 'codebuddy' if getattr(self.runner, 'provider', None) == 'codebuddy' else 'pi'
            if self.runner is None or (backend is not None and backend != current):
                from .llm import create_runner
                self.runner = create_runner('codebuddy') if backend == 'codebuddy' else create_runner()
            self.allow_excerpts = allow_excerpts
            if self.auto:
                self.automation_reason = ''
                from .automation import persist
                persist(self)
                self.automation_wake.set()
        return {'llm_enabled': True, 'allow_excerpts': self.allow_excerpts,
                'provider': self.runner.provider, 'model': self.runner.model}

    def configure_automation(self, enabled, scope='now', consent=False, idle_minutes=3):
        from .automation import configure
        return configure(self, enabled, scope, consent, idle_minutes)

    def retry_automation(self):
        from .automation import retry_failures
        return retry_failures(self)

    def automation_status(self):
        from .automation import status
        return status(self)

    def revise_work_record(self, record_id, category=None, archived=None):
        from .ledger import revise_record
        db = connect(self.path)
        try:
            return revise_record(db, record_id, category, archived, self.clock())
        finally:
            db.close()

    def defer_session(self, session, day, deferred):
        from .triage import defer_session
        db = connect(self.path)
        try:
            return defer_session(db, session, day, self.zone, deferred, self.clock())
        finally:
            db.close()

    def rename_session(self, session, name):
        if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{16}', session):
            raise ValueError('Invalid session')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Name must contain 1 to 80 characters')
        db = connect(self.path)
        try:
            if not db.execute('SELECT 1 FROM events WHERE session=? LIMIT 1', (session,)).fetchone():
                raise ValueError('Unknown session')
            with db:
                db.execute('INSERT INTO session_names VALUES (?,?,?) ON CONFLICT(session) '
                           'DO UPDATE SET name=excluded.name,origin=excluded.origin',
                           (session, redact(name.strip(), 80), 'manual'))
            return {'renamed': True}
        finally:
            db.close()

    def name_sessions(self, day):
        # Explicit local-only consent: read bounded user titles, store only compact display names.
        from .adapters import collect
        from .presentation import topic_name
        start, end = bounds(day, self.zone)
        if not self.roots:
            raise ValueError('No configured sources for topic extraction')
        db = connect(self.path)
        try:
            known = {row[0] for row in db.execute(
                'SELECT DISTINCT session FROM events WHERE ts>=? AND ts<?', (start, end))}
            titles = {}
            coverage = {}
            for source, root in self.roots.items():
                events, stats = collect(root, source, self.key, titles=True, max_files=20)
                coverage[source] = stats
                for event in sorted(events, key=lambda item: item['ts']):
                    if event['session'] in known and start <= event['ts'] < end and event['title']:
                        titles.setdefault(event['session'], []).append(event['title'])
            changed = 0
            with db:
                for session, candidates in titles.items():
                    name = topic_name(candidates)
                    if name:
                        changed += db.execute('INSERT INTO session_names VALUES (?,?,?) '
                            "ON CONFLICT(session) DO UPDATE SET name=excluded.name WHERE session_names.origin='excerpt'",
                            (session, name, 'excerpt')).rowcount
            return {'renamed': changed, 'coverage': coverage}
        finally:
            db.close()

    def auto_tick(self):
        from .automation import tick
        tick(self)

    def _loop(self):
        next_auto = 0
        while not self.stop.wait(0.5):
            if time.monotonic() >= next_auto or self.automation_wake.is_set():
                self.automation_wake.clear()
                next_auto = time.monotonic() + 15
                try:
                    self.auto_tick()
                except (OSError, ValueError, sqlite3.Error):
                    LOGGER.warning('Auto summary scan unavailable; private diagnostics suppressed.')
            if self.runner is None:
                continue
            try:
                self._one_job()
            except (OSError, ValueError, sqlite3.Error):
                LOGGER.warning('Summary queue unavailable; private diagnostics suppressed.')
                self.stop.wait(1)

    def _one_job(self):
        with self.guard:
            db = connect(self.path)
            try:
                job = db.execute("SELECT * FROM summaries WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
                if not job:
                    return
                job = dict(job)
                with db:
                    db.execute("UPDATE summaries SET status='running',started_at=? WHERE id=?", (self.clock(), job['id']))
            finally:
                db.close()
        result, cost, error = None, None, None
        try:
            data = json.loads(job['input_json'])
            if (job['provider'] != self.runner.provider or job['model'] != self.runner.model
                    or data['coverage']['allow_excerpts'] != self.allow_excerpts
                    or data.get('source_scope') != source_scope(self.roots, self.key)):
                raise SummaryError('configuration_changed')
            if data.get('evidence_version') != 2:
                raise SummaryError('configuration_changed')
            require_evidence(data)
            model_data = dict(data)
            model_data.pop('automatic_batch', None)
            result, cost = self.runner(model_data, job['mode'], self.stop)
            result = validate_result(json.dumps(result, ensure_ascii=False), {e['id'] for e in data['evidence']})
            validate_business_result(result, data)
        except EvidenceUnavailable:
            error = 'insufficient_evidence'
        except SummaryError as failure:
            error = str(failure) if str(failure) in ['cancelled', 'timeout', 'pi_failed', 'output_limit',
                'diagnostic_limit', 'input_limit', 'invalid_output', 'invalid_json', 'invalid_schema',
                'invalid_text', 'invalid_citation', 'invalid_pi_stream', 'tools_forbidden',
                'incomplete_response', 'configuration_changed', 'codebuddy_failed',
                'invalid_codebuddy_stream', 'business_summary_required', 'non_content_citation',
                'missing_outcome', 'missing_verification', 'context_only_summary', 'status_conflict'] else 'summary_failed'
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            error = 'summary_failed'
        db = connect(self.path)
        try:
            with db:
                db.execute('UPDATE summaries SET status=?,output_json=?,usage_json=?,error=?,finished_at=? WHERE id=?',
                           ('error' if error else 'success', json.dumps(result, ensure_ascii=False) if result else None,
                            json.dumps(cost) if cost else None, error, self.clock(), job['id']))
                if not error:
                    from .ledger import project_summary
                    project_summary(db, self._task(job['task_id']), job['id'], data, result, self.clock())
                    from .automation import mark_covered
                    mark_covered(db, job['task_id'], data, result)
                db.execute('UPDATE task_automation SET last_error=?,updated_at=? WHERE task_id=?',
                           (error, self.clock(), job['task_id']))
                if not error and data['coverage'].get('excerpts_available', 0):
                    sessions = {item['session'] for item in data['evidence']}
                    if len(sessions) == 1:
                        from .presentation import topic_name
                        name = topic_name([result['work_items'][0]['title']] if result.get('work_items') else [result['overview']['text']])
                        if name:
                            db.execute('INSERT INTO session_names VALUES (?,?,?) ON CONFLICT(session) '
                                "DO UPDATE SET name=excluded.name,origin=excluded.origin WHERE session_names.origin!='manual'",
                                (next(iter(sessions)), name, 'summary'))
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            with db:
                db.execute("UPDATE summaries SET status='error',output_json=NULL,error='projection_failed',finished_at=? WHERE id=?",
                           (self.clock(), job['id']))
                db.execute("UPDATE task_automation SET last_error='projection_failed',updated_at=? WHERE task_id=?",
                           (self.clock(), job['task_id']))
        finally:
            db.close()
