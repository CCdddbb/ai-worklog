import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog.adapters import event
from worklog.core import bounds
from worklog.store import connect, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class TaskArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'worklog.sqlite3'
        key = salt(self.db)
        rows = [event(key, 'pi', 's1', 'project', 'u1', START + 3600, 'user', title='处理登录问题')]
        save(self.db, rows, {'source': 'pi', 'selected': 1}, DAY, 'UTC')
        self.service = TaskService(self.db, None, zone='UTC', clock=lambda: START + 7200)
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

    def record_state(self, task_id):
        db = connect(self.db)
        try:
            return [row[0] for row in db.execute(
                'SELECT archived_at FROM work_records WHERE source_task_id=? ORDER BY id', (task_id,))]
        finally:
            db.close()

    def test_review_archive_finishes_task_and_ledger_together(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.records(task['id'], 2)
        result = self.service.review(
            task['id'], 'confirmed', '登录回调已修复', '', '观察线上日志', '本地集成测试通过', [],
            archive=True)
        self.assertTrue(result['archived'])
        self.assertEqual(result['status'], 'confirmed')
        self.assertTrue(all(value is not None for value in self.record_state(task['id'])))
        listing = self.service.list(DAY)['tasks'][0]
        self.assertTrue(listing['archived'])
        self.assertEqual(listing['archived_records'], 2)
        self.assertEqual(listing['record_count'], 2)
        self.assertEqual(listing['project_names'], ['project'])
        self.assertEqual(listing['review']['outcome'], '登录回调已修复')

    def test_review_reopen_clears_archive_state(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.records(task['id'], 1)
        self.service.review(task['id'], 'confirmed', '完成', '', '', '', [], archive=True)
        reopened = self.service.review(task['id'], 'in_progress', '继续跟进', '', '', '', [], archive=False)
        self.assertFalse(reopened['archived'])
        self.assertEqual(reopened['status'], 'in_progress')
        self.assertEqual(self.record_state(task['id']), [None])
        listing = self.service.list(DAY)['tasks'][0]
        self.assertFalse(listing['archived'])
        self.assertEqual(listing['archived_records'], 0)

    def test_review_without_archive_keeps_previous_state(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.records(task['id'], 1)
        self.service.review(task['id'], 'confirmed', '完成', '', '', '', [], archive=True)
        plain = self.service.review(task['id'], 'confirmed', '补充结论', '', '', '', [])
        self.assertTrue(plain['archived'])
        self.assertTrue(all(value is not None for value in self.record_state(task['id'])))

    def test_batch_archive_validates_input_and_updates_every_task(self):
        first = self.service.create('登录问题', [self.session], DAY)
        second = self.service.create('回调排查', [], DAY)
        self.records(first['id'], 1)
        result = self.service.archive([first['id'], second['id']], True)
        self.assertEqual(result, {'archived': True, 'count': 2})
        self.assertTrue(all(value is not None for value in self.record_state(first['id'])))
        statuses = {task['id']: task['status'] for task in self.service.list(DAY)['tasks']}
        self.assertEqual(statuses, {first['id']: 'completed', second['id']: 'completed'})
        self.service.archive([first['id'], second['id']], False)
        self.assertEqual(self.record_state(first['id']), [None])

        for ids, archived in [([], True), (['x'], True), ([first['id']] * 2, True),
                              ([first['id']], 'true'), (['a' * 32], True), ([first['id']] * 51, True)]:
            with self.assertRaises(ValueError):
                self.service.archive(ids, archived)

    def test_archive_api_requires_auth_origin_and_known_fields(self):
        task = self.service.create('登录问题', [self.session], DAY)
        server = make_server(self.db, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'task_ids': [task['id']], 'archived': True}
            self.assertEqual(self.request(server, '/api/task-archive', body, auth=False)[0], 401)
            self.assertEqual(self.request(server, '/api/task-archive', body, origin=False)[0], 403)
            self.assertEqual(self.request(server, '/api/task-archive', {'task_ids': [task['id']]})[0], 400)
            self.assertEqual(self.request(server, '/api/task-archive', dict(body, archived='true'))[0], 400)
            self.assertEqual(self.request(server, '/api/task-archive', dict(body, extra=1))[0], 400)
            status, payload = self.request(server, '/api/task-archive', body)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload), {'archived': True, 'count': 1})
            listing = json.loads(self.request(server, '/api/tasks?date=' + DAY)[1])
            self.assertTrue(listing['tasks'][0]['archived'])
            self.assertEqual(self.request(server, '/api/task-archive', dict(body, archived=False))[0], 200)
            listing = json.loads(self.request(server, '/api/tasks?date=' + DAY)[1])
            self.assertFalse(listing['tasks'][0]['archived'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_review_api_accepts_archive_flag_only_as_boolean(self):
        task = self.service.create('登录问题', [self.session], DAY)
        server = make_server(self.db, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'task_id': task['id'], 'status': 'confirmed', 'outcome': '完成', 'learnings': '',
                    'next_steps': '', 'verification': '', 'commit_ids': [], 'archive': True}
            self.assertEqual(self.request(server, '/api/task-review', dict(body, archive='yes'))[0], 400)
            status, payload = self.request(server, '/api/task-review', body)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(payload)['archived'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
