"""Synthetic automatic-workbench server. No real provider or user logs."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from test_automation import AutomaticRunner

from worklog.adapters import collect
from worklog.core import bounds
from worklog.store import salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-10-28'


def main():
    start, _ = bounds(DAY, 'UTC')
    with tempfile.TemporaryDirectory(prefix='worklog-automatic-ui-') as directory:
        root = Path(directory)
        logs = root / 'logs'
        logs.mkdir()
        db = root / 'fixture.sqlite3'
        for name, user, answer in [('login', '修复登录回调丢失参数导致登录失败的问题。', '助手报告已保存并校验参数，定向验证通过。'),
                                   ('design', '评估桌面工作台方案，暂不进行实现。', '讨论了桌面应用方案，尚未制作安装包。')]:
            rows = [{'type': 'session', 'id': name, 'cwd': '/private/fictional-project'}]
            for index, (role, body) in enumerate([('user', user), ('toolResult', 'PRIVATE_TOOL_OUTPUT'), ('assistant', answer)]):
                rows.append({'type': 'message', 'id': name + str(index),
                             'timestamp': datetime.fromtimestamp(start + 9 * 3600 + index * 120, timezone.utc).isoformat(),
                             'message': {'role': role, 'content': body, 'stopReason': 'stop', 'provider': 'synthetic', 'model': 'fixture'}})
            (logs / (name + '.jsonl')).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        events, coverage = collect(logs, 'pi', salt(db))
        save(db, events, dict(coverage, synthetic=True), DAY, 'UTC')
        service = TaskService(db, AutomaticRunner(), {'pi': logs}, zone='UTC', clock=lambda: start + 12 * 3600)
        server = make_server(db, DAY, 'UTC', 0, service)
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
