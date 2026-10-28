"""Explicit synthetic browser fixture; never invokes a real model."""
import argparse
import tempfile
from pathlib import Path

from worklog.cli import demo
from worklog.core import bounds
from worklog.tasks import TaskService
from worklog.web import make_server


class FixtureRunner:
    provider = 'synthetic'
    model = 'no-network-fixture'

    def __call__(self, data, mode, cancel):
        item = {'text': '虚构测试：梳理了活动线索，结果尚未验收。',
                'evidence_ids': [data['evidence'][0]['id']]}
        return {'overview': item, 'work': [item], 'results': [], 'blockers': [], 'next_steps': []}, {'tokens': 0}


def main():
    with tempfile.TemporaryDirectory(prefix='worklog-summary-ui-') as directory:
        db = Path(directory) / 'fixture.sqlite3'
        demo(argparse.Namespace(db=str(db), timezone='Asia/Shanghai'), '2026-10-28')
        service = TaskService(db, FixtureRunner(), zone='Asia/Shanghai',
                              clock=lambda: bounds('2026-10-28', 'Asia/Shanghai')[0] + 12 * 3600)
        server = make_server(db, '2026-10-28', 'Asia/Shanghai', 0, service)
        print(f'http://127.0.0.1:{server.server_port}/#token={server.access_token}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.close()


if __name__ == '__main__':
    main()
