import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worklog.adapters import collect
from worklog.store import connect, markdown, report_data, salt, save

DAY = '2026-01-02'
KEY = b'x' * 32
SECRET = 'PRIVATE_CONTENT_CANARY'


def pi_header():
    return {'type': 'session', 'id': 'fake-session', 'cwd': '/private/client-secret'}


def pi_message(role='assistant', ident='a', usage=None):
    return {'type': 'message', 'id': ident, 'timestamp': '2026-01-02T01:00:00Z', 'message': {
        'role': role, 'provider': 'codebuddy', 'model': 'demo-model', 'stopReason': 'stop',
        'content': SECRET, 'arguments': {'token': SECRET}, 'thinking': SECRET,
        'usage': usage or {'input': 10, 'output': 2, 'cacheRead': 8}}}


def codex_row(kind, payload, ordinal):
    return {'type': kind, 'payload': payload, 'ordinal': ordinal, 'timestamp': '2026-01-02T01:00:00Z'}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'sessions'
        self.source.mkdir()
        self.path = self.source / 'sample.jsonl'
        self.db = self.root / 'data/local.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, values):
        self.path.write_text(''.join(json.dumps(v) + '\n' for v in values))

    def test_source_is_read_only_and_no_body_is_retained(self):
        self.write([pi_header(), pi_message(), pi_message('user', 'b')])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        events, stats = collect(self.source, 'pi', KEY)
        save(self.db, events, stats, DAY, 'UTC')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertNotIn(SECRET.encode(), self.db.read_bytes())
        self.assertNotIn(b'/private/client-secret', self.db.read_bytes())
        self.assertEqual(events[0]['provider'], 'codebuddy')

    def test_default_title_disabled_and_explicit_title_is_short(self):
        self.write([pi_header(), pi_message('user')])
        rows, _ = collect(self.source, 'pi', KEY)
        self.assertEqual(rows[0]['title'], '')
        rows, _ = collect(self.source, 'pi', KEY, titles=True)
        self.assertEqual(rows[0]['title'], SECRET)

    def test_reimport_is_idempotent(self):
        self.write([pi_header(), pi_message()])
        rows, stats = collect(self.source, 'pi', KEY)
        self.assertEqual(save(self.db, rows, stats, DAY, 'UTC')['new_events'], 1)
        self.assertEqual(save(self.db, rows, stats, DAY, 'UTC')['new_events'], 0)
        self.assertEqual(report_data(self.db, DAY, 'UTC')['tokens'], 20)

    def test_duplicate_response_ids_deduplicate_even_across_clones(self):
        message = pi_message()
        message['message']['responseId'] = 'response-test'
        self.write([pi_header(), message])
        clone = self.source / 'clone.jsonl'
        clone.write_text(json.dumps(dict(pi_header(), id='clone')) + '\n' + json.dumps(message) + '\n')
        rows, stats = collect(self.source, 'pi', KEY)
        save(self.db, rows, stats, DAY, 'UTC')
        self.assertEqual(report_data(self.db, DAY, 'UTC')['tokens'], 20)

    def test_auxiliary_usage_counted_but_summaries_ignored(self):
        extra = {'type': 'compaction', 'id': 'compact', 'timestamp': '2026-01-02T01:00:00Z',
                 'summary': SECRET, 'usage': {'input': 3, 'output': 1}}
        self.write([pi_header(), pi_message(), extra])
        rows, stats = collect(self.source, 'pi', KEY)
        save(self.db, rows, stats, DAY, 'UTC')
        value = report_data(self.db, DAY, 'UTC')
        self.assertEqual(value['tokens'], 24)
        self.assertEqual(value['request_count'], 1)
        auxiliary = next(r for r in rows if r['kind'] == 'auxiliary')
        self.assertEqual(auxiliary['model'], 'unknown')
        self.assertNotIn(SECRET.encode(), self.db.read_bytes())

    def test_partial_tail_is_retried_on_next_import(self):
        self.write([pi_header()])
        with self.path.open('a') as stream:
            stream.write(json.dumps(pi_message()))
        rows, stats = collect(self.source, 'pi', KEY)
        self.assertEqual(rows, [])
        self.assertEqual(stats['partial_lines'], 1)
        with self.path.open('a') as stream:
            stream.write('\n')
        self.assertEqual(len(collect(self.source, 'pi', KEY)[0]), 1)

    def test_malformed_line_does_not_hide_next_valid_record(self):
        self.write([pi_header(), pi_message()])
        self.path.write_text('not-json\n[]\n' + self.path.read_text())
        rows, stats = collect(self.source, 'pi', KEY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats['malformed_lines'], 2)

    def test_limits_disclose_incomplete_coverage(self):
        self.write([pi_header(), pi_message()])
        with patch('worklog.adapters.MAX_FILE', 10):
            rows, stats = collect(self.source, 'pi', KEY)
        self.assertEqual(rows, [])
        self.assertEqual(stats['oversized_files'], 1)
        (self.source / 'second.jsonl').write_text(self.path.read_text())
        _, stats = collect(self.source, 'pi', KEY, max_files=1)
        self.assertEqual(stats['omitted_files'], 1)

    def test_symlink_is_not_read(self):
        self.write([pi_header(), pi_message()])
        (self.source / 'link.jsonl').symlink_to(self.path)
        rows, stats = collect(self.source, 'pi', KEY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats['symlinks_skipped'], 1)

    def test_codex_new_and_old_usage_not_added_together(self):
        self.write([
            codex_row('session_meta', {'id': 'test', 'cwd': '/test', 'model_provider': 'openai'}, 0),
            codex_row('turn_context', {'model': 'test'}, 1),
            codex_row('token_usage_record', {'response_id': 'r', 'usage': {'input_tokens': 100,
                      'cached_input_tokens': 80, 'output_tokens': 5}}, 2),
            codex_row('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 5}}}, 3)])
        rows, stats = collect(self.source, 'codex', KEY)
        self.assertEqual(sum(r['tokens'] for r in rows), 105)
        self.assertEqual(rows[0]['input'], 20)
        self.assertEqual(stats['legacy_usage_suppressed'], 1)

    def test_codex_legacy_cumulative_is_differenced(self):
        self.write([codex_row('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
            'input_tokens': value, 'output_tokens': 0}}}, i) for i, value in enumerate([100, 150, 150])])
        rows, _ = collect(self.source, 'codex', KEY)
        self.assertEqual(sum(r['tokens'] for r in rows), 150)

    def test_codex_counter_reset_is_not_negative_or_double_counted(self):
        self.write([codex_row('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
            'input_tokens': value}}}, i) for i, value in enumerate([100, 20, 30])])
        rows, stats = collect(self.source, 'codex', KEY)
        self.assertEqual(sum(r['tokens'] for r in rows), 110)
        self.assertEqual(stats['counter_resets'], 1)

    def test_database_ownership_and_identity_permissions(self):
        key = salt(self.db)
        self.assertEqual(len(key), 32)
        self.assertEqual(salt(self.db), key)
        connect(self.db).close()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        other = self.root / 'foreign.db'
        db = sqlite3.connect(other)
        db.execute('CREATE TABLE private (value TEXT)')
        db.close()
        with self.assertRaises(ValueError):
            connect(other)

    def test_unchanged_owned_database_reads_do_not_commit_header_writes(self):
        connect(self.db).close()
        observer = sqlite3.connect(self.db)
        try:
            before = observer.execute('PRAGMA data_version').fetchone()[0]
            for _ in range(5):
                connect(self.db).close()
            after = observer.execute('PRAGMA data_version').fetchone()[0]
            self.assertEqual(before, after)
        finally:
            observer.close()

    def test_missing_identity_key_does_not_silently_duplicate_existing_data(self):
        self.write([pi_header(), pi_message()])
        rows, stats = collect(self.source, 'pi', KEY)
        save(self.db, rows, stats, DAY, 'UTC')
        with self.assertRaises(ValueError):
            salt(self.db)
        self.assertFalse((self.db.parent / 'identity.key').exists())

    def test_report_contains_limitations_and_no_claim_of_finished_tasks(self):
        self.write([pi_header(), pi_message()])
        rows, stats = collect(self.source, 'pi', KEY)
        save(self.db, rows, stats, DAY, 'UTC')
        text = markdown(report_data(self.db, DAY, 'UTC'))
        self.assertIn('待人工补充', text)
        self.assertIn('不是实际账单', text)
        self.assertNotIn(SECRET, text)


if __name__ == '__main__':
    unittest.main()
