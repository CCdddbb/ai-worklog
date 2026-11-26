"""WorkBuddy (YDKJ) source tests. Synthetic JSONL only; never reads real App data."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from worklog.adapters import collect
from worklog.automation import configure
from worklog.cli import discover_sources, main, serve
from worklog.conversations import tree_data
from worklog.core import bounds
from worklog.evidence import excerpts, source_scope
from worklog.review import ReviewError, review
from worklog.store import connect, salt, save
from worklog.tasks import TaskService

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')
KEY = b'w' * 32
SECRET = 'PRIVATE_CANARY_TOKEN_0123456789'
SESSION = 'wb-session-1'
USER_TEXT = '修复登录回调参数丢失导致登录失败的问题。'
ASSISTANT_TEXT = '助手报告已保存回调参数，等待人工核验。'

USAGE = {'inputTokens': 1000, 'outputTokens': 50,
         'inputTokensDetails': [{'cached_tokens': 300}],
         'rawUsage': {'prompt_cache_write_tokens': 100}}


def stamp(offset):
    """WorkBuddy timestamps are millisecond epochs."""
    return int((START + offset) * 1000)


def base(ident, offset, **kw):
    row = {'timestamp': stamp(offset), 'sessionId': SESSION,
           'cwd': '/private/workbuddy-project', 'id': ident}
    row.update(kw)
    return row


def message(role, text, ident, offset, status='completed', usage=None):
    provider = {'model': 'hy4-free'}
    if usage:
        provider['usage'] = usage
    return base(ident, offset, type='message', role=role, status=status,
                content=[{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
                providerData=provider)


def sample_lines():
    # The same id is intentionally reused across a message and a function_call:
    # only the line number keeps their event ids distinct.
    return [
        base('meta', 0, type='session-meta'),
        message('user', USER_TEXT, 'shared', 60),
        message('assistant', ASSISTANT_TEXT, 'shared', 120),
        base('title', 130, type='ai-title', aiTitle='修复登录问题'),
        base('shared', 140, type='function_call', providerData={'model': 'hy4-free', 'usage': USAGE},
             arguments={'text': SECRET}),
        base('result', 150, type='function_call_result', status='cancelled'),
        base('think', 160, type='reasoning', text=SECRET),
        base('snapshot', 170, type='file-history-snapshot', rawContent=SECRET),
    ]


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root / 'projects'
        self.logs.mkdir()
        self.path = self.logs / 'session.jsonl'
        self.db = self.root / 'local.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        return self.logs

    def test_events_usage_status_and_unique_ids(self):
        self.write(sample_lines())
        events, stats = collect(self.logs, 'workbuddy', KEY)
        self.assertEqual(stats['source'], 'workbuddy')
        self.assertEqual([e['kind'] for e in events],
                         ['user', 'assistant', 'auxiliary', 'tool', 'usage', 'tool'])
        self.assertEqual(len({e['id'] for e in events}), len(events))
        self.assertEqual(events[0]['ts'], START + 60)
        self.assertEqual(events[0]['source'], 'workbuddy')
        self.assertEqual(events[0]['provider'], 'workbuddy')
        self.assertEqual(events[0]['model'], 'hy4-free')
        self.assertEqual(events[1]['status'], 'stop')
        self.assertEqual(events[5]['status'], 'aborted')
        usage = events[4]
        self.assertEqual((usage['input'], usage['cache_read'], usage['cache_write'],
                          usage['output'], usage['tokens']), (600, 300, 100, 50, 1050))
        self.assertTrue(all(e['file_id'] for e in events))
        save(self.db, events, stats, DAY, 'UTC')
        raw = self.db.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(USER_TEXT.encode(), raw)

    def test_titles_are_opt_in_and_redacted(self):
        self.write(sample_lines())
        plain, _ = collect(self.logs, 'workbuddy', KEY)
        titled, _ = collect(self.logs, 'workbuddy', KEY, titles=True)
        self.assertTrue(all(not e['title'] for e in plain))
        self.assertTrue(any(e['title'] for e in titled))
        self.assertTrue(all(SECRET not in e['title'] for e in titled))
        self.assertTrue(all(len(e['title']) <= 100 for e in titled))

    def test_excerpts_only_cover_user_and_assistant_messages(self):
        self.write(sample_lines())
        events, _ = collect(self.logs, 'workbuddy', KEY)
        dialogue = [e for e in events if e['kind'] in ('user', 'assistant')]
        found = excerpts(dialogue, {'workbuddy': str(self.logs)}, KEY)
        self.assertEqual(set(found), {e['id'] for e in dialogue})
        self.assertIn(USER_TEXT, ''.join(found.values()))
        self.assertIn(ASSISTANT_TEXT, ''.join(found.values()))
        other = [e for e in events if e['kind'] in ('tool', 'usage', 'auxiliary')]
        self.assertEqual(excerpts(other, {'workbuddy': str(self.logs)}, KEY), {})
        self.assertNotIn(SECRET, ''.join(found.values()))

    def test_conversation_tree_includes_workbuddy_sessions(self):
        self.write(sample_lines())
        events, stats = collect(self.logs, 'workbuddy', KEY)
        save(self.db, events, stats, DAY, 'UTC')
        tree = tree_data(self.db, DAY, 'UTC')
        self.assertGreaterEqual(tree['conversation_count'], 1)
        self.assertIn(events[0]['session'], json.dumps(tree, ensure_ascii=False))

    def test_discover_sources_only_uses_the_ydkj_app(self):
        home = self.root / 'home'
        for name in ['.pi/agent/sessions', '.codex/sessions', '.workbuddy/projects',
                     '.workbuddyydkj/projects']:
            (home / name).mkdir(parents=True)
        found = discover_sources(home)
        self.assertEqual(found['pi_dir'], str(home / '.pi/agent/sessions'))
        self.assertEqual(found['codex_dir'], str(home / '.codex/sessions'))
        self.assertEqual(found['workbuddy_dir'], str(home / '.workbuddyydkj/projects'))
        self.assertNotIn(str(home / '.workbuddy/projects'), json.dumps(found))

    def test_explicit_import_writes_workbuddy_coverage(self):
        self.write(sample_lines())
        os.utime(self.path, (START + 3600, START + 3600))
        with contextlib.redirect_stdout(io.StringIO()):
            main(['import', '--db', str(self.db), '--date', DAY, '--timezone', 'UTC',
                  '--workbuddy-dir', str(self.logs)])
        connection = connect(self.db)
        try:
            coverage = json.loads(connection.execute(
                "SELECT coverage FROM import_days WHERE source='workbuddy' AND day=?", (DAY,)
            ).fetchone()[0])
            count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE source='workbuddy'").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(coverage['source'], 'workbuddy')
        self.assertGreater(count, 0)

    def test_review_roots_include_workbuddy_without_breaking_old_callers(self):
        self.write(sample_lines())
        with contextlib.redirect_stdout(io.StringIO()):
            main(['review', '--db', str(self.db), '--start-now', '--timezone', 'UTC',
                  '--workbuddy-dir', str(self.logs)])
        connection = connect(self.db)
        try:
            scope = connection.execute('SELECT scope FROM review_state').fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(scope, source_scope({'workbuddy': str(self.logs.resolve())}, salt(self.db)))
        # Older callers pass an args object without the new attribute.
        old = Namespace(db=str(self.root / 'old.sqlite3'), pi_dir=None, codex_dir=None,
                        batch_limit=4, last=True, out=None, allow_excerpts=None,
                        start_now=False, since=None, preview=False, allow_partial=False,
                        max_files=500, timezone='UTC')
        with self.assertRaises(ReviewError):
            review(old, lambda *_: None, lambda *_: None)

    def test_serve_passes_workbuddy_root_to_task_service(self):
        class FakeServer:
            server_port = 0
            access_token = 'fake'

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        class FakeService:
            runner = None
            allow_excerpts = False
            auto = False

            def close(self):
                pass

        args = Namespace(db=str(self.db), date=DAY, timezone='UTC', port=0, interval=30,
                         watch=False, auto_summarize=False, enable_llm=False, allow_excerpts=False,
                         summary_provider=None, summary_model=None, pi_dir=None, codex_dir=None,
                         workbuddy_dir=str(self.logs), idle_minutes=3, daily_summary_limit=None)
        with patch('worklog.cli.make_server', return_value=FakeServer()), \
             patch('worklog.tasks.TaskService') as service_class:
            service_class.return_value = FakeService()
            with contextlib.redirect_stdout(io.StringIO()):
                serve(args, DAY)
            roots = service_class.call_args.args[2]
        self.assertEqual(roots, {'workbuddy': str(self.logs)})

    def test_automatic_scan_creates_a_task_for_workbuddy_events(self):
        class Runner:
            provider = 'fake'
            model = 'workbuddy-fixture'

        rows = [
            message('user', USER_TEXT, 'u1', 9 * 3600),
            message('assistant', ASSISTANT_TEXT, 'a1', 9 * 3600 + 300),
        ]
        self.write(rows)
        events, stats = collect(self.logs, 'workbuddy', salt(self.db))
        save(self.db, events, stats, DAY, 'UTC')
        service = TaskService(self.db, Runner(), {'workbuddy': self.logs}, allow_excerpts=True,
                              zone='UTC', clock=lambda: START + 12 * 3600)
        try:
            configure(service, True, 'today', True)
            service.auto_tick()
            connection = connect(self.db)
            try:
                linked = connection.execute(
                    'SELECT session FROM conversation_sources').fetchall()
                tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            finally:
                connection.close()
            self.assertGreater(tasks, 0)
            self.assertIn(events[0]['session'], [row[0] for row in linked])
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
