"""Purging is a privacy promise, so the specification itself is tested.

`worklog/purge.py` describes every derived table as data. These tests fail when
a new table or reference column appears without a purge policy, and they check
that each mode leaves no orphaned AI-derived row behind.
"""
import tempfile
import unittest
from pathlib import Path

from worklog.adapters import event
from worklog.core import bounds
from worklog.purge import (
    AUTOMATION_TABLES,
    CONVERSATION_TABLES,
    REVIEW_TABLES,
    before,
    summaries_only,
    titles_only,
    uncovered_references,
)
from worklog.store import connect, salt, save
from worklog.tasks import TaskService

DAY = '2026-01-02'
LATER = '2026-01-09'
SESSION = 'a1b2c3d4e5f60718'
START, _ = bounds(DAY, 'UTC')
LATER_START, _ = bounds(LATER, 'UTC')


class PurgeSpecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'worklog.sqlite3'
        key = salt(self.db)
        rows = [event(key, 'pi', SESSION, 'project', 'u1', START + 3600, 'user', title='处理登录问题'),
                event(key, 'pi', SESSION, 'project', 'u2', START + 3700, 'assistant', '已修复'),
                event(key, 'pi', 's2', 'project', 'u3', LATER_START + 3600, 'user', title='后面的工作')]
        save(self.db, rows, {'source': 'pi', 'selected': 1}, DAY, 'UTC')
        save(self.db, rows[2:], {'source': 'pi', 'selected': 1}, LATER, 'UTC')
        self.service = TaskService(self.db, None, zone='UTC', clock=lambda: LATER_START + 7200)
        db = connect(self.db)
        try:
            self.session = db.execute('SELECT session FROM events ORDER BY ts LIMIT 1').fetchone()[0]
        finally:
            db.close()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def run_sql(self, sql, params=()):
        db = connect(self.db)
        try:
            with db:
                db.execute(sql, params)
        finally:
            db.close()

    def count(self, table, where='1=1', params=()):
        db = connect(self.db)
        try:
            return db.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', params).fetchone()[0]
        finally:
            db.close()

    def seed(self, task):
        record = 'r' + task['id'][:8]
        job = 'job-' + task['id'][:8]
        db = connect(self.db)
        try:
            with db:
                db.execute('INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (record, DAY, task['id'], job, '登录问题', 'bugfix', 0, 'reported_resolved',
                            '{}', '[]', '[]', '[]', START + 3600, START + 4000, 400, None, START, START))
                db.execute('INSERT INTO summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (job, task['id'], 'stage', 'fp', 'digest', 'success', '{}', '{}', None,
                            'fake', 'fixture', START, START, '{}', START))
                db.execute('INSERT INTO task_reviews VALUES (?,?,?,?,?,?,?)',
                           (task['id'], 'confirmed', '登录回调已修复', '', '', '', START))
                db.execute('INSERT INTO task_git VALUES (?,?)', (task['id'], 'c1'))
                db.execute('INSERT INTO automatic_coverage VALUES (?,?,?)', (task['id'], 'u1', 'd'))
                db.execute('INSERT INTO task_automation VALUES (?,?,?,?,?,?)',
                           (task['id'], 'project', 0, None, None, START))
                db.execute('INSERT INTO conversation_sources VALUES (?,?,?)', (task['id'], self.session, DAY))
                db.execute('INSERT INTO conversation_parts VALUES (?,?,?,?,?,?,?,?)',
                           ('part1', self.session, '登录', 'bugfix', 'reported_resolved', '{}', START, START))
                db.execute('INSERT INTO conversation_progress VALUES (?,?,?,?,?)', ('part1', job, DAY, '{}', START))
                db.execute('INSERT INTO conversation_part_records VALUES (?,?)', ('part1', record))
                db.execute('INSERT INTO work_record_sessions VALUES (?,?)', (record, self.session))
                db.execute('INSERT INTO work_record_history VALUES (?,?,?,?)', (record, job, '{}', START))
                db.execute('INSERT INTO legacy_shared_records VALUES (?)', (record,))
                db.execute('INSERT INTO legacy_context_jobs VALUES (?)', (job,))
                db.execute('INSERT INTO conversation_projection_jobs VALUES (?)', (job,))
                db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)',
                           ('rv1', 'scope', START, START, START, 'out', '{}', 'fake', 'fixture'))
                db.execute('INSERT INTO review_seen VALUES (?,?,?,?)', ('scope', 'u1', 'd', 'rv1'))
                db.execute('INSERT INTO review_cache VALUES (?,?,?)', ('fp', '{}', '{}'))
                db.execute('INSERT INTO review_state VALUES (?,?,?,?)', ('scope', START, START, 0))
                db.execute('INSERT INTO session_names VALUES (?,?,?)', (self.session, '登录问题', 'summary'))
                db.execute('INSERT INTO automation_scans VALUES (?,?,?)', (DAY, 'UTC', 'd'))
                db.execute('INSERT INTO automation_settings VALUES (?,?)', (1, '{}'))
        finally:
            db.close()

    def test_specification_covers_every_reference_column(self):
        db = connect(self.db)
        try:
            self.assertEqual(uncovered_references(db), [])
        finally:
            db.close()

    def test_summaries_only_leaves_no_derived_row(self):
        task = self.service.create('登录问题', [self.session], DAY)
        self.seed(task)
        events = self.count('events')

        db = connect(self.db)
        try:
            with db:
                summaries_only(db)
        finally:
            db.close()

        tables = ('tasks', 'work_records', 'work_record_history', 'summaries', 'task_reviews',
                  'task_git', 'automatic_coverage', 'task_automation') + AUTOMATION_TABLES + REVIEW_TABLES
        for table in tables:
            self.assertEqual(self.count(table), 0, table)
        for table in CONVERSATION_TABLES:
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.count('session_names', "origin='summary'"), 0)
        self.assertEqual(self.count('events'), events, 'source events must survive')

    def test_before_keeps_later_work_and_drops_orphans(self):
        early = self.service.create('登录问题', [self.session], DAY)
        self.seed(early)
        db = connect(self.db)
        try:
            with db:
                before(db, LATER, 'UTC')
        finally:
            db.close()

        self.assertEqual(self.count('events', 'ts < ?', (LATER_START,)), 0)
        self.assertGreater(self.count('events'), 0)
        self.assertEqual(self.count('tasks'), 0)
        self.assertEqual(self.count('summaries'), 0)
        self.assertEqual(self.count('task_reviews'), 0)
        self.assertEqual(self.count('task_git'), 0)
        self.assertEqual(self.count('automatic_coverage'), 0)
        self.assertEqual(self.count('conversation_sources'), 0)
        self.assertEqual(self.count('work_records'), 0)
        self.assertEqual(self.count('work_record_history'), 0)
        self.assertEqual(self.count('legacy_context_jobs'), 0)
        self.assertEqual(self.count('legacy_shared_records'), 0)
        self.assertEqual(self.count('reviews'), 0)
        self.assertEqual(self.count('review_seen'), 0)

    def test_titles_only_keeps_manual_names(self):
        db = connect(self.db)
        try:
            with db:
                db.execute('INSERT INTO session_names VALUES (?,?,?)', (self.session, '自动标题', 'summary'))
                db.execute('INSERT INTO session_names VALUES (?,?,?)', ('s2', '我的标题', 'manual'))
        finally:
            db.close()
        db = connect(self.db)
        try:
            with db:
                titles_only(db)
        finally:
            db.close()
        self.assertEqual(self.count('session_names', 'origin=?', ('manual',)), 1)
        self.assertEqual(self.count('session_names', 'origin=?', ('summary',)), 0)


if __name__ == '__main__':
    unittest.main()
