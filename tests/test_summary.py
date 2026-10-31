import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from business_fixture import business_result

from worklog.adapters import collect, event
from worklog.cli import main
from worklog.core import bounds
from worklog.evidence import redact, snapshot
from worklog.llm import PiRunner, SummaryError, run_bounded, validate_result
from worklog.store import connect, markdown, report_data, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class FakeRunner:
    provider = 'fake'
    model = 'fake-model'

    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, data, mode, cancel):
        self.calls.append((data, mode))
        if self.fail:
            raise SummaryError('pi_failed')
        return business_result(data), {'tokens': 12}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'local.sqlite3'
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.key = salt(self.db)
        for i in range(2):
            self.write_session(i)
        self.ingest()
        self.now = [START + 10 * 3600]
        self.runner = FakeRunner()
        self.service = TaskService(self.db, self.runner, {'pi': self.logs}, True, zone='UTC', clock=lambda: self.now[0])
        db = connect(self.db)
        self.sessions = [r[0] for r in db.execute('SELECT DISTINCT session FROM events')]
        db.close()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write_session(self, index, seconds=9 * 3600, last='stop'):
        def stamp(offset):
            from datetime import datetime, timezone
            return datetime.fromtimestamp(START + seconds + offset, timezone.utc).isoformat()
        values = [
            {'type': 'session', 'id': 's-' + str(index), 'cwd': '/private/customer'},
            {'type': 'message', 'id': 'user-' + str(seconds), 'timestamp': stamp(0), 'message': {
                'role': 'user', 'content': '检查认证配置 token=FAKE_CREDENTIAL_VALUE'}},
            {'type': 'message', 'id': 'tool-' + str(seconds), 'timestamp': stamp(5), 'message': {
                'role': 'toolResult', 'content': 'TOOL_OUTPUT_MUST_NOT_LEAVE', 'isError': False}},
            {'type': 'message', 'id': 'assistant-' + str(seconds), 'timestamp': stamp(10), 'message': {
                'role': 'assistant', 'provider': 'demo', 'model': 'demo', 'stopReason': last,
                'content': [{'type': 'thinking', 'thinking': 'THINKING_MUST_NOT_LEAVE'},
                            {'type': 'text', 'text': '已调整配置，仍需验证。'}], 'usage': {'input': 10, 'output': 3}}}]
        with (self.logs / (str(index) + '.jsonl')).open('a') as f:
            f.write(''.join(json.dumps(v) + '\n' for v in values))

    def ingest(self):
        rows, stats = collect(self.logs, 'pi', self.key)
        save(self.db, rows, stats, DAY, 'UTC')

    def create(self):
        return self.service.create('认证配置排查', self.sessions, DAY)

    def wait(self, job_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            db = connect(self.db)
            row = db.execute('SELECT * FROM summaries WHERE id=?', (job_id,)).fetchone()
            db.close()
            if row and row['status'] in ['success', 'error', 'interrupted']:
                return dict(row)
            time.sleep(0.05)
        self.fail('Summary worker did not settle')

    def test_cross_session_preview_is_scoped_redacted_and_not_sent(self):
        task = self.create()
        preview = self.service.preview(task['id'])
        self.assertEqual(len({e['session'] for e in preview['evidence']}), 2)
        text = json.dumps(preview, ensure_ascii=False)
        for private in ['FAKE_CREDENTIAL_VALUE', 'TOOL_OUTPUT_MUST_NOT_LEAVE', 'THINKING_MUST_NOT_LEAVE', '/private/customer']:
            self.assertNotIn(private, text)
        self.assertEqual(self.runner.calls, [])

    def test_stage_summary_does_not_complete_task_and_is_deduplicated(self):
        task = self.create()
        job = self.service.request(task['id'])
        self.assertEqual(self.wait(job['id'])['status'], 'success')
        again = self.service.request(task['id'])
        self.assertEqual(again['id'], job['id'])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.service._task(task['id'])['status'], 'open')

    def test_manual_final_freezes_scope_and_exports_draft(self):
        task = self.create()
        job = self.service.request(task['id'], 'final')
        self.assertEqual(self.wait(job['id'])['status'], 'success')
        self.assertEqual(self.service._task(task['id'])['status'], 'completed')
        frozen = self.service.preview(task['id'], job['id'])
        self.write_session(0, seconds=11 * 3600)
        self.ingest()
        self.now[0] = START + 12 * 3600
        self.assertEqual(self.service.preview(task['id'], job['id']), frozen)
        self.assertEqual(self.service.request(task['id'], 'final')['id'], job['id'])
        report = markdown(report_data(self.db, DAY, 'UTC'))
        self.assertIn('LLM任务总结', report)
        self.assertIn('待人工审核', report)

    def test_failed_calls_are_not_automatically_retried(self):
        self.runner.fail = True
        task = self.create()
        first = self.service.request(task['id'])
        self.assertEqual(self.wait(first['id'])['status'], 'error')
        self.service.request(task['id'])
        self.assertEqual(len(self.runner.calls), 1)
        self.runner.fail = False
        retried = self.service.request(task['id'], retry=True)
        again = self.service.request(task['id'], retry=True)
        self.assertEqual(retried['id'], again['id'])
        self.assertEqual(self.wait(retried['id'])['status'], 'success')
        self.assertEqual(len(self.runner.calls), 2)

    def test_daily_budget_and_worker_ownership(self):
        self.service.daily_limit = 1
        task = self.create()
        job = self.service.request(task['id'])
        self.wait(job['id'])
        with self.assertRaises(ValueError):
            self.service.request(task['id'], 'final')
        with self.assertRaises(ValueError):
            TaskService(self.db, FakeRunner())
        with self.assertRaises(ValueError):
            main(['purge', '--db', str(self.db), '--summaries-only'])

    def test_old_queued_jobs_cannot_bypass_todays_execution_budget(self):
        task = self.create()
        with self.service.guard:
            first = self.service.request(task['id'], 'stage')
            second = self.service.request(task['id'], 'final')
            db = connect(self.db)
            with db:
                db.execute('UPDATE summaries SET created_at=?', (START - 86400,))
            db.close()
            self.service.daily_limit = 1
            self.service._one_job()
            self.service._one_job()
            self.assertEqual(len(self.runner.calls), 1)
            db = connect(self.db)
            self.assertEqual(db.execute('SELECT status FROM summaries WHERE id=?', (second['id'],)).fetchone()[0], 'queued')
            db.close()
        self.assertEqual(self.wait(first['id'])['status'], 'success')
        self.assertEqual(self.service.list(DAY)['runs_today'], 1)

    def test_invalid_sessions_and_start_scope_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.create('任务', ['f' * 16], DAY)
        with self.assertRaises(ValueError):
            self.service.create('任务', self.sessions, DAY, '99:99')
        with self.assertRaises(ValueError):
            self.service.create('任务', self.sessions, DAY, '23:59')

    def test_auto_only_new_idle_terminal_activity_and_never_completes_task(self):
        self.service.auto = True
        self.service.auto_tick()
        self.assertEqual(self.service.list(DAY)['tasks'], [])  # no retroactive upload
        self.now[0] += 60
        self.write_session(0, seconds=10 * 3600 + 30, last='toolUse')
        self.ingest()
        self.service.auto_tick()
        self.now[0] += 700
        self.service.auto_tick()
        self.assertEqual(self.runner.calls, [])  # pending tool isn't a finished turn
        self.write_session(0, seconds=10 * 3600 + 100)
        self.ingest()
        self.service.auto_tick()
        tasks = self.service.list(DAY)['tasks']
        job_id = tasks[0]['summary']['id']
        self.assertEqual(self.wait(job_id)['status'], 'success')
        self.service.auto_tick()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.service.list(DAY)['tasks'][0]['status'], 'open')

    def test_task_http_end_to_end_requires_auth_origin_and_fixed_schema(self):
        server = make_server(self.db, DAY, 'UTC', 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(path, body=None, origin=None, auth=True):
            headers = {'Authorization': 'Bearer ' + server.access_token} if auth else {}
            if origin:
                headers['Origin'] = origin
            if body is not None:
                headers['Content-Type'] = 'application/json'
            request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with opener.open(request, timeout=5) as response:
                    raw = response.read().decode()
                    return response.status, json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw
            except urllib.error.HTTPError as error:
                return error.code, None
        try:
            body = {'title': '跨会话总结', 'sessions': self.sessions, 'date': DAY}
            self.assertEqual(call('/api/tasks', body, base, auth=False)[0], 401)
            self.assertEqual(call('/api/tasks', body)[0], 403)
            self.assertEqual(call('/api/tasks', body, 'https://untrusted.invalid')[0], 403)
            self.assertEqual(call('/api/tasks', dict(body, source='/private'), base)[0], 400)
            status, task = call('/api/tasks', body, base)
            self.assertEqual(status, 201)
            self.assertEqual(call('/api/task-preview?id=' + task['id'])[0], 200)
            self.assertEqual(self.runner.calls, [])
            status, job = call('/api/summarize', {'task_id': task['id'], 'mode': 'final'}, base)
            self.assertEqual(status, 202)
            self.assertEqual(self.wait(job['id'])['status'], 'success')
            self.assertEqual(call('/api/tasks?date=' + DAY)[1]['tasks'][0]['status'], 'completed')
            self.assertIn('LLM任务总结', call('/api/report?date=' + DAY)[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_restart_does_not_send_old_excerpts_under_changed_consent(self):
        task = self.create()
        self.service.close()
        queued = self.service.request(task['id'])
        self.service = TaskService(self.db, self.runner, allow_excerpts=False, zone='UTC', clock=lambda: self.now[0])
        self.assertEqual(self.wait(queued['id'])['error'], 'configuration_changed')
        self.assertEqual(self.runner.calls, [])

    def test_restart_with_different_source_scope_blocks_old_snapshot(self):
        task = self.create()
        self.service.close()
        queued = self.service.request(task['id'])
        other = self.root / 'other'
        other.mkdir()
        self.service = TaskService(self.db, self.runner, {'pi': other}, True, zone='UTC', clock=lambda: self.now[0])
        self.assertEqual(self.wait(queued['id'])['error'], 'configuration_changed')
        self.assertEqual(self.runner.calls, [])

    def test_changing_model_does_not_silently_reuse_other_model_summary(self):
        task = self.create()
        old = self.service.request(task['id'])
        self.wait(old['id'])
        self.runner.model = 'new-model'
        new = self.service.request(task['id'])
        self.assertNotEqual(old['id'], new['id'])
        self.wait(new['id'])


class LlmTests(unittest.TestCase):
    def test_schema_citations_and_output_redaction(self):
        item = {'text': 'password=FAKE_VALUE 已检查', 'evidence_ids': ['e_1']}
        value = {'overview': item, 'work': [item], 'results': [], 'blockers': [], 'next_steps': []}
        result = validate_result(json.dumps(value), {'e_1'})
        self.assertNotIn('FAKE_VALUE', json.dumps(result))
        with self.assertRaises(SummaryError):
            validate_result(json.dumps(value), {'different'})
        with self.assertRaises(SummaryError):
            validate_result('not json', {'e_1'})

    def test_code_fences_private_keys_and_long_messages_are_not_forwarded(self):
        self.assertNotIn('print', redact('结论\n```python\nprint("secret")\n```'))
        self.assertEqual(redact('x' * 5000), '[长消息已省略]')
        self.assertNotIn('FAKE_VALUE', redact('"api_key": "FAKE_VALUE"'))

    def test_bounded_sampling_keeps_every_linked_session(self):
        key = b'x' * 32
        rows = [event(key, 'pi', str(group), 'demo', f'{group}:{i}', START + group * 100 + i,
                      'user' if i % 2 == 0 else 'assistant') for group in range(12) for i in range(20)]
        data = snapshot({'title': '虚构多会话任务'}, rows, {}, key, False)
        self.assertEqual(len(data['evidence']), 24)
        self.assertEqual(data['coverage']['selected_sessions'], 12)
        self.assertTrue(data['coverage']['sampled'])
        for session in {r['session'] for r in rows}:
            self.assertGreaterEqual(sum(e['session'] == session for e in data['evidence']), 2)

    def test_pi_command_disables_tools_resources_and_session_persistence(self):
        with patch('worklog.llm.shutil.which', return_value='/safe/pi'), patch('worklog.llm.default_model', return_value=('native', 'model')):
            runner = PiRunner()
        command = runner.command()
        for flag in ['--no-tools', '--no-mcp', '--no-extensions', '--no-session', '--no-context-files', '--no-skills', '--no-approve']:
            self.assertIn(flag, command)
        self.assertNotIn('--api-key', command)

    def test_runner_uses_authoritative_message_end_and_settled(self):
        item = {'text': '记录了活动', 'evidence_ids': ['e_1']}
        result = {'overview': item, 'work': [], 'results': [], 'blockers': [], 'next_steps': []}
        wire = '\n'.join(json.dumps(e) for e in [
            {'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'not final'}},
            {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop', 'content': [
                {'type': 'text', 'text': json.dumps(result)}], 'usage': {'input': 10, 'output': 5}}},
            {'type': 'agent_settled', 'aborted': False}])
        with patch('worklog.llm.shutil.which', return_value='/safe/pi'), patch('worklog.llm.default_model', return_value=('native', 'model')):
            runner = PiRunner()
        with patch('worklog.llm.run_bounded', return_value=wire):
            answer, tokens = runner({'evidence': [{'id': 'e_1'}]}, 'stage', threading.Event())
            self.assertEqual(answer['overview']['text'], '记录了活动')
            self.assertEqual(tokens['tokens'], 15)
        with patch('worklog.llm.run_bounded', return_value='{"type":"tool_execution_start"}\n'), self.assertRaises(SummaryError):
            runner({'evidence': [{'id': 'e_1'}]}, 'stage', threading.Event())

    def test_subprocess_is_bounded_and_cancelled_without_leaking_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'fake.py'
            script.write_text('import time\ntime.sleep(10)\n')
            with self.assertRaisesRegex(SummaryError, 'timeout'):
                run_bounded([sys.executable, str(script)], b'', directory, threading.Event(), timeout=0.1)
            script.write_text('import sys\nsys.stdout.write("x" * 1100000)\n')
            with self.assertRaisesRegex(SummaryError, 'output_limit'):
                run_bounded([sys.executable, str(script)], b'', directory, threading.Event(), timeout=3)


if __name__ == '__main__':
    unittest.main()
