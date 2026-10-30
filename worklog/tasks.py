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
from .evidence import activity_digest, redact, snapshot, source_scope
from .llm import SummaryError, validate_result
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
        self.daily_limit = daily_limit
        self.zone = zone
        self.clock = clock
        self.started = clock()
        self.stop = threading.Event()
        self.guard = threading.RLock()
        if allow_excerpts and not self.roots:
            raise ValueError('Excerpts require explicitly configured source directories')
        if any(not Path(root).expanduser().is_dir() for root in self.roots.values()):
            raise ValueError('Source directories must exist')
        if idle_seconds < 60 or not 1 <= daily_limit <= 100:
            raise ValueError('Invalid idle threshold or daily job limit')
        connect(self.path).close()
        self.key = salt(self.path)
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
                           (task_id, redact(title, 100), day, json.dumps(list(dict.fromkeys(sessions))),
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

    def request(self, task_id, mode='stage', retry=False):
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
            data = snapshot(task, self._rows(task), self.roots, self.key, self.allow_excerpts)
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256((encoded + self.runner.provider + self.runner.model).encode()).hexdigest()
            db = connect(self.path)
            try:
                old = db.execute('SELECT * FROM summaries WHERE task_id=? AND mode=? AND '
                                 '(fingerprint=? OR fingerprint LIKE ?) ORDER BY rowid DESC LIMIT 1',
                                 (task_id, mode, fingerprint, fingerprint + ':retry:%')).fetchone()
                if old and (old['status'] in ['queued', 'running', 'success'] or not retry):
                    return {'id': old['id'], 'status': old['status'], 'cached': True}
                today = datetime.fromtimestamp(now, ZoneInfo(self.zone)).date().isoformat()
                daily_start, daily_end = bounds(today, self.zone)
                used = db.execute('SELECT COUNT(*) FROM summaries WHERE created_at>=? AND created_at<?',
                                  (daily_start, daily_end)).fetchone()[0]
                if used >= self.daily_limit:
                    raise ValueError('Daily summary job limit reached')
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
                reviewed = db.execute('SELECT * FROM task_reviews WHERE task_id=?', (task['id'],)).fetchone()
                task['review'] = dict(reviewed) if reviewed else None
                task['commit_ids'] = [linked[0] for linked in db.execute(
                    'SELECT commit_id FROM task_git WHERE task_id=?', (task['id'],))]
                job = db.execute('SELECT id,mode,status,error,provider,model,created_at,finished_at,output_json,usage_json '
                                 'FROM summaries WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (task['id'],)).fetchone()
                task['summary'] = dict(job) if job else None
                if job:
                    task['summary']['result'] = json.loads(task['summary'].pop('output_json') or 'null')
                    task['summary']['usage'] = json.loads(task['summary'].pop('usage_json') or 'null')
                tasks.append(task)
            today = datetime.fromtimestamp(self.clock(), ZoneInfo(self.zone)).date().isoformat()
            runs_today = db.execute('SELECT COUNT(*) FROM summaries WHERE started_at>=? AND started_at<?', bounds(today, self.zone)).fetchone()[0]
            return {'tasks': tasks, 'commits': commits, 'enabled': True, 'llm_enabled': self.runner is not None,
                    'auto': self.auto, 'allow_excerpts': self.allow_excerpts,
                    'runs_today': runs_today,
                    'idle_minutes': self.idle_seconds / 60, 'daily_limit': self.daily_limit,
                    'provider': self.runner.provider if self.runner else None,
                    'model': self.runner.model if self.runner else None}
        finally:
            db.close()

    def review(self, task_id, status, outcome, learnings='', next_steps='', verification='', commit_ids=None):
        task = self._task(task_id)
        if status not in ('confirmed', 'in_progress', 'blocked'):
            raise ValueError('Invalid review status')
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
            return {'task_id': task_id, 'status': status, 'commit_ids': commit_ids}
        finally:
            db.close()

    def auto_tick(self):
        if not self.auto:
            return
        with self.guard:
            now = self.clock()
            day = datetime.fromtimestamp(now, ZoneInfo(self.zone)).date().isoformat()
            start, end = bounds(day, self.zone)
            db = connect(self.path)
            try:
                sessions = [r[0] for r in db.execute("SELECT DISTINCT session FROM events WHERE ts>=? AND ts<? AND source IN ('pi','codex')",
                                                    (max(start, self.started), min(end, now + 1)))]
                tasks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE day=? AND status='open'", (day,))]
                for session in sessions:
                    if any(session in json.loads(t['sessions']) for t in tasks):
                        continue
                    latest_end = db.execute("SELECT MAX(end_ts) FROM tasks WHERE day=? AND status='completed' AND sessions LIKE ?",
                                            (day, '%' + session + '%')).fetchone()[0]
                    since = max(start, self.started, (latest_end or 0) + 0.001)
                    has_new = db.execute('SELECT 1 FROM events WHERE session=? AND ts>? AND ts<=? LIMIT 1', (session, since, now)).fetchone()
                    if has_new:
                        task = self.create('自动任务 · 会话' + session[:8], [session], day, internal_start=since)
                        tasks.append(task)
                for task in tasks:
                    rows = self._rows(task)
                    active = [r for r in rows if r['kind'] not in ['usage', 'auxiliary']]
                    if not active:
                        continue
                    last = active[-1]
                    # Don't mistake an outstanding tool call/user input for a completed run.
                    if last['status'] not in ['stop', 'length', 'error', 'aborted']:
                        continue
                    if last['ts'] < self.started or now - last['ts'] < self.idle_seconds:
                        continue
                    digest = activity_digest(rows)
                    if db.execute("SELECT 1 FROM summaries WHERE task_id=? AND mode='stage' AND activity_digest=? LIMIT 1",
                                  (task['id'], digest)).fetchone():
                        continue
                    try:
                        self.request(task['id'], 'stage')
                    except ValueError:
                        pass  # Budget/scope guard: no automatic retry or exception contents.
            finally:
                db.close()

    def _loop(self):
        if self.runner is None:
            return
        next_auto = 0
        while not self.stop.wait(0.5):
            if time.monotonic() >= next_auto:
                next_auto = time.monotonic() + 15
                try:
                    self.auto_tick()
                except (OSError, ValueError, sqlite3.Error):
                    LOGGER.warning('Auto summary scan unavailable; private diagnostics suppressed.')
            try:
                self._one_job()
            except (OSError, ValueError, sqlite3.Error):
                LOGGER.warning('Summary queue unavailable; private diagnostics suppressed.')
                self.stop.wait(1)

    def _one_job(self):
        with self.guard:
            db = connect(self.path)
            try:
                today = datetime.fromtimestamp(self.clock(), ZoneInfo(self.zone)).date().isoformat()
                used = db.execute('SELECT COUNT(*) FROM summaries WHERE started_at>=? AND started_at<?', bounds(today, self.zone)).fetchone()[0]
                if used >= self.daily_limit:
                    return  # Old queued jobs must not bypass today's execution budget.
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
            result, cost = self.runner(data, job['mode'], self.stop)
            result = validate_result(json.dumps(result, ensure_ascii=False), {e['id'] for e in data['evidence']})
        except SummaryError as failure:
            error = str(failure) if str(failure) in ['cancelled', 'timeout', 'pi_failed', 'output_limit',
                'diagnostic_limit', 'input_limit', 'invalid_output', 'invalid_json', 'invalid_schema',
                'invalid_text', 'invalid_citation', 'invalid_pi_stream', 'tools_forbidden',
                'incomplete_response', 'configuration_changed'] else 'summary_failed'
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            error = 'summary_failed'
        db = connect(self.path)
        try:
            with db:
                db.execute('UPDATE summaries SET status=?,output_json=?,usage_json=?,error=?,finished_at=? WHERE id=?',
                           ('error' if error else 'success', json.dumps(result, ensure_ascii=False) if result else None,
                            json.dumps(cost) if cost else None, error, self.clock(), job['id']))
        finally:
            db.close()
