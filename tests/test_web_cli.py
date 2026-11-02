import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog.cli import main
from worklog.store import connect
from worklog.web import make_server

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'data.sqlite3'
        connect(self.db).close()
        self.server = make_server(self.db, '2026-01-02', 'UTC', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path, headers=None, auth=True):
        values = {'Authorization': 'Bearer ' + self.server.access_token} if auth else {}
        values.update(headers or {})
        request = urllib.request.Request(self.base + path, headers=values)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_loopback_and_token_required_for_private_api(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.get('/api/day', auth=False)[0], 401)
        self.assertEqual(self.get('/api/day', {'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.get('/api/day')[0], 200)

    def test_origin_and_host_restrictions(self):
        self.assertEqual(self.get('/api/day', {'Origin': 'https://attacker.invalid'})[0], 403)
        self.assertEqual(self.get('/api/day', {'Host': 'attacker.invalid'})[0], 403)

    def test_no_arbitrary_paths_or_bad_dates(self):
        self.assertEqual(self.get('/api/day?date=2026-02-30')[0], 400)
        self.assertEqual(self.get('/api/day?date=../../etc/passwd')[0], 400)
        self.assertEqual(self.get('/../../etc/passwd')[0], 404)
        self.assertEqual(self.get('/identity.key')[0], 404)

    def test_security_headers_and_static_page_has_no_token(self):
        status, headers, body = self.get('/', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn(self.server.access_token.encode(), body)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_api_returns_data_and_markdown(self):
        value = json.loads(self.get('/api/day')[2])
        self.assertEqual(value['date'], '2026-01-02')
        self.assertEqual(value['event_count'], 0)
        self.assertNotIn(str(self.db), json.dumps(value))
        self.assertIn('本日期没有已导入', self.get('/api/report')[2].decode())

    def test_workbench_styles_are_local_public_assets_without_private_data(self):
        status, headers, body = self.get('/workbench.css', auth=False)
        self.assertEqual(status, 200)
        self.assertIn('text/css', headers['Content-Type'])
        self.assertIn('button.button-ai', body.decode())
        self.assertNotIn(str(self.db), body.decode())

    def test_frontend_uses_text_nodes_and_no_third_party_scripts(self):
        script = (ROOT / 'static/app.js').read_text()
        self.assertNotIn('innerHTML', script)
        self.assertNotIn('https://', script)
        self.assertNotIn('localStorage', script)
        self.assertIn('textContent', script)


class CliTests(unittest.TestCase):
    def test_demo_report_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'demo.sqlite3')
            output = Path(directory) / 'report.md'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['demo', '--db', db, '--date', '2026-01-02']), 0)
                self.assertEqual(main(['report', '--db', db, '--date', '2026-01-02', '--out', str(output)]), 0)
            self.assertTrue(output.exists())
            with self.assertRaises(FileExistsError):
                main(['report', '--db', db, '--out', str(output)])
            with self.assertRaises(ValueError):
                main(['demo', '--db', db])

    def test_import_requires_explicit_source_and_limits(self):
        with self.assertRaises(ValueError):
            main(['import'])
        with self.assertRaises(ValueError):
            main(['import', '--max-files', '0'])

    def test_title_purge_only_changes_owned_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'demo.sqlite3')
            with contextlib.redirect_stdout(io.StringIO()):
                main(['demo', '--db', db, '--date', '2026-01-02'])
                main(['purge', '--db', db, '--titles-only'])
            connection = connect(db)
            try:
                self.assertEqual(connection.execute("SELECT count(*) FROM events WHERE title != ''").fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT count(*) FROM events').fetchone()[0], 8)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
