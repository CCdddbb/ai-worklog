import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog.core import bounds
from worklog.store import connect
from worklog.tasks import TaskService
from worklog.web import MAX_JSON_BODY, make_server


class TaskWorkspaceTests(unittest.TestCase):
    def test_list_discloses_full_count_when_current_window_is_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            start, _ = bounds('2026-10-28', 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                db = connect(path)
                with db:
                    db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                        [(f'{index:032x}', f'合成记录 {index}', '2026-10-28', '[]', start, None, 'open', start + index)
                         for index in range(120)])
                db.close()
                listing = service.list('2026-10-28')
                self.assertEqual(listing['total_tasks'], 120)
                self.assertEqual(len(listing['tasks']), 100)
                self.assertEqual(listing['tasks'][0]['title'], '合成记录 119')
                self.assertEqual(listing['tasks'][-1]['title'], '合成记录 20')
            finally:
                service.close()

    def test_valid_long_business_title_is_not_silently_cut_to_100_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            start, _ = bounds('2026-10-28', 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                title = '修复登录回调并补充验证和上线观察计划' * 7
                self.assertGreater(len(title), 100)
                self.assertLessEqual(len(title), 150)
                created = service.create(title, [], '2026-10-28')
                self.assertEqual(created['title'], title)
                self.assertEqual(service.list('2026-10-28')['tasks'][0]['title'], title)
            finally:
                service.close()

    def test_review_accepts_valid_multibyte_fields_but_rejects_oversized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            start, _ = bounds('2026-10-28', 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            server = make_server(path, '2026-10-28', 'UTC', 0, task_service=service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def post(values):
                body = json.dumps(values, ensure_ascii=False).encode('utf-8')
                request = urllib.request.Request(base + '/api/task-review', data=body, headers={
                    'Authorization': 'Bearer ' + server.access_token,
                    'Origin': base, 'Content-Type': 'application/json',
                })
                try:
                    with opener.open(request, timeout=3) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    with error:
                        return error.code

            try:
                task = service.create('合成多字节工作审阅', [], '2026-10-28')
                values = {'task_id': task['id'], 'status': 'confirmed', 'outcome': '结果' * 1000,
                          'learnings': '经验' * 1000, 'next_steps': '后续' * 1000,
                          'verification': '验证' * 1000, 'commit_ids': []}
                size = len(json.dumps(values, ensure_ascii=False).encode('utf-8'))
                self.assertGreater(size, 16384)
                self.assertLess(size, MAX_JSON_BODY)
                self.assertEqual(post(values), 200)
                self.assertEqual(service.list('2026-10-28')['tasks'][0]['review']['verification'], values['verification'])
                self.assertEqual(post({**values, 'outcome': '验' * 2001}), 400)
                self.assertEqual(post({**values, 'outcome': 'x' * MAX_JSON_BODY}), 413)
                self.assertEqual(service.list('2026-10-28')['tasks'][0]['review']['outcome'], values['outcome'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                service.close()

    def test_empty_day_reports_zero_without_borrowing_another_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            start, _ = bounds('2026-10-28', 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                service.create('合成线下工作', [], '2026-10-28')
                self.assertEqual(service.list('2026-10-28')['total_tasks'], 1)
                listing = service.list('2026-10-27')
                self.assertEqual(listing['total_tasks'], 0)
                self.assertEqual(listing['tasks'], [])
            finally:
                service.close()

    def test_list_window_reports_its_limit_and_whether_more_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            day = '2026-10-28'
            start, _ = bounds(day, 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                db = connect(path)
                with db:
                    db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                        [(f'{index:032x}', f'合成记录 {index}', day, '[]', start, None, 'open', start + index)
                         for index in range(120)])
                db.close()
                small = service.list(day, limit=10)
                self.assertEqual(small['window_limit'], 10)
                self.assertTrue(small['has_more'])
                self.assertEqual(small['total_tasks'], 120)
                self.assertEqual(len(small['tasks']), 10)
                self.assertEqual(small['tasks'][0]['title'], '合成记录 119')
                full = service.list(day, limit=500)
                self.assertEqual(len(full['tasks']), 120)
                self.assertFalse(full['has_more'])
            finally:
                service.close()

    def test_list_pins_requested_ids_outside_window_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            day = '2026-10-28'
            start, _ = bounds(day, 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                db = connect(path)
                with db:
                    db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                        [(f'{index:032x}', f'合成记录 {index}', day, '[]', start, None, 'open', start + index)
                         for index in range(120)])
                db.close()
                oldest, newest = f'{0:032x}', f'{119:032x}'
                pinned = service.list(day, limit=10, ids=[oldest, oldest])
                self.assertEqual(len(pinned['tasks']), 11)
                self.assertEqual(sum(task['id'] == oldest for task in pinned['tasks']), 1)
                self.assertEqual(pinned['window_limit'], 10)
                self.assertTrue(pinned['has_more'])
                self.assertEqual(pinned['tasks'][-1]['id'], oldest)
                self.assertEqual(len(service.list(day, limit=10, ids=[newest])['tasks']), 10)
                for bad in (0, 501, 'abc', None):
                    with self.subTest(limit=bad):
                        with self.assertRaises(ValueError):
                            service.list(day, limit=bad)
                with self.assertRaises(ValueError):
                    service.list(day, ids=oldest)
                with self.assertRaises(ValueError):
                    service.list(day, ids=['x' * 65])
                with self.assertRaises(ValueError):
                    service.list(day, ids=[f'{index:032x}' for index in range(21)])
            finally:
                service.close()

    def test_tasks_api_accepts_limit_and_repeated_or_comma_separated_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.sqlite3'
            day = '2026-10-28'
            start, _ = bounds(day, 'UTC')
            service = TaskService(path, None, zone='UTC', clock=lambda: start + 12 * 3600)
            try:
                db = connect(path)
                with db:
                    db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                        [(f'{index:032x}', f'合成记录 {index}', day, '[]', start, None, 'open', start + index)
                         for index in range(12)])
                db.close()
                server = make_server(path, day, 'UTC', 0, task_service=service)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def call(query):
                        request = urllib.request.Request(
                            f'http://127.0.0.1:{server.server_port}/api/tasks?{query}',
                            headers={'Authorization': 'Bearer ' + server.access_token})
                        try:
                            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                                    request, timeout=3) as response:
                                return response.status, json.load(response)
                        except urllib.error.HTTPError as error:
                            return error.code, json.load(error)

                    oldest, second, newest = f'{0:032x}', f'{1:032x}', f'{11:032x}'
                    status, window = call('limit=5')
                    self.assertEqual(status, 200)
                    self.assertEqual(window['window_limit'], 5)
                    self.assertTrue(window['has_more'])
                    self.assertEqual(window['total_tasks'], 12)
                    self.assertEqual(len(window['tasks']), 5)
                    status, pinned = call(f'limit=5&ids={oldest}')
                    self.assertEqual(status, 200)
                    self.assertEqual(len(pinned['tasks']), 6)
                    self.assertIn(oldest, [task['id'] for task in pinned['tasks']])
                    self.assertEqual(len(call(f'limit=5&ids={oldest},{newest}')[1]['tasks']), 6)
                    self.assertEqual(len(call(f'limit=5&ids={oldest}&ids={second}')[1]['tasks']), 7)
                    for query in ('limit=0', 'limit=501', 'limit=abc',
                                  'limit=5&ids=' + ','.join(f'{index:032x}' for index in range(21))):
                        with self.subTest(query=query[:24]):
                            self.assertEqual(call(query)[0], 400)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
