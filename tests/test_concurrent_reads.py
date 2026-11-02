"""Offline dashboard concurrency regression; no source scans or model calls."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from weekly_fixture import populate

from worklog.store import salt
from worklog.tasks import TaskService
from worklog.web import make_server


class ConcurrentReadTests(unittest.TestCase):
    def test_day_week_and_report_reads_succeed_while_reviews_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'concurrent.sqlite3'
            salt(path)
            populate(path)
            service = TaskService(path, None, zone='Asia/Shanghai')
            server = make_server(path, '2026-10-28', 'Asia/Shanghai', 0, service)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            headers = {'Authorization': 'Bearer ' + server.access_token}
            def fetch(index):
                route = ['/api/day?date=2026-10-28', '/api/week?date=2026-10-28',
                         '/api/report-view?date=2026-10-28&period=week'][index % 3]
                with urlopen(Request(base + route, headers=headers), timeout=15) as response:
                    self.assertEqual(response.status, 200)
                    return json.load(response)
            def write_reviews():
                for index in range(20):
                    service.review('0' * 32, 'confirmed', f'合成并发保存 {index}',
                                   verification='仅用于本地并发测试', commit_ids=[])
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    writer = pool.submit(write_reviews)
                    results = list(pool.map(fetch, range(24)))
                    writer.result()
                self.assertEqual(len(results), 24)
                for index, result in enumerate(results):
                    if index % 3 == 1:
                        self.assertEqual(len(result['days']), 7)
                    if index % 3 == 2:
                        self.assertEqual(result['totals']['tasks'], 9)
                        self.assertIn('AI 工作周报', result['markdown'])
                self.assertEqual(service.list('2026-10-26')['tasks'][0]['review']['outcome'], '合成并发保存 19')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                service.close()


if __name__ == '__main__':
    unittest.main()
