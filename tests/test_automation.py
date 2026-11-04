"""Automatic task capture, persistence, duration, classification and archiving; no network."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from worklog.adapters import collect
from worklog.core import bounds
from worklog.documents import report_document
from worklog.ledger import work_record_data
from worklog.llm import SummaryError
from worklog.store import connect, salt, save
from worklog.tasks import TaskService
from worklog.weekly import week_data

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


class AutomaticRunner:
    provider = 'fake'
    model = 'automatic-fixture'

    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, data, mode, cancel):
        self.calls.append(data)
        if self.fail:
            raise SummaryError('pi_failed')
        groups = []
        for session in dict.fromkeys(entry['session'] for entry in data['evidence']):
            entries = [entry for entry in data['evidence'] if entry['session'] == session]
            user = next(entry for entry in entries if entry['kind'] == 'user')
            answer = next(entry for entry in reversed(entries) if entry['kind'] == 'assistant')
            def item(text, source=answer):
                return {'text': text, 'evidence_ids': [source['id']]}
            design = '桌面' in user['text']
            failed = '失败' in answer['text']
            groups.append({'title': '桌面工作台方案评估' if design else '修复登录回调丢失参数',
                'problem': item(user['text'], user), 'actions': [item('讨论 Tauri 外壳与数据迁移方案。' if design else '助手报告保存并校验登录回调参数。')],
                'outcome': item('形成桌面方案，尚未实施。' if design else '助手报告回调参数丢失问题已修复。') if not failed else None,
                'verification': None if design else item('仍有2个回归用例失败。' if failed else '助手报告定向测试通过，生产环境未验证。'),
                'remaining': [item('仍需处理安装包。' if design else '仍需继续修复和回归。')] if design or failed else [],
                'status': 'discussed' if design else 'blocked' if failed else 'reported_resolved',
                'category': 'design' if design else 'bugfix'})
        return {'schema_version': 2, 'overview': groups[0]['problem'], 'work_items': groups}, {'tokens': 12}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'local.sqlite3'
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.key = salt(self.db)
        self.now = [START + 12 * 3600]
        self.runner = AutomaticRunner()
        self.sequence = 0
        self.append('login', 9 * 3600, 'user', '修复登录回调丢失参数导致登录失败的问题。')
        self.append('design', 9 * 3600, 'user', '只评估桌面工作台方案，不进行实现。')
        for session, text in [('login', '助手报告已保存回调参数，等待人工核验。'),
                              ('design', '讨论了桌面外壳方案，尚未制作安装包。')]:
            self.append(session, 9 * 3600 + 120, 'toolResult', 'PRIVATE_TOOL_OUTPUT')
            self.append(session, 9 * 3600 + 240, 'assistant', text)
        self.ingest()
        self.service = TaskService(self.db, self.runner, {'pi': self.logs}, zone='UTC', clock=lambda: self.now[0])

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def append(self, session, offset, role, text):
        path = self.logs / (session + '.jsonl')
        if not path.exists():
            path.write_text(json.dumps({'type': 'session', 'id': session, 'cwd': '/private/synthetic-project'}) + '\n')
        self.sequence += 1
        row = {'type': 'message', 'id': f'event-{self.sequence}',
               'timestamp': datetime.fromtimestamp(START + offset, timezone.utc).isoformat(),
               'message': {'role': role, 'content': text, 'stopReason': 'stop', 'provider': 'fake', 'model': 'fixture'}}
        with path.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    def ingest(self):
        rows, coverage = collect(self.logs, 'pi', self.key)
        save(self.db, rows, coverage, DAY, 'UTC')

    def wait(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.05)
        self.fail('Automatic workflow did not settle')

    def records(self):
        return work_record_data(self.db, DAY, 'UTC')

    def enable(self):
        self.service.configure_automation(True, 'today', True, 3)
        self.wait(lambda: self.records()['stats']['total'] == 2)

    def test_today_backfill_produces_tasks_results_time_categories_and_archive_without_manual_input(self):
        self.enable()
        result = self.records()
        self.assertEqual(len(self.runner.calls), 2)  # Each conversation is summarized independently.
        self.assertTrue(all(len({entry['session'] for entry in call['evidence']}) == 1 for call in self.runner.calls))
        self.assertEqual(result['stats']['total'], 2)
        self.assertEqual(result['stats']['archived'], 2)
        self.assertEqual({row['category'] for row in result['records']}, {'design', 'bugfix'})
        self.assertEqual(result['stats']['active_seconds'], 240)
        self.assertEqual(sum(row['active_seconds'] for row in result['records']), 240)
        self.assertTrue(all(row['active_seconds'] == 120 for row in result['records']))
        self.assertTrue(all(row['span_seconds'] == 240 for row in result['records']))
        self.assertTrue(all(row['work']['problem']['text'] and row['work']['outcome'] for row in result['records']))
        db = connect(self.db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reviews').fetchone()[0], 0)
        db.close()
        self.service.auto_tick()
        self.assertEqual(len(self.runner.calls), 2)
        self.assertNotIn('PRIVATE_TOOL_OUTPUT', json.dumps(self.runner.calls))
        document = report_document(self.db, DAY, 'UTC')
        self.assertIn('自动记录的任务', document['markdown'])
        self.assertIn('活跃时长估计', document['markdown'])
        self.assertIn('修复登录回调', document['markdown'])
        self.assertNotIn('工作内容待人工补充', document['markdown'])
        weekly = week_data(self.db, DAY, 'UTC')
        self.assertEqual(weekly['automatic_work']['stats']['total'], 2)
        self.assertEqual(weekly['totals']['tasks'], 2)

    def test_new_evidence_updates_same_record_reopens_blocked_work_and_preserves_category_correction(self):
        self.enable()
        login = next(row for row in self.records()['records'] if row['category'] == 'bugfix')
        self.service.revise_work_record(login['id'], category='docs')
        self.append('login', 10 * 3600, 'user', '继续处理回调问题，不能当成已完成。')
        self.append('login', 10 * 3600 + 60, 'assistant', '仍有2个回归用例失败，尚未解决。')
        self.ingest()
        self.service.auto_tick()
        self.wait(lambda: next(row for row in self.records()['records'] if row['id'] == login['id'])['status'] == 'blocked')
        result = self.records()
        self.assertEqual(result['stats']['total'], 2)
        updated = next(row for row in result['records'] if row['id'] == login['id'])
        self.assertFalse(updated['archived'])
        self.assertEqual(updated['category'], 'docs')
        self.assertEqual(result['stats']['active_seconds'], 300)  # The long waiting gap is not work time.
        self.assertGreater(updated['span_seconds'], updated['active_seconds'])
        filtered = work_record_data(self.db, DAY, 'UTC', category='docs', state='blocked')
        self.assertEqual(filtered['stats']['total'], 1)
        self.assertEqual(len(self.runner.calls), 3)

    def test_explicit_consent_required_and_now_scope_does_not_send_old_content(self):
        with self.assertRaises(ValueError):
            self.service.configure_automation(True, 'today', False)
        self.assertEqual(self.runner.calls, [])
        boundary = self.now[0]
        self.service.configure_automation(True, 'now', True)
        self.service.auto_tick()
        self.assertEqual(self.service.list(DAY)['tasks'], [])
        self.append('login', 12 * 3600 + 10, 'user', '修复新的登录回调问题。')
        self.append('login', 12 * 3600 + 20, 'assistant', '助手报告已保留参数并完成定向验证。')
        self.now[0] += 240
        self.ingest()
        self.service.auto_tick()
        self.wait(lambda: len(self.records()['records']) == 1)
        self.assertTrue(all(entry['ts'] >= boundary for call in self.runner.calls for entry in call['evidence']))

    def test_persisted_policy_restores_after_restart_without_repeating_calls_and_scope_change_revokes(self):
        self.enable()
        self.service.close()
        self.service = TaskService(self.db, self.runner, {'pi': self.logs}, zone='UTC', clock=lambda: self.now[0])
        self.assertTrue(self.service.auto)
        self.assertTrue(self.service.allow_excerpts)
        self.assertEqual(self.service.automation_since, START)
        self.service.auto_tick()
        self.assertEqual(len(self.runner.calls), 2)
        self.service.close()
        different = self.root / 'different'
        different.mkdir()
        self.service = TaskService(self.db, self.runner, {'pi': different}, zone='UTC', clock=lambda: self.now[0])
        self.assertFalse(self.service.auto)
        self.assertFalse(self.service.allow_excerpts)
        self.assertIn('来源', self.service.automation_status()['reason'])

    def test_failed_digest_not_retried_infinitely_and_pause_keeps_all_records(self):
        self.runner.fail = True
        self.service.configure_automation(True, 'today', True)
        self.wait(lambda: sum(issue['count'] for issue in self.service.automation_status()['issues']) == 2)
        self.assertEqual(len(self.runner.calls), 2)
        for _ in range(3):
            self.service.auto_tick()
        self.assertEqual(len(self.runner.calls), 2)
        self.service.configure_automation(False)
        self.assertFalse(self.service.auto)
        self.assertTrue(self.service.list(DAY)['tasks'])

    def test_legacy_daily_limit_does_not_block_new_stages(self):
        self.service.daily_limit = 1
        self.enable()
        self.append('login', 10 * 3600, 'user', '继续修复登录回调参数。')
        self.append('login', 10 * 3600 + 60, 'assistant', '助手报告已补充处理，等待核验。')
        self.ingest()
        self.service.auto_tick()
        self.wait(lambda: len(self.runner.calls) == 3)
        self.assertFalse(any(issue['code'] == 'daily_limit' for issue in self.service.automation_status()['issues']))

    def test_long_sources_are_processed_in_batches_without_sampling_away_middle_records(self):
        for index in range(12):
            self.append('login', 10 * 3600 + index * 2, 'user', f'继续处理第{index}轮登录回调参数校验。')
            self.append('login', 10 * 3600 + index * 2 + 1, 'assistant', f'助手报告第{index}轮保留参数并完成定向验证。')
        self.ingest()
        self.enable()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.service.automation_status()['pending_events']:
            self.service.auto_tick()
            time.sleep(.2)
        self.assertEqual(self.service.automation_status()['pending_events'], 0)
        fresh = [entry['id'] for call in self.runner.calls for entry in call['evidence'] if not entry.get('context_only')]
        self.assertEqual(len(fresh), 28)
        self.assertEqual(len(set(fresh)), 28)
        self.assertEqual(self.records()['stats']['total'], 2)
        self.assertGreater(self.records()['stats']['active_seconds'], 240)
        login = next(row for row in self.records()['records'] if row['category'] == 'bugfix')
        self.assertEqual(login['first_ts'], START + 9 * 3600)
        self.assertGreater(login['updates'], 1)

    def test_explicit_retry_recovers_failed_automation_without_manual_forms(self):
        self.runner.fail = True
        self.service.configure_automation(True, 'today', True)
        self.wait(lambda: sum(issue['count'] for issue in self.service.automation_status()['issues']) == 2)
        self.runner.fail = False
        self.service.retry_automation()
        self.wait(lambda: self.records()['stats']['total'] == 2)
        self.assertEqual(len(self.runner.calls), 4)
        self.assertFalse(self.service.automation_status()['issues'])

    def test_closed_manual_source_is_backfilled_without_changing_its_human_scope(self):
        db = connect(self.db)
        sessions = [row[0] for row in db.execute('SELECT DISTINCT session FROM events')]
        db.close()
        task = self.service.create('旧的已结束记录', sessions, DAY)
        db = connect(self.db)
        with db:
            db.execute("UPDATE tasks SET status='completed',end_ts=? WHERE id=?", (START + 11 * 3600, task['id']))
        db.close()
        self.enable()
        self.assertEqual(self.service._task(task['id'])['status'], 'completed')
        self.assertEqual(self.service._task(task['id'])['end_ts'], START + 11 * 3600)
        self.assertEqual(len(self.runner.calls), 2)

    def test_pause_cancels_unexecuted_jobs_and_resume_reclaims_their_quota(self):
        self.service.daily_limit = 1
        with self.service.guard:
            self.service.configure_automation(True, 'today', True)
            self.service.auto_tick()
            self.service.configure_automation(False)
            self.assertEqual(len(self.runner.calls), 0)
            db = connect(self.db)
            self.assertEqual(db.execute('SELECT status FROM summaries').fetchone()[0], 'interrupted')
            db.close()
        self.service.configure_automation(True, 'today', True)
        self.wait(lambda: self.records()['stats']['total'] == 2)
        self.assertEqual(len(self.runner.calls), 2)

    def test_projection_is_atomic_and_reports_failure_instead_of_false_success(self):
        with patch('worklog.ledger.project_summary', side_effect=ValueError('synthetic')):
            self.service.configure_automation(True, 'today', True)
            self.wait(lambda: any(issue['code'] == 'projection_failed' for issue in self.service.automation_status()['issues']))
        self.assertEqual(self.records()['stats']['total'], 0)
        db = connect(self.db)
        self.assertEqual(db.execute('SELECT status FROM summaries').fetchone()[0], 'error')
        db.close()


if __name__ == '__main__':
    unittest.main()
