"""Synthetic-only tests for charts, naming consent and on-demand model setup."""
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_product_iteration as product_tests
from business_fixture import business_result

from worklog.adapters import event
from worklog.core import bounds, summarize
from worklog.presentation import topic_name
from worklog.store import connect, report_data, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'dashboard.sqlite3'
        self.key = salt(self.path)
        self.rows = [event(self.key, 'pi', 'fixture', 'project', 'user', START + 3600, 'user'),
                     event(self.key, 'pi', 'fixture', 'project', 'reply', START + 7200, 'assistant',
                           values={'tokens': 100})]
        save(self.path, self.rows, {'source': 'pi', 'selected': 1}, DAY, 'UTC')
        self.session = self.rows[0]['session']
        self.service = TaskService(self.path, None, {'pi': self.temp.name}, zone='UTC',
                                   clock=lambda: START + 10800)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_hourly_chart_counts_and_timezone(self):
        data = summarize(self.rows, DAY, 'UTC')
        self.assertEqual(len(data['hourly']), 24)
        self.assertEqual(data['hourly'][1]['events'], 1)
        self.assertEqual(data['hourly'][2]['requests'], 1)
        self.assertEqual(data['hourly'][2]['tokens'], 100)
        self.assertEqual(sum(row['events'] for row in data['hourly']), data['event_count'])
        local = summarize(self.rows, DAY, 'Asia/Shanghai')
        self.assertEqual(local['hourly'][9]['events'], 1)
        self.assertEqual(local['hourly'][10]['requests'], 1)
        empty = summarize([], DAY, 'UTC')
        self.assertEqual(sum(row['events'] for row in empty['hourly']), 0)

    def test_names_have_readable_fallback_and_manual_persistence(self):
        before = report_data(self.path, DAY, 'UTC')['sessions'][0]
        self.assertEqual(before['display_name'], 'Pi · 01:00 的协作')
        self.assertEqual(before['name_source'], 'metadata')
        self.service.rename_session(self.session, '登录回调问题排查')
        self.assertEqual(report_data(self.path, DAY, 'UTC')['sessions'][0]['display_name'], '登录回调问题排查')
        save(self.path, self.rows, {'source': 'pi', 'selected': 1}, DAY, 'UTC')
        self.assertEqual(report_data(self.path, DAY, 'UTC')['sessions'][0]['name_source'], 'manual')
        with self.assertRaises(ValueError):
            self.service.rename_session('a' * 16, '未知会话')
        for name in ['', 'x' * 81, None]:
            with self.assertRaises(ValueError):
                self.service.rename_session(self.session, name)

    def test_local_names_do_not_store_raw_body_or_overwrite_manual_name(self):
        extracted = dict(self.rows[0], title='请修复登录回调。之后检查测试')
        with patch('worklog.adapters.collect', return_value=([extracted], {'omitted_files': 1})) as collect:
            result = self.service.name_sessions(DAY)
            self.assertEqual(result['renamed'], 1)
            self.assertTrue(collect.call_args.kwargs['titles'])
            self.assertEqual(collect.call_args.kwargs['max_files'], 20)
            self.assertEqual(report_data(self.path, DAY, 'UTC')['sessions'][0]['display_name'], '修复登录回调')
            db = connect(self.path)
            self.assertEqual(db.execute("SELECT title FROM events WHERE kind='user'").fetchone()[0], '')
            db.close()
            self.service.rename_session(self.session, '人工主题')
            self.service.name_sessions(DAY)
            self.assertEqual(report_data(self.path, DAY, 'UTC')['sessions'][0]['display_name'], '人工主题')

    def test_linked_task_name_and_topic_redaction(self):
        self.service.create('认证问题定位', [self.session], DAY)
        session = report_data(self.path, DAY, 'UTC')['sessions'][0]
        self.assertEqual(session['display_name'], '认证问题定位')
        self.assertEqual(session['name_source'], 'task')
        self.assertEqual(topic_name(['<system>instructions', '请修复登录回调。补测试']), '修复登录回调')
        self.assertNotIn('password=abc', topic_name(['登录问题 password=abc']))
        self.assertLessEqual(len(topic_name(['主题' * 40])), 37)

    def test_model_configuration_is_explicit_and_does_not_call_model(self):
        runner = Mock(provider='synthetic', model='fixture')
        with patch('worklog.llm.PiRunner', return_value=runner) as factory:
            self.service.configure_summary(False)
            factory.assert_called_once_with()
            runner.assert_not_called()
            self.assertFalse(self.service.allow_excerpts)
            self.service.configure_summary(True)
            self.assertTrue(self.service.allow_excerpts)
            runner.assert_not_called()
        for value in [None, 'true', 1]:
            with self.assertRaises(ValueError):
                self.service.configure_summary(value)

    def test_enabling_model_after_start_runs_queue_and_renames_from_summary(self):
        class Runner:
            provider = 'synthetic'
            model = 'fixture'

            def __call__(self, data, mode, cancel):
                result = business_result(data, '登录回调排查与验证')
                result['work_items'][0]['title'] = '登录回调排查与验证'
                return result, {}

        self.assertTrue(self.service.thread.is_alive())
        with patch('worklog.llm.PiRunner', return_value=Runner()):
            self.service.configure_summary(True)
        task = self.service.create('登录问题', [self.session], DAY)
        with patch('worklog.evidence.excerpts', return_value={self.rows[0]['id']: '排查登录回调'}):
            self.service.request(task['id'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = self.service.list(DAY)['tasks']
            if tasks[0]['summary']['status'] == 'success':
                break
            time.sleep(0.05)
        self.assertEqual(tasks[0]['summary']['status'], 'success')
        self.assertEqual(tasks[0]['saved_summary']['overview']['text'], '登录回调排查与验证')
        session = report_data(self.path, DAY, 'UTC')['sessions'][0]
        self.assertEqual(session['display_name'], '登录回调排查与验证')
        self.assertEqual(session['name_source'], 'summary')

    def test_summary_backend_can_switch_explicitly_without_model_calls(self):
        runner = Mock(provider='codebuddy', model='deepseek-v4.1-flash')
        with patch('worklog.codebuddy.CodeBuddyRunner', return_value=runner):
            result = self.service.configure_summary(False, 'codebuddy')
        self.assertEqual(result['provider'], 'codebuddy')
        self.assertEqual(result['model'], 'deepseek-v4.1-flash')
        runner.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.configure_summary(False, 'untrusted-backend')

    def test_title_purge_keeps_only_manual_session_names(self):
        from worklog.cli import main
        db = connect(self.path)
        with db:
            db.executemany('INSERT INTO session_names VALUES (?,?,?)', [
                ('a' * 16, '本地主题', 'excerpt'), ('b' * 16, 'AI主题', 'summary'),
                ('c' * 16, '人工主题', 'manual')])
        db.close()
        self.service.close()
        with contextlib.redirect_stdout(io.StringIO()):
            main(['purge', '--db', str(self.path), '--titles-only'])
        db = connect(self.path)
        self.assertEqual([row['origin'] for row in db.execute('SELECT * FROM session_names')], ['manual'])
        db.close()

    def test_new_endpoints_require_auth_origin_and_explicit_consent(self):
        server = make_server(self.path, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = product_tests.ProductIterationTests.request
        try:
            body = {'session': self.session, 'name': '安全命名'}
            self.assertEqual(request(self, server, '/api/session-rename', body, auth=False)[0], 401)
            self.assertEqual(request(self, server, '/api/session-rename', body, origin=False)[0], 403)
            self.assertEqual(request(self, server, '/api/session-rename', body)[0], 200)
            self.assertEqual(request(self, server, '/api/session-names', {'date': DAY})[0], 400)
            self.assertEqual(request(self, server, '/api/session-names', {'date': DAY, 'consent': False})[0], 400)
            with patch('worklog.adapters.collect', return_value=([], {})):
                self.assertEqual(request(self, server, '/api/session-names', {'date': DAY, 'consent': True})[0], 200)
            self.assertEqual(request(self, server, '/api/summary-settings', {'allow_excerpts': 'yes'})[0], 400)
            with patch('worklog.llm.PiRunner', side_effect=ValueError('sensitive path')):
                status, text = request(self, server, '/api/summary-settings', {'allow_excerpts': False})
                self.assertEqual(status, 400)
                self.assertNotIn('sensitive path', text)
                self.assertIn('Pi CLI', json.loads(text)['hint'])
            status, text = request(self, server, '/api/day?date=' + DAY)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(text)['sessions'][0]['display_name'], '安全命名')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
