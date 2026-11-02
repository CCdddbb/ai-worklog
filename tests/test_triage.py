import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from worklog.adapters import event
from worklog.core import bounds
from worklog.store import connect, report_data, salt, save
from worklog.tasks import TaskService
from worklog.triage import defer_session, deferred_sessions
from worklog.web import make_server


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'triage.sqlite3'
        self.key = salt(self.path)
        self.day = '2026-10-28'
        self.start, _ = bounds(self.day, 'UTC')
        self.row = event(self.key, 'pi', 'session', 'p', 'one', self.start + 9 * 3600, 'user')
        self.session = self.row['session']
        save(self.path, [self.row], {'source': 'pi'}, self.day, 'UTC')

    def toggle(self, value):
        db = connect(self.path)
        try:
            return defer_session(db, self.session, self.day, 'UTC', value, self.start)
        finally:
            db.close()

    def test_snooze_is_persistent_reversible_and_does_not_delete_activity(self):
        self.toggle(True)
        result = report_data(self.path, self.day, 'UTC')
        self.assertTrue(result['sessions'][0]['deferred'])
        self.assertEqual(result['event_count'], 1)
        self.toggle(False)
        self.assertFalse(report_data(self.path, self.day, 'UTC')['sessions'][0]['deferred'])

    def test_new_activity_at_same_timestamp_returns_to_inbox(self):
        self.toggle(True)
        other = event(self.key, 'pi', 'session', 'p', 'two', self.row['ts'], 'user')
        save(self.path, [self.row, other], {'source': 'pi'}, self.day, 'UTC')
        self.assertFalse(report_data(self.path, self.day, 'UTC')['sessions'][0]['deferred'])

    def test_reimport_and_usage_only_do_not_reopen_inbox(self):
        self.toggle(True)
        usage = event(self.key, 'pi', 'session', 'p', 'usage', self.row['ts'] + 1, 'usage')
        save(self.path, [self.row, usage], {'source': 'pi'}, self.day, 'UTC')
        self.assertTrue(report_data(self.path, self.day, 'UTC')['sessions'][0]['deferred'])

    def test_snooze_does_not_apply_to_another_day_or_timezone(self):
        self.toggle(True)
        db = connect(self.path)
        try:
            self.assertEqual(deferred_sessions(db, self.day, 'Asia/Shanghai'), set())
            self.assertEqual(deferred_sessions(db, '2026-10-29', 'UTC'), set())
        finally:
            db.close()

    def test_invalid_unknown_and_assigned_scopes_are_rejected(self):
        db = connect(self.path)
        try:
            for session, day, deferred in [('bad', self.day, True), ('0' * 16, self.day, True),
                                            (self.session, '2026-10-29', True),
                                            (self.session, self.day, 'true')]:
                with self.assertRaises((ValueError, TypeError)):
                    defer_session(db, session, day, 'UTC', deferred, self.start)
            with db:
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                           ('task', 'manual link', self.day, json.dumps([self.session]), self.start, None, 'open', self.start))
            with self.assertRaises(ValueError):
                defer_session(db, self.session, self.day, 'UTC', True, self.start)
        finally:
            db.close()

    def test_auto_mode_respects_snooze_until_new_activity(self):
        service = TaskService(self.path, None, roots={'pi': self.temp.name}, allow_excerpts=True,
                              auto=True, zone='UTC', clock=lambda: self.start + 8 * 3600)
        self.addCleanup(service.close)
        service.clock = lambda: self.start + 12 * 3600
        service.defer_session(self.session, self.day, True)
        service.auto_tick()
        self.assertEqual(service.list(self.day)['tasks'], [])
        other = event(self.key, 'pi', 'session', 'p', 'two', self.row['ts'] + 1, 'user')
        save(self.path, [self.row, other], {'source': 'pi'}, self.day, 'UTC')
        service.auto_tick()
        self.assertEqual(len(service.list(self.day)['tasks']), 1)

    def test_mutation_requires_token_origin_and_exact_fields(self):
        service = TaskService(self.path, None, zone='UTC')
        self.addCleanup(service.close)
        server = make_server(self.path, self.day, 'UTC', 0, service)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            body = {'session': self.session, 'date': self.day, 'deferred': True}
            def post(body, auth=True, origin=True):
                headers = {'Content-Type': 'application/json'}
                if auth:
                    headers['Authorization'] = 'Bearer ' + server.access_token
                if origin:
                    headers['Origin'] = base
                return urlopen(Request(base + '/api/session-triage', data=json.dumps(body).encode(), headers=headers))
            for auth, origin, expected in [(False, True, 401), (True, False, 403)]:
                with self.assertRaises(HTTPError) as error:
                    post(body, auth, origin)
                self.assertEqual(error.exception.code, expected)
            with post(body) as response:
                self.assertTrue(json.load(response)['deferred'])
            with self.assertRaises(HTTPError) as error:
                post(dict(body, path='/private/synthetic'))
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
