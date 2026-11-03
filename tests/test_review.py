import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from business_fixture import business_result

from worklog.adapters import prefix_timestamp
from worklog.cli import main
from worklog.evidence import EvidenceUnavailable
from worklog.llm import SummaryError
from worklog.review import (
    ReviewError,
    batches,
    review,
    review_write_lock,
    since_timestamp,
)
from worklog.store import connect, salt

START = 1767312000.0


class Runner:
    provider = 'fake'
    model = 'review-fixture'

    def __init__(self):
        self.calls = []
        self.fail_at = None

    def __call__(self, data, mode, cancel):
        self.calls.append(data)
        if len(self.calls) == self.fail_at:
            raise SummaryError('pi_failed')
        return business_result(data, '调整了终端配置，结果仍待核验。'), {'tokens': 10}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.db = self.root / 'review.sqlite3'
        self.now = START + 1000
        self.runner = Runner()
        self.output = []
        self.progress = []

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **kw):
        values = {'db': str(self.db), 'pi_dir': str(self.logs), 'codex_dir': None, 'timezone': 'UTC',
                  'start_now': False, 'since': None, 'last': False, 'allow_excerpts': None, 'preview': False,
                  'max_files': 500, 'batch_limit': 4, 'allow_partial': False, 'summary_provider': None,
                  'summary_model': None, 'out': None}
        values.update(kw)
        return Namespace(**values)

    def run_review(self, **kw):
        return review(self.args(**kw), self.output.append, self.progress.append,
                      runner_factory=lambda *args: self.runner, clock=lambda: self.now)

    def init(self, consent=True):
        self.now = START
        self.run_review(start_now=True, allow_excerpts=consent)
        self.now = START + 1000
        self.output.clear()

    def log(self, offset=10, count=1, name='session', partial=False, source='pi'):
        values = []
        stamp = lambda ts: datetime.fromtimestamp(START + ts, timezone.utc).isoformat()
        if source == 'pi':
            values.append({'type': 'session', 'id': name, 'cwd': '/private/customer'})
            for i in range(count):
                values += [
                    {'type': 'message', 'id': f'u{offset}-{i}', 'timestamp': stamp(offset + i * 2),
                     'message': {'role': 'user', 'content': '调整终端 password=DO_NOT_SEND_THIS'}},
                    {'type': 'message', 'id': f'a{offset}-{i}', 'timestamp': stamp(offset + i * 2 + 1),
                     'message': {'role': 'assistant', 'stopReason': 'stop', 'provider': 'demo', 'model': 'demo',
                                 'content': [{'type': 'thinking', 'thinking': 'PRIVATE_THINKING'},
                                             {'type': 'text', 'text': '已调整配置，等待核验。'}]}},
                    {'type': 'message', 'id': f't{offset}-{i}', 'timestamp': stamp(offset + i * 2 + 1),
                     'message': {'role': 'toolResult', 'content': 'PRIVATE_TOOL_OUTPUT'}}]
        else:
            values = [{'type': 'session_meta', 'payload': {'id': name, 'cwd': '/private/customer'}},
                      {'type': 'response_item', 'timestamp': stamp(offset), 'payload': {
                          'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '核对菜单配置'}]}},
                      {'type': 'response_item', 'timestamp': stamp(offset + 1), 'payload': {
                          'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                          'content': [{'type': 'output_text', 'text': '已检查，待验证。'}]}}]
        with (self.logs / (name + '.jsonl')).open('a') as f:
            f.write(''.join(json.dumps(v) + '\n' for v in values))
            if partial:
                f.write('{"type":')

    def count(self, table):
        db = connect(self.db)
        try:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        finally:
            db.close()

    def test_first_run_requires_explicit_boundary_and_init_does_not_read_logs(self):
        self.log()
        with self.assertRaises(ReviewError):
            self.run_review()
        self.init()
        self.assertEqual(self.count('events'), 0)
        self.assertEqual(self.runner.calls, [])
        with self.assertRaises(ReviewError):
            self.run_review(start_now=True)

    def test_success_and_repeated_command_do_not_repeat_model_or_events(self):
        self.init()
        self.log(count=2)
        self.assertEqual(self.run_review()['events'], 4)
        self.assertEqual(self.run_review()['status'], 'empty')
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.count('review_seen'), 4)
        self.assertEqual(self.count('reviews'), 1)
        self.assertIn('问题/目标', ''.join(self.output))

    def test_late_arrival_same_timestamp_and_cross_day_are_not_lost(self):
        self.init()
        self.log(offset=20)
        self.run_review()
        self.log(offset=5, name='late')
        self.assertEqual(self.run_review()['events'], 2)
        self.log(offset=20, name='same-time')
        self.assertEqual(self.run_review()['events'], 2)
        self.now += 86400
        self.log(offset=86420, name='tomorrow')
        self.assertEqual(self.run_review()['events'], 2)

    def test_failure_keeps_checkpoint_and_retry_reuses_verified_batches(self):
        self.init()
        self.log(count=15)
        self.runner.fail_at = 2
        with self.assertRaises(SummaryError):
            self.run_review()
        self.assertEqual(self.count('review_seen'), 0)
        self.assertEqual(self.count('reviews'), 0)
        self.assertEqual(self.count('review_cache'), 1)
        self.assertIn('处理措施', ''.join(self.output))  # Already streamed before the second call failed.
        self.runner.fail_at = None
        self.assertEqual(self.run_review()['events'], 30)
        self.assertEqual(len(self.runner.calls), 3)
        self.assertTrue(any('复用' in line for line in self.progress))

    def test_batch_budget_leaves_backlog_for_next_command(self):
        self.init()
        self.log(count=15)
        self.assertEqual(self.run_review(batch_limit=1)['remaining'], 6)
        self.assertEqual(self.count('review_seen'), 24)
        self.assertEqual(self.run_review(batch_limit=1)['events'], 6)
        self.assertEqual(self.run_review()['status'], 'empty')

    def test_preview_redaction_and_no_persisted_excerpts(self):
        self.init()
        self.log()
        self.assertEqual(self.run_review(preview=True)['status'], 'preview')
        self.assertEqual(self.count('review_seen'), 0)
        self.assertEqual(self.runner.calls, [])
        self.run_review()
        sent = json.dumps(self.runner.calls)
        for private in ['DO_NOT_SEND_THIS', 'PRIVATE_THINKING', 'PRIVATE_TOOL_OUTPUT', '/private/customer']:
            self.assertNotIn(private, sent)
            self.assertNotIn(private.encode(), self.db.read_bytes())
        self.assertIn('[REDACTED]', sent)

    def test_metadata_only_can_revoke_persisted_excerpt_consent(self):
        self.init()
        self.log()
        with self.assertRaises(EvidenceUnavailable):
            self.run_review(allow_excerpts=False)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.count('review_seen'), 0)
        self.log(offset=30, name='second')
        with self.assertRaises(EvidenceUnavailable):
            self.run_review()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.count('review_seen'), 0)

    def test_coverage_gap_is_blocked_unless_explicitly_accepted(self):
        self.init()
        self.log(name='first')
        self.log(name='second')
        with self.assertRaises(ReviewError):
            self.run_review(max_files=1)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.count('review_seen'), 0)
        self.assertEqual(self.run_review(max_files=1, allow_partial=True)['status'], 'success')
        self.assertIn('来源不完整', ''.join(self.output))
        self.assertEqual(self.run_review()['events'], 2)

    def test_old_oversized_record_does_not_block_but_current_one_does(self):
        self.init()
        self.log()
        path = self.logs / 'big.jsonl'
        def big(offset):
            return json.dumps({'type': 'message', 'timestamp': datetime.fromtimestamp(
                START + offset, timezone.utc).isoformat(), 'message': {'role': 'user', 'content': 'x' * 2048}}) + '\n'
        path.write_text(big(-50))
        with patch('worklog.adapters.MAX_LINE', 1024):
            self.assertEqual(self.run_review()['events'], 2)
        self.assertIn('窗口外过长行=1', ''.join(self.output))
        path.write_text(big(50))
        with patch('worklog.adapters.MAX_LINE', 1024), self.assertRaises(ReviewError):
            self.run_review()
        self.assertEqual(self.count('review_seen'), 2)

    def test_prefix_timestamp_never_matches_nested_message_text(self):
        nested = b'{"message":{"timestamp":"2000-01-01T00:00:00Z"},"timestamp":"2026-01-02T00:00:00Z","content":"unfinished'
        self.assertEqual(prefix_timestamp(nested), START)
        unknown = b'{"message":{"timestamp":"2000-01-01T00:00:00Z","text":"' + b'x' * 2048
        self.assertIsNone(prefix_timestamp(unknown))
        self.assertIsNone(prefix_timestamp(b'not JSON'))

    def test_output_failure_and_existing_export_never_consume_events(self):
        self.init()
        self.log()
        out = self.root / 'existing.md'
        out.write_text('do not replace')
        with self.assertRaises(FileExistsError):
            self.run_review(out=str(out))
        self.assertEqual(self.count('review_seen'), 0)
        self.assertEqual(out.read_text(), 'do not replace')
        def broken(_):
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            review(self.args(), broken, self.progress.append,
                   runner_factory=lambda *args: self.runner, clock=lambda: self.now)
        self.assertEqual(self.count('review_seen'), 0)
        self.assertEqual(self.run_review()['events'], 2)
        self.assertEqual(len(self.runner.calls), 1)

    def test_invalid_citations_never_print_model_text_or_advance(self):
        self.init()
        self.log()
        original = self.runner.__class__.__call__
        def invalid(runner, *args):
            result, usage = original(runner, *args)
            result['overview']['evidence_ids'] = ['e_not_real']
            return result, usage
        with patch.object(Runner, '__call__', invalid), self.assertRaises(SummaryError):
            self.run_review()
        self.assertNotIn('调整了终端配置', ''.join(self.output))
        self.assertEqual(self.count('review_seen'), 0)

    def test_new_activity_during_generation_is_deferred(self):
        self.init()
        self.log()
        original = self.runner.__class__.__call__
        def concurrent(runner, *args):
            self.log(offset=500, name='during-call')
            return original(runner, *args)
        with patch.object(Runner, '__call__', concurrent):
            self.assertEqual(self.run_review()['events'], 2)
        self.assertEqual(self.run_review()['events'], 2)

    def test_changed_event_status_is_reviewed_again(self):
        self.init()
        self.log()
        self.run_review()
        path = self.logs / 'session.jsonl'
        path.write_text(path.read_text().replace('"stopReason": "stop"', '"stopReason": "error"'))
        self.assertEqual(self.run_review()['events'], 1)

    def test_last_export_and_summary_purge(self):
        self.init()
        self.log()
        self.run_review()
        out = self.root / 'review.md'
        self.assertEqual(self.run_review(last=True, out=str(out))['status'], 'last')
        self.assertIn('AI 增量回顾', out.read_text())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(main(['purge', '--db', str(self.db), '--summaries-only']), 0)
        for table in ['reviews', 'review_seen', 'review_state', 'review_cache']:
            self.assertEqual(self.count(table), 0)

    def test_codex_source_and_no_implicit_scope_expansion(self):
        self.now = START
        self.run_review(start_now=True, pi_dir=None, codex_dir=str(self.logs), allow_excerpts=True)
        self.now += 1000
        self.log(source='codex')
        self.assertEqual(self.run_review(pi_dir=None, codex_dir=str(self.logs))['events'], 2)
        self.assertIn('核对菜单配置', json.dumps(self.runner.calls, ensure_ascii=False))
        with self.assertRaises(ReviewError):
            self.run_review()

    def test_lock_rejects_concurrent_review_and_purge(self):
        self.init()
        with review_write_lock(self.db):
            with self.assertRaises(ReviewError):
                self.run_review()
            with self.assertRaises(ReviewError):
                main(['purge', '--db', str(self.db), '--summaries-only'])

    def test_identity_key_cannot_silently_reset_initialized_scope(self):
        self.init()
        (self.root / 'identity.key').unlink()
        with self.assertRaises(ValueError):
            salt(self.db)

    def test_date_validation_and_batches_do_not_sample_middle_sessions(self):
        self.assertEqual(since_timestamp('2026-01-02T00:00:00Z'), START)
        with self.assertRaises(ReviewError):
            since_timestamp('2026-01-02T00:00:00')
        rows = [{'id': str(i), 'session': str(i), 'ts': i} for i in range(25)]
        groups = batches(rows)
        self.assertEqual([len(g) for g in groups], [12, 12, 1])
        self.assertEqual([r for group in groups for r in group], rows)

    def test_cli_calls_isolated_pi_process_with_synthetic_evidence(self):
        self.init()
        self.log()
        fake = self.root / 'pi-fixture'
        body = self.root / 'pi-fixture.py'
        body.write_text('import json, sys\n'
                        'for flag in ["--no-tools", "--no-session", "--no-extensions", "--offline"]:\n'
                        '    assert flag in sys.argv\n'
                        'assert "复盘" in sys.argv[-1]\n'
                        'data = json.load(sys.stdin)\n'
                        'item = {"text": "合成CLI任务，尚待核验", "evidence_ids": [data["evidence"][0]["id"]]}\n'
                        'result = {"schema_version": 2, "overview": item, "work_items": [{"title": "合成CLI配置任务", "problem": item, "actions": [item], "outcome": None, "verification": None, "remaining": [], "status": "in_progress"}]}\n'
                        'message = {"role": "assistant", "stopReason": "stop", "content": '
                        '[{"type": "text", "text": json.dumps(result)}], "usage": {"input": 1}}\n'
                        'print(json.dumps({"type": "message_end", "message": message}))\n'
                        'print(json.dumps({"type": "agent_settled", "aborted": False}))\n')
        fake.write_text('#!/bin/sh\nexec "' + sys.executable + '" "' + str(body) + '" "$@"\n')
        fake.chmod(0o700)
        with patch('worklog.llm.shutil.which', return_value=str(fake)), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('sys.stderr', new_callable=io.StringIO):
            code = main(['review', '--db', str(self.db), '--pi-dir', str(self.logs),
                         '--summary-provider', 'fixture', '--summary-model', 'fixture'])
        self.assertEqual(code, 0)
        self.assertIn('合成CLI任务', output.getvalue())
        self.assertEqual(self.count('review_seen'), 2)

    def test_cli_init_preview_and_last_do_not_require_model(self):
        with patch('sys.stdout', new_callable=io.StringIO):
            code = main(['review', '--db', str(self.db), '--pi-dir', str(self.logs), '--start-now'])
        self.assertEqual(code, 0)
        db = connect(self.db)
        try:
            self.assertEqual(db.execute('SELECT excerpts FROM review_state').fetchone()[0], 0)
        finally:
            db.close()
