import json
import unittest
from datetime import date, timedelta

import test_automation as fixtures

from worklog.adapters import collect
from worklog.conversations import progress_data, tree_data
from worklog.projects import project_metadata
from worklog.store import connect, salt, save
from worklog.tasks import TaskService

NEXT_DAY = (date.fromisoformat(fixtures.DAY) + timedelta(days=1)).isoformat()


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutomationTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def tree(self, day=fixtures.DAY):
        return tree_data(self.fixture.db, day, 'UTC')

    def test_git_subdirectories_and_worktrees_share_one_project_but_other_repositories_do_not(self):
        root = self.fixture.root
        repo = root / 'main'
        (repo / '.git' / 'worktrees' / 'topic').mkdir(parents=True)
        (repo / 'src').mkdir()
        worktree = root / 'branch'
        worktree.mkdir()
        (worktree / '.git').write_text('gitdir: ' + str(repo / '.git/worktrees/topic'))
        (repo / '.git/worktrees/topic/commondir').write_text('../..')
        other = root / 'other'
        (other / '.git').mkdir(parents=True)
        key = salt(self.fixture.db)
        one = project_metadata(key, str(repo))
        self.assertEqual(one['project'], project_metadata(key, str(repo / 'src'))['project'])
        self.assertEqual(one['project'], project_metadata(key, str(worktree))['project'])
        self.assertNotEqual(one['project'], project_metadata(key, str(other))['project'])
        self.assertEqual(one['project_name'], 'main')

    def test_tree_groups_conversations_without_combining_their_model_evidence(self):
        self.fixture.enable()
        value = self.tree()
        self.assertEqual(value['project_count'], 1)
        self.assertEqual(value['conversation_count'], 2)
        self.assertEqual(value['part_count'], 2)
        self.assertEqual(len(self.fixture.runner.calls), 2)
        for call in self.fixture.runner.calls:
            self.assertEqual(len({entry['session'] for entry in call['evidence']}), 1)
        self.assertEqual(sum(item['active_seconds'] for item in value['projects'][0]['conversations']), 240)

    def test_cross_day_progress_updates_one_cumulative_part_and_keeps_daily_changes_separate(self):
        self.fixture.enable()
        self.fixture.now[0] += 86400
        self.fixture.append('login', 86400 + 9 * 3600, 'user', '继续处理登录回调，不能当成已经完成。')
        self.fixture.append('login', 86400 + 9 * 3600 + 60, 'assistant', '仍有2个回归用例失败，尚未解决。')
        events, coverage = collect(self.fixture.logs, 'pi', salt(self.fixture.db))
        save(self.fixture.db, events, coverage, NEXT_DAY, 'UTC')
        self.fixture.service.auto_tick()
        self.fixture.wait(lambda: self.tree(NEXT_DAY)['part_count'] == 2 and any(
            part['status'] == 'blocked' for project in self.tree(NEXT_DAY)['projects']
            for conversation in project['conversations'] for part in conversation['parts']))
        value = self.tree(NEXT_DAY)
        self.assertEqual(value['conversation_count'], 2)
        self.assertEqual(value['part_count'], 2)
        login = next(conversation for conversation in value['projects'][0]['conversations'] if conversation['parts'][0]['status'] == 'blocked')
        self.assertEqual(login['parts'][0]['updates'], 2)
        self.assertTrue(login['changed_today'])
        self.assertTrue(self.fixture.runner.calls[-1]['conversation_context'])
        self.assertEqual({row['day'] for row in progress_data(self.fixture.db, login['session'])['progress']}, {fixtures.DAY, NEXT_DAY})
        self.assertEqual(self.fixture.records()['stats']['archived'], 2)  # Yesterday is not rewritten by today's failure.

    def test_explicit_continuation_keeps_identity_when_a_part_is_renamed(self):
        self.fixture.enable()
        original = self.fixture.runner
        old = next(part for project in self.tree()['projects'] for conversation in project['conversations']
                   for part in conversation['parts'] if part['category'] == 'bugfix')

        class ContinuedRunner:
            provider, model = original.provider, original.model

            def __call__(self, data, mode, stop=None):
                result, usage = original(data, mode, stop)
                result['work_items'][0]['title'] = '认证链路边界条件校验'
                result['work_items'][0]['continuation_id'] = old['id']
                return result, usage

        self.fixture.service.runner = ContinuedRunner()
        self.fixture.append('login', 10 * 3600, 'user', '继续处理登录回调的边界条件。')
        self.fixture.append('login', 10 * 3600 + 60, 'assistant', '助手报告已补充边界校验并完成定向验证。')
        self.fixture.ingest()
        self.fixture.service.auto_tick()
        self.fixture.wait(lambda: any(part['title'] == '认证链路边界条件校验' for project in self.tree()['projects']
                                     for conversation in project['conversations'] for part in conversation['parts']))
        parts = [part for project in self.tree()['projects'] for conversation in project['conversations'] for part in conversation['parts']]
        self.assertEqual(len(parts), 2)
        self.assertEqual(next(part for part in parts if part['title'] == '认证链路边界条件校验')['id'], old['id'])
        self.assertEqual(self.fixture.records()['stats']['total'], 2)

    def test_mixed_legacy_source_is_split_locally_without_resending_covered_messages(self):
        db = connect(self.fixture.db)
        sessions = [row[0] for row in db.execute('SELECT DISTINCT session FROM events')]
        db.close()
        task = self.fixture.service.create('旧版项目合并来源', sessions, fixtures.DAY)
        self.fixture.service.configure_summary(True)
        self.fixture.service.request(task['id'])
        self.fixture.wait(lambda: self.fixture.service.list(fixtures.DAY)['tasks'][0]['summary']['status'] == 'success')
        db = connect(self.fixture.db)
        with db:
            db.execute('INSERT INTO task_automation VALUES (?,?,1,NULL,NULL,?)', (task['id'], 'old-project', self.fixture.now[0]))
        db.close()
        self.fixture.service.configure_automation(True, 'today', True)
        self.fixture.service.auto_tick()
        self.assertEqual(len(self.fixture.runner.calls), 1)
        self.assertEqual(self.tree()['part_count'], 2)
        self.assertEqual(self.fixture.service.automation_status()['pending_events'], 0)
        db = connect(self.fixture.db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_sources').fetchone()[0], 2)
        db.close()

    def test_older_backfill_adds_history_without_regressing_newer_results(self):
        self.fixture.enable()
        db = connect(self.fixture.db)
        saved = next(row for row in db.execute("SELECT * FROM summaries WHERE status='success'")
                     if json.loads(row['output_json'])['work_items'][0]['category'] == 'bugfix')
        data, result, task_id = json.loads(saved['input_json']), json.loads(saved['output_json']), saved['task_id']
        db.close()
        self.fixture.append('login', 10 * 3600, 'user', '继续处理登录回调，不能视为完成。')
        self.fixture.append('login', 10 * 3600 + 60, 'assistant', '仍有2个回归用例失败，尚未解决。')
        self.fixture.ingest()
        self.fixture.service.auto_tick()
        self.fixture.wait(lambda: self.fixture.records()['stats']['blocked'] == 1)
        from worklog.ledger import project_summary
        db = connect(self.fixture.db)
        with db:
            project_summary(db, self.fixture.service._task(task_id), 'c' * 32, data, result, self.fixture.now[0])
        db.close()
        parts = [part for project in self.tree()['projects'] for conversation in project['conversations'] for part in conversation['parts']]
        self.assertEqual(len(parts), 2)
        self.assertEqual(next(part for part in parts if part['category'] == 'bugfix')['status'], 'blocked')
        self.assertEqual(self.fixture.records()['stats']['blocked'], 1)

    def test_restart_resumes_shutdown_interruption_without_manual_retry(self):
        self.fixture.enable()
        self.fixture.service.close()
        db = connect(self.fixture.db)
        task_id = db.execute('SELECT task_id FROM conversation_sources LIMIT 1').fetchone()[0]
        with db:
            db.execute("UPDATE summaries SET status='error',error='cancelled',output_json=NULL WHERE task_id=?", (task_id,))
            db.execute('DELETE FROM automatic_coverage WHERE task_id=?', (task_id,))
            db.execute("UPDATE task_automation SET last_error='cancelled' WHERE task_id=?", (task_id,))
        db.close()
        self.fixture.service = TaskService(self.fixture.db, self.fixture.runner, {'pi': self.fixture.logs}, zone='UTC', clock=lambda: self.fixture.now[0])
        self.fixture.service.auto_tick()
        self.fixture.wait(lambda: len(self.fixture.runner.calls) == 3 and not self.fixture.service.automation_status()['issues'])

    def test_resuming_with_now_scope_does_not_reuse_an_older_daily_source(self):
        self.fixture.enable()
        self.fixture.service.configure_automation(False)
        boundary = self.fixture.now[0]
        self.fixture.service.configure_automation(True, 'now', True)
        self.fixture.append('login', 12 * 3600 + 10, 'user', '修复新的登录回调边界问题。')
        self.fixture.append('login', 12 * 3600 + 20, 'assistant', '助手报告补充了校验并完成定向验证。')
        self.fixture.now[0] += 240
        self.fixture.ingest()
        self.fixture.service.auto_tick()
        self.fixture.wait(lambda: len(self.fixture.runner.calls) == 3)
        self.assertTrue(all(entry['ts'] >= boundary for entry in self.fixture.runner.calls[-1]['evidence']))
        self.assertFalse(self.fixture.runner.calls[-1]['conversation_context'])

    def test_legacy_mixed_prose_is_preserved_but_rechecked_per_conversation_once(self):
        original = self.fixture.runner

        class MixedRunner:
            provider, model = original.provider, original.model

            def __call__(self, data, mode, stop=None):
                result, usage = original(data, mode, stop)
                if len({entry['session'] for entry in data['evidence']}) > 1:
                    group = result['work_items'][0]
                    own = next(entry['session'] for entry in data['evidence'] if entry['id'] == group['problem']['evidence_ids'][0])
                    group['problem']['evidence_ids'].append(next(entry['id'] for entry in data['evidence'] if entry['session'] != own))
                return result, usage

        self.fixture.service.runner = MixedRunner()
        self.fixture.service.configure_summary(True)
        db = connect(self.fixture.db)
        sessions = [row[0] for row in db.execute('SELECT DISTINCT session FROM events')]
        db.close()
        task = self.fixture.service.create('旧合并总结', sessions, fixtures.DAY)
        self.fixture.service.request(task['id'])
        self.fixture.wait(lambda: len(original.calls) == 1 and self.fixture.service.list(fixtures.DAY)['tasks'][0]['summary']['status'] == 'success')
        from worklog.conversation_upgrade import upgrade
        db = connect(self.fixture.db)
        with db:
            db.execute('DELETE FROM local_migrations')
            upgrade(db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM legacy_shared_records').fetchone()[0], 1)
        db.close()
        self.fixture.enable()
        self.fixture.wait(lambda: self.fixture.service.automation_status()['pending_events'] == 0)
        self.assertEqual(len(original.calls), 3)
        self.assertEqual(self.tree()['part_count'], 2)
        self.assertEqual(self.fixture.records()['stats']['active_seconds'], 240)
        self.assertTrue(all(len({entry['session'] for entry in data['evidence']}) == 1 for data in original.calls[1:]))
        self.fixture.service.auto_tick()
        self.assertEqual(len(original.calls), 3)

    def test_legacy_quota_migrates_to_unlimited_without_repeating_completed_evidence(self):
        self.fixture.enable()
        self.fixture.service.close()
        db = connect(self.fixture.db)
        state = json.loads(db.execute('SELECT settings_json FROM automation_settings').fetchone()[0])
        state['daily_limit'] = 1
        state.pop('summary_unit', None)
        with db:
            db.execute('UPDATE automation_settings SET settings_json=?', (json.dumps(state),))
            db.execute("UPDATE task_automation SET last_error='daily_limit'")
        db.close()
        self.fixture.service = TaskService(self.fixture.db, self.fixture.runner, {'pi': self.fixture.logs}, zone='UTC', clock=lambda: self.fixture.now[0])
        self.assertIsNone(self.fixture.service.daily_limit)
        self.fixture.service.auto_tick()
        self.assertEqual(len(self.fixture.runner.calls), 2)
        self.assertFalse(self.fixture.service.automation_status()['issues'])
        self.assertEqual(self.tree()['part_count'], 2)


if __name__ == '__main__':
    unittest.main()
