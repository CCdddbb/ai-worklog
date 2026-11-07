"""Compact project timeline: read-only projection plus on-demand record detail."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from worklog.ledger import record_detail
from worklog.store import connect
from worklog.timeline import SUMMARY_LIMIT, timeline_data
from worklog.web import make_server

DAY = '2026-01-02'
ROOT = Path(__file__).resolve().parents[1]


def insert_record(db, record_id, projects, outcome='登录回调已修复并验证', status='reported_resolved', first=3600):
    work = {
        'title': '登录问题',
        'problem': {'text': '登录回调丢失参数'},
        'actions': [{'text': '补齐参数校验'}],
        'outcome': {'text': outcome},
        'verification': {'text': '本地集成测试通过'},
        'remaining': [],
        'status': status,
    }
    db.execute('''INSERT INTO work_records
        (id,day,source_task_id,source_job_id,title,category,category_override,status,
         group_json,projects_json,anchors_json,intervals_json,first_ts,last_ts,
         active_seconds,archived_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (record_id, DAY, 'task', 'job', '修复登录回调', 'bugfix', 0, status,
         json.dumps(work, ensure_ascii=False), json.dumps(projects), '[]', '[]',
         first, first + 400, 400, None, first, first))


class TimelineDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'timeline.sqlite3'
        db = connect(self.db)
        with db:
            db.execute('INSERT INTO projects (id,name) VALUES (?,?)', ('p1', '示例项目'))
            db.execute('INSERT INTO project_aliases (alias,project) VALUES (?,?)', ('p1-alias', 'p1'))
            insert_record(db, 'a' * 32, ['p1-alias'], outcome='结' * (SUMMARY_LIMIT + 40))
            insert_record(db, 'b' * 32, ['p2'], outcome='', status='blocked', first=7200)
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_projection_is_grouped_clipped_and_free_of_full_text(self):
        value = timeline_data(self.db, DAY, 'UTC')
        self.assertEqual(value['project_count'], 2)
        self.assertEqual(len(value['nodes']), 2)
        self.assertEqual(value['stats']['total'], 2)
        self.assertEqual(value['stats']['blocked'], 1)
        names = {item['id']: item['name'] for item in value['projects']}
        self.assertEqual(names['p1-alias'], '示例项目')
        self.assertEqual(names['p2'], '未识别名称的项目')
        first = next(node for node in value['nodes'] if node['id'] == 'a' * 32)
        self.assertEqual(len(first['summary']), SUMMARY_LIMIT + 1)
        self.assertTrue(first['summary'].endswith('…'))
        self.assertNotIn('work', first)
        self.assertNotIn('problem', first)
        self.assertNotIn('verification', first)
        self.assertNotIn('结' * (SUMMARY_LIMIT + 40), json.dumps(value, ensure_ascii=False))
        self.assertIn('圆点', value['note'])
        self.assertIn('不等于工时', value['note'])
        groups = {item['id']: item['node_ids'] for item in value['projects']}
        self.assertEqual(groups['p1-alias'], ['a' * 32])
        self.assertEqual(groups['p2'], ['b' * 32])

    def test_scope_filters_reach_the_projection(self):
        self.assertEqual(timeline_data(self.db, DAY, 'UTC', state='blocked')['stats']['blocked'], 1)
        self.assertEqual(len(timeline_data(self.db, DAY, 'UTC', state='blocked')['nodes']), 1)
        self.assertEqual(len(timeline_data(self.db, DAY, 'UTC', category='docs')['nodes']), 0)
        with self.assertRaises(ValueError):
            timeline_data(self.db, DAY, 'UTC', category='unknown')

    def test_full_text_only_comes_from_the_detail_reader(self):
        value = record_detail(self.db, 'a' * 32)
        self.assertEqual(value['record']['work']['outcome']['text'], '结' * (SUMMARY_LIMIT + 40))
        self.assertEqual(value['record']['work']['verification']['text'], '本地集成测试通过')
        for bad in ('', '../etc/passwd', 'z' * 32, 123):
            with self.assertRaises(ValueError):
                record_detail(self.db, bad)


class TimelineEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'timeline.sqlite3'
        db = connect(self.db)
        with db:
            insert_record(db, 'c' * 32, ['p1'])
        db.close()
        self.server = make_server(self.db, DAY, 'UTC', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path, auth=True):
        headers = {'Authorization': 'Bearer ' + self.server.access_token} if auth else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_timeline_and_detail_endpoints_are_private_and_validated(self):
        self.assertEqual(self.get('/api/work-timeline', auth=False)[0], 401)
        self.assertEqual(self.get('/api/work-record-detail?id=' + 'c' * 32, auth=False)[0], 401)
        status, _, body = self.get(f'/api/work-timeline?date={DAY}')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload['nodes']), 1)
        self.assertNotIn('work', payload['nodes'][0])
        self.assertEqual(self.get('/api/work-timeline?date=2026-02-30')[0], 400)
        self.assertEqual(self.get('/api/work-timeline?date=' + DAY + '&state=nope')[0], 400)
        self.assertEqual(self.get('/api/work-record-detail?id=nope')[0], 400)
        self.assertEqual(self.get('/api/work-record-detail?id=' + 'd' * 32)[0], 400)
        status, _, body = self.get('/api/work-record-detail?id=' + 'c' * 32)
        self.assertEqual(status, 200)
        self.assertIn('登录回调已修复', json.loads(body)['record']['work']['outcome']['text'])

    def test_static_timeline_script_is_local_and_safe(self):
        status, headers, body = self.get('/timeline.js', auth=False)
        self.assertEqual(status, 200)
        self.assertIn('text/javascript', headers['Content-Type'])
        script = body.decode()
        self.assertIn('renderWorkTimeline', script)
        self.assertIn('/api/work-record-detail', script)
        self.assertNotIn('innerHTML', script)
        self.assertNotIn('https://', script)
        self.assertNotIn('localStorage', script)
        page = self.get('/', auth=False)[2].decode()
        self.assertIn('/timeline.js', page)
        self.assertIn('/ledger.js', page)
        self.assertLess(page.index('/timeline.js'), page.index('/ledger.js'))
