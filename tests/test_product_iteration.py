import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog.adapters import event
from worklog.cli import main
from worklog.core import bounds
from worklog.store import connect, report_data, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class ProductIterationTests(unittest.TestCase):
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

    def request(self, server, path, body=None, auth=True, origin=True):
        base = f'http://127.0.0.1:{server.server_port}'
        headers = {'Authorization': 'Bearer ' + server.access_token} if auth else {}
        if origin:
            headers['Origin'] = base
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_review_persists_and_report_distinguishes_confirmed_from_draft(self):
        task = self.service.create('登录问题', [self.session], DAY)
        server = make_server(self.db, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'task_id': task['id'], 'status': 'confirmed', 'outcome': '登录接口已修复',
                    'learnings': '先检查回调参数', 'next_steps': '观察线上日志',
                    'verification': '本地集成测试通过', 'commit_ids': []}
            self.assertEqual(self.request(server, '/api/task-review', body, auth=False)[0], 401)
            self.assertEqual(self.request(server, '/api/task-review', body, origin=False)[0], 403)
            self.assertEqual(self.request(server, '/api/task-review', body)[0], 200)
            listing = json.loads(self.request(server, '/api/tasks?date=' + DAY)[1])
            self.assertEqual(listing['tasks'][0]['review']['outcome'], '登录接口已修复')
            self.assertIn('登录接口已修复', self.request(server, '/api/report?date=' + DAY)[1])
            self.assertEqual(self.request(server, '/api/task-review', dict(body, status='invalid'))[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_week_and_month_report_include_confirmed_work(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.service.review(task['id'], 'confirmed', '修复登录回调', '掌握状态校验', '补监控', '测试通过', [])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['report', '--db', str(self.db), '--date', '2026-01-04',
                                   '--timezone', 'UTC', '--period', 'week']), 0)
        self.assertIn('修复登录回调', output.getvalue())
        self.assertIn('2025-12-29', output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['report', '--db', str(self.db), '--date', '2026-01-31',
                                   '--timezone', 'UTC', '--period', 'month']), 0)
        self.assertIn('修复登录回调', output.getvalue())

    def test_manual_work_without_ai_session_can_be_reviewed(self):
        task = self.service.create('线下联调', [], DAY)
        self.service.review(task['id'], 'confirmed', '完成接口联调', '掌握现场日志定位', '', '同事验收', [])
        result = report_data(self.db, DAY, 'UTC')
        self.assertEqual(result['task_reviews'][0]['outcome'], '完成接口联调')
        self.assertEqual(result['task_reviews'][0]['commits'], [])

    def test_review_rejects_commit_outside_task_day(self):
        task = self.service.create('登录问题', [self.session], DAY)
        old_id = 'a' * 40
        db = connect(self.db)
        with db:
            db.execute('INSERT INTO git_commits VALUES (?,?,?,?,?,?,?)',
                       (old_id, 'project', START - 86400, '旧提交', 1, 1, 0))
        db.close()
        with self.assertRaises(ValueError):
            self.service.review(task['id'], 'confirmed', '今日完成', '', '', '', [old_id])

    def test_purge_clears_review_links_and_old_git_evidence(self):
        task = self.service.create('登录问题', [self.session], DAY)
        commit_id = 'b' * 40
        db = connect(self.db)
        with db:
            db.execute('INSERT INTO git_commits VALUES (?,?,?,?,?,?,?)',
                       (commit_id, 'project', START + 3600, '测试提交', 1, 1, 0))
        db.close()
        self.service.review(task['id'], 'confirmed', '完成', '', '', '', [commit_id])
        self.service.close()
        with contextlib.redirect_stdout(io.StringIO()):
            main(['purge', '--db', str(self.db), '--summaries-only'])
        db = connect(self.db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reviews').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM task_git').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM git_commits').fetchone()[0], 1)
        db.close()
        with contextlib.redirect_stdout(io.StringIO()):
            main(['purge', '--db', str(self.db), '--before', '2026-01-03', '--timezone', 'UTC'])
        db = connect(self.db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM git_commits').fetchone()[0], 0)
        db.close()

    def test_git_commit_can_be_imported_once_and_linked_to_task(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'feature.txt').write_text('first\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'feature.txt'], check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test',
                   GIT_AUTHOR_DATE='2026-01-02T01:30:00+00:00',
                   GIT_COMMITTER_DATE='2026-01-02T01:30:00+00:00')
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', '修复登录回调'], check=True, env=env)
        with contextlib.redirect_stdout(io.StringIO()):
            main(['import', '--db', str(self.db), '--date', DAY, '--timezone', 'UTC', '--git-repo', str(repo)])
            main(['import', '--db', str(self.db), '--date', DAY, '--timezone', 'UTC', '--git-repo', str(repo)])
        db = connect(self.db)
        commits = [dict(row) for row in db.execute('SELECT * FROM git_commits')]
        db.close()
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]['subject'], '修复登录回调')
        task = self.service.create('登录问题', [self.session], DAY)
        self.service.review(task['id'], 'confirmed', '修复登录回调', '', '', '本地测试通过', [commits[0]['id']])
        data = report_data(self.db, DAY, 'UTC')
        self.assertEqual(data['task_reviews'][0]['commits'][0]['id'], commits[0]['id'])
        self.assertTrue(any(source.startswith('git:') for source in data['coverage']))

    def test_start_discovers_pi_directory_without_model_call(self):
        from worklog.cli import discover_sources
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.pi/agent/sessions').mkdir(parents=True)
            found = discover_sources(base)
            self.assertEqual(found['pi_dir'], str(base / '.pi/agent/sessions'))
            self.assertIsNone(found['codex_dir'])


if __name__ == '__main__':
    unittest.main()
