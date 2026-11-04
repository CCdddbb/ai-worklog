"""Deleting invalid work records must also ignore their source conversations."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog import automation
from worklog.adapters import event
from worklog.core import bounds
from worklog.store import connect, report_data, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class TaskDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'worklog.sqlite3'
        key = salt(self.db)
        rows = [event(key, 'pi', 's1', 'project', 'u1', START + 3600, 'user', title='处理登录问题'),
                event(key, 'pi', 's1', 'project', 'u2', START + 3700, 'assistant', '已修复')]
        save(self.db, rows, {'source': 'pi', 'selected': 1}, DAY, 'UTC')
        self.clock = [START + 7200]
        self.service = TaskService(self.db, None, zone='UTC', clock=lambda: self.clock[0])
        db = connect(self.db)
        self.session = db.execute('SELECT session FROM events LIMIT 1').fetchone()[0]
        db.close()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def records(self, task_id, count=1):
        db = connect(self.db)
        with db:
            for index in range(count):
                db.execute(
                    'INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'r{index}{task_id[:8]}', DAY, task_id, 'job', '登录问题', 'bugfix', 0,
                     'reported_resolved', '{}', '[]', '[]', '[]', START + 3600,
                     START + 4000, 400, None, START + 3600, START + 3600))
        db.close()

    def seed_derived(self, task):
        db = connect(self.db)
        with db:
            db.execute('INSERT INTO summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('job-' + task['id'][:8], task['id'], 'stage', 'fp', 'digest', 'success', '{}', '{}',
                        None, 'fake', 'fixture', START, START, '{}', START))
            db.execute('INSERT INTO task_git VALUES (?,?)', (task['id'], 'c1'))
            db.execute('INSERT INTO automatic_coverage VALUES (?,?,?)', (task['id'], 'e1', 'd'))
            db.execute('INSERT INTO task_automation VALUES (?,?,?,?,?,?)',
                       (task['id'], 'project', 0, None, None, START))
            db.execute('INSERT INTO conversation_sources VALUES (?,?,?)', (task['id'], self.session, DAY))
            db.execute('INSERT INTO conversation_parts VALUES (?,?,?,?,?,?,?,?)',
                       ('part1', self.session, '登录', 'bugfix', 'reported_resolved', '{}', START, START))
            db.execute('INSERT INTO conversation_progress VALUES (?,?,?,?,?)',
                       ('part1', 'job-' + task['id'][:8], DAY, '{}', START))
            db.execute('INSERT INTO conversation_part_records VALUES (?,?)', ('part1', f'r0{task["id"][:8]}'))
            db.execute('INSERT INTO work_record_sessions VALUES (?,?)', (f'r0{task["id"][:8]}', self.session))
            db.execute('INSERT INTO work_record_history VALUES (?,?,?,?)',
                       (f'r0{task["id"][:8]}', 'job-' + task['id'][:8], '{}', START))
            db.execute('INSERT INTO legacy_shared_records VALUES (?)', (f'r0{task["id"][:8]}',))
        db.close()

    def count(self, sql, params=()):
        db = connect(self.db)
        try:
            return db.execute(sql, params).fetchone()[0]
        finally:
            db.close()

    def test_delete_removes_records_drafts_and_ignores_conversation(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.records(task['id'], 2)
        self.service.review(task['id'], 'confirmed', '登录回调已修复', '', '', '', [])
        self.seed_derived(task)
        events = self.count('SELECT COUNT(*) FROM events')

        result = self.service.delete([task['id']])

        self.assertEqual(result, {'deleted': 1, 'ignored_sessions': [self.session]})
        for table in ('tasks', 'summaries', 'task_reviews', 'task_git', 'automatic_coverage',
                      'task_automation', 'conversation_sources', 'work_records',
                      'work_record_history', 'work_record_sessions', 'conversation_parts',
                      'conversation_progress', 'conversation_part_records',
                      'legacy_shared_records'):
            self.assertEqual(self.count(f'SELECT COUNT(*) FROM {table}'), 0, table)
        self.assertEqual(self.count('SELECT COUNT(*) FROM ignored_sessions WHERE session=?', (self.session,)), 1)
        self.assertEqual(self.count('SELECT COUNT(*) FROM events'), events)
        self.assertEqual(self.service.list(DAY)['tasks'], [])

    def test_shared_session_stays_ignored_only_when_no_task_keeps_it(self):
        first = self.service.create('登录问题', [self.session], DAY)
        second = self.service.create('同一对话的另一项工作', [self.session], DAY)
        result = self.service.delete([first['id']])
        self.assertEqual(result['ignored_sessions'], [])
        self.assertEqual(self.count('SELECT COUNT(*) FROM ignored_sessions'), 0)
        self.assertEqual([task['id'] for task in self.service.list(DAY)['tasks']], [second['id']])

        result = self.service.delete([second['id']])
        self.assertEqual(result['ignored_sessions'], [self.session])
        self.assertEqual(self.count('SELECT COUNT(*) FROM ignored_sessions'), 1)

    def test_batch_delete_is_atomic_and_validates_ids(self):
        first = self.service.create('登录问题', [self.session], DAY)
        second = self.service.create('手工记录', [], DAY)
        self.records(first['id'], 1)

        for ids in ([], ['x'], [first['id']] * 2, ['a' * 32], [first['id']] * 51,
                    [first['id'], 'b' * 32], first['id']):
            with self.assertRaises(ValueError):
                self.service.delete(ids)
        self.assertEqual(self.count('SELECT COUNT(*) FROM tasks'), 2)
        self.assertEqual(self.count('SELECT COUNT(*) FROM work_records'), 1)

        result = self.service.delete([first['id'], second['id']])
        self.assertEqual(result, {'deleted': 2, 'ignored_sessions': [self.session]})
        self.assertEqual(self.count('SELECT COUNT(*) FROM tasks'), 0)

    def test_automatic_capture_skips_ignored_sessions_until_restored(self):
        self.service.auto = True
        self.service.automation_since = START
        self.service.automation_scope = 'today'
        automation.tick(self.service)
        tasks = self.service.list(DAY)['tasks']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['sessions'], [self.session])

        self.service.delete([tasks[0]['id']])
        self.clock[0] += 120
        automation.tick(self.service)
        self.assertEqual(self.service.list(DAY)['tasks'], [])
        self.assertEqual(self.count('SELECT COUNT(*) FROM ignored_sessions'), 1)

        self.service.ignore_session(self.session, False)
        self.clock[0] += 120
        automation.tick(self.service)
        tasks = self.service.list(DAY)['tasks']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['sessions'], [self.session])

    def test_ignored_sessions_are_marked_in_the_session_list(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.service.delete([task['id']])
        data = report_data(self.db, DAY, 'UTC')
        session = next(item for item in data['sessions'] if item['session'] == self.session)
        self.assertTrue(session['ignored'])
        self.assertFalse(session['deferred'])
        self.service.ignore_session(self.session, False)
        data = report_data(self.db, DAY, 'UTC')
        self.assertFalse(next(item for item in data['sessions'] if item['session'] == self.session)['ignored'])

    def request(self, server, path, body=None, auth=True, origin=True):
        base = f'http://127.0.0.1:{server.server_port}'
        headers = {'Authorization': 'Bearer ' + server.access_token} if auth else {}
        if origin:
            headers['Origin'] = base
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_delete_and_ignore_api_require_auth_origin_and_known_fields(self):
        task = self.service.create('登录问题', [self.session], DAY)
        server = make_server(self.db, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'task_ids': [task['id']]}
            self.assertEqual(self.request(server, '/api/task-delete', body, auth=False)[0], 401)
            self.assertEqual(self.request(server, '/api/task-delete', body, origin=False)[0], 403)
            self.assertEqual(self.request(server, '/api/task-delete', {'task_ids': task['id']})[0], 400)
            self.assertEqual(self.request(server, '/api/task-delete', dict(body, extra=1))[0], 400)
            self.assertEqual(self.request(server, '/api/task-delete', {'task_ids': ['b' * 32]})[0], 400)
            status, payload = self.request(server, '/api/task-delete', body)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload), {'deleted': 1, 'ignored_sessions': [self.session]})

            self.assertEqual(self.request(server, '/api/session-ignore',
                                          {'session': self.session, 'ignored': 'yes'})[0], 400)
            self.assertEqual(self.request(server, '/api/session-ignore',
                                          {'session': 'x', 'ignored': True})[0], 400)
            self.assertEqual(self.request(server, '/api/session-ignore',
                                          {'session': self.session, 'ignored': True, 'extra': 1})[0], 400)
            self.assertEqual(self.request(server, '/api/session-ignore',
                                          {'session': self.session, 'ignored': False})[0], 200)
            self.assertEqual(self.count('SELECT COUNT(*) FROM ignored_sessions'), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
