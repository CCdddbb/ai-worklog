import json
import tempfile
import unittest
from pathlib import Path

from worklog.adapters import event
from worklog.core import bounds
from worklog.store import connect, save
from worklog.weekly import week_data


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'week.sqlite3'

    def test_calendar_week_crosses_month_and_year(self):
        result = week_data(self.path, '2026-01-01', 'UTC')
        self.assertEqual(result['first'], '2025-12-29')
        self.assertEqual(result['last'], '2026-01-04')
        self.assertEqual(len(result['days']), 7)
        self.assertEqual(result['totals']['recorded_days'], 0)
        self.assertTrue(all(not day['recorded'] for day in result['days']))

    def test_usage_and_reviews_are_separate(self):
        start, _ = bounds('2026-10-28', 'Asia/Shanghai')
        rows = [event(b'k', 'pi', 's', 'p', 'a', start + 1, 'assistant',
                      values={'tokens': 123})]
        save(self.path, rows, {'source': 'pi', 'selected': 1}, '2026-10-28', 'Asia/Shanghai')
        db = connect(self.path)
        with db:
            for index, status in enumerate(['confirmed', 'blocked', None]):
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                           (str(index), '<unsafe>', '2026-10-28', '[]', start, None, 'open', start))
                if status:
                    db.execute('INSERT INTO task_reviews VALUES (?,?,?,?,?,?,?)',
                               (str(index), status, 'result', '', 'follow up', 'test', start))
        db.close()
        result = week_data(self.path, '2026-10-28', 'Asia/Shanghai')
        self.assertEqual(result['totals']['tokens'], 123)
        self.assertEqual(result['totals']['requests'], 1)
        self.assertEqual(result['totals']['tasks'], 3)
        self.assertEqual(result['totals']['confirmed'], 1)
        self.assertEqual(result['totals']['blocked'], 1)
        self.assertEqual(result['totals']['unreviewed'], 1)
        self.assertEqual(result['totals']['sessions'], 1)
        self.assertEqual(result['days'][2]['tasks'][0]['title'], '<unsafe>')

    def test_daily_scan_history_does_not_disappear_after_next_import(self):
        save(self.path, [], {'source': 'pi', 'selected': 2, 'omitted_files': 1}, '2026-10-27', 'UTC')
        save(self.path, [], {'source': 'pi', 'selected': 0}, '2026-10-28', 'UTC')
        result = week_data(self.path, '2026-10-28', 'UTC')
        self.assertTrue(result['days'][1]['coverage_gaps'])
        self.assertTrue(result['days'][1]['recorded'])
        self.assertTrue(result['days'][2]['recorded'])
        self.assertFalse(result['days'][0]['recorded'])
        self.assertEqual(result['totals']['active_days'], 0)
        self.assertEqual(result['totals']['recorded_days'], 2)

    def test_coverage_gaps_ignore_findings_outside_the_day(self):
        save(self.path, [], {'source': 'pi', 'irrelevant_files': 5,
                             'out_of_window_oversized_lines': 2}, '2026-10-28', 'UTC')
        result = week_data(self.path, '2026-10-28', 'UTC')
        self.assertTrue(result['days'][2]['recorded'])
        self.assertFalse(result['days'][2]['coverage_gaps'])

    def test_timezone_boundary_and_sunday_end(self):
        start, end = bounds('2026-03-08', 'America/New_York')
        self.assertEqual(end - start, 23 * 3600)
        rows = [event(b'k', 'pi', 's', 'p', str(i), ts, 'assistant')
                for i, ts in enumerate([start - 1, start, end - 1, end])]
        save(self.path, rows, {'source': 'pi'}, '2026-03-08', 'America/New_York')
        result = week_data(self.path, '2026-03-08', 'America/New_York')
        self.assertEqual(result['days'][-1]['requests'], 2)
        self.assertEqual(result['totals']['requests'], 3)

    def test_legacy_coverage_is_only_used_for_its_own_day(self):
        db = connect(self.path)
        with db:
            db.execute('INSERT INTO imports VALUES (?,?,?)', ('pi', 'now', json.dumps({
                'source': 'pi', 'date': '2026-10-28', 'timezone': 'UTC'})))
        db.close()
        result = week_data(self.path, '2026-10-28', 'UTC')
        self.assertEqual(result['totals']['recorded_days'], 1)
        self.assertTrue(result['days'][2]['recorded'])
        self.assertFalse(any(day['recorded'] for day in week_data(self.path, '2026-10-28', 'Asia/Shanghai')['days']))

    def test_invalid_date_and_private_api(self):
        from threading import Thread
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        from worklog.web import make_server
        server = make_server(self.path, '2026-10-28', 'UTC', 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/week')
            self.assertEqual(error.exception.code, 401)
            with urlopen(Request(base + '/api/week?date=2026-10-28', headers={
                    'Authorization': 'Bearer ' + server.access_token})) as response:
                self.assertEqual(len(json.load(response)['days']), 7)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + '/api/week?date=not-a-date', headers={
                    'Authorization': 'Bearer ' + server.access_token}))
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
