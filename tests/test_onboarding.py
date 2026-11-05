import json
import tempfile
import unittest
from pathlib import Path

from worklog.core import bounds
from worklog.onboarding import onboarding_state, read_gaps
from worklog.store import connect

DAY = '2026-01-02'
ZONE = 'UTC'
SESSION = 'a' * 16
START = bounds(DAY, ZONE)[0] + 60


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'data.sqlite3'
        connect(self.db).close()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, statement, values=()):
        db = connect(self.db)
        try:
            with db:
                db.execute(statement, values)
        finally:
            db.close()

    def add_event(self, day=DAY):
        ts = bounds(day, ZONE)[0] + 60
        self.write('INSERT INTO events (id,source,session,project,ts,kind,status,tokens) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   (f'e-{day}-{SESSION}', 'pi', SESSION, 'p1', ts, 'assistant', 'ok', 10))
        return ts

    def add_import(self, coverage=None, day=DAY, zone=ZONE, source='pi'):
        payload = {'source': source, 'discovered': 3, 'selected': 3, **dict(coverage or {})}
        self.write('INSERT OR REPLACE INTO import_days VALUES (?,?,?,?)',
                   (source, day, zone, json.dumps(payload)))

    def add_task(self, ts):
        self.write('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                   ('t' * 32, '手工记录', DAY, '[]', ts, None, 'open', ts))

    def add_automatic_record(self, ts):
        self.write('INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('w' * 32, DAY, 't' * 32, 'j' * 32, '自动记录', 'other', 0, 'open',
                    '{}', '[]', '[]', '[]', ts, ts, 0, None, ts, ts))

    def state(self, day=DAY):
        return onboarding_state(self.db, day, ZONE)

    def test_empty_database_points_at_existing_database_entry(self):
        state = self.state()
        self.assertEqual(state['state'], 'empty')
        self.assertEqual(state['facts']['events'], 0)
        self.assertIn('另一个数据库', state['message'])
        self.assertIn('--db', state['database']['command'])
        self.assertNotIn('没有工作', state['message'])
        self.assertEqual([item['action'] for item in state['actions']], ['creation', 'sources'])

    def test_imported_sessions_without_records_are_unorganized(self):
        self.add_event()
        self.add_import()
        state = self.state()
        self.assertEqual(state['state'], 'unorganized')
        self.assertEqual(state['facts']['sessions'], 1)
        self.assertEqual(state['facts']['task_count'], 0)
        self.assertIn('无需启用模型', state['message'])
        self.assertEqual(state['actions'][0]['action'], 'triage')

    def test_manual_or_automatic_record_moves_to_ready(self):
        ts = self.add_event()
        self.add_import()
        self.add_task(ts)
        self.assertEqual(self.state()['state'], 'ready')
        db = connect(self.db)
        try:
            with db:
                db.execute('DELETE FROM tasks')
        finally:
            db.close()
        self.add_automatic_record(ts)
        self.assertEqual(self.state()['state'], 'ready')

    def test_structural_gaps_are_disclosed_and_never_equal_no_work(self):
        self.add_event()
        self.add_import({'omitted_files': 2, 'malformed_lines': 1, 'discovered': 9})
        state = self.state()
        self.assertEqual(state['state'], 'incomplete')
        self.assertEqual(state['facts']['gaps'], {'pi': {'omitted_files': 2, 'malformed_lines': 1}})
        self.assertIn('不是「没有工作」', state['message'])
        self.assertIn('不需要模型授权', state['message'])

    def test_gaps_from_another_day_do_not_leak_into_today(self):
        self.add_event(day='2026-01-01')
        self.add_import({'omitted_files': 5}, day='2026-01-01')
        state = self.state()
        self.assertEqual(state['facts']['gaps'], {})
        # History elsewhere in the database must not be reported as an empty database,
        # nor as today's collection gap.
        self.assertEqual(state['state'], 'empty_day')
        self.assertEqual(state['facts']['lifetime_events'], 1)
        self.assertIn('已有其他日期', state['message'])
        self.assertNotIn('另一个数据库', state['message'])

    def test_demo_data_is_labelled_as_fiction(self):
        self.add_import({'selected': 0, 'synthetic': True}, source='demo')
        state = self.state()
        self.assertEqual(state['state'], 'demo')
        self.assertIn('虚构', state['message'])

    def test_read_gaps_ignores_booleans_and_non_gap_stats(self):
        self.assertEqual(read_gaps({'pi': {'selected': 4, 'omitted_files': 0,
                                           'unreadable_files': True, 'truncated_files': 3}}),
                         {'pi': {'truncated_files': 3}})

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            onboarding_state(self.db, '2026-02-30', ZONE)


if __name__ == '__main__':
    unittest.main()
