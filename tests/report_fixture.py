"""Realistic report presentation fixture: synthetic results, no model or source reads."""
import copy
import json
import tempfile
from pathlib import Path

from weekly_fixture import populate

from worklog.core import bounds
from worklog.llm import validate_business_result, validate_result
from worklog.store import connect, salt
from worklog.tasks import TaskService
from worklog.web import make_server


def seed_drafts(path):
    start, _ = bounds('2026-10-28', 'Asia/Shanghai')
    texts = [
        '助手报告修复 OAuth 回调状态丢失：跳转前保存 state，回调读取校验后清理；13项定向测试通过，生产端到端回归尚未执行。',
        '尝试把订单筛选保存在路由参数，但清空筛选后翻页仍有2个回归用例失败，阻塞交付，未完成修复。',
        '仅讨论采用 Tauri 外壳复用前端与 Python 服务的桌面方案，没有编写桌面代码或制作安装包。',
    ]
    evidence = [{'id': f'e_synthetic_{index}', 'kind': 'assistant', 'text': text, 'ts': start + index,
                 'source': 'demo', 'session': 'synthetic', 'status': 'stop'} for index, text in enumerate(texts)]
    def entry(text, index):
        return {'text': text, 'evidence_ids': [evidence[index]['id']]}
    result = {'schema_version': 2, 'overview': entry('登录回调已由助手报告修复；筛选回归仍失败，桌面方案仅讨论。', 0), 'work_items': [
        {'title': '修复 OAuth 登录回调', 'status': 'reported_resolved', 'problem': entry('登录回调丢失 state 导致无法登录。', 0),
         'actions': [entry('跳转前保存 state，回调读取、校验并清理。', 0)], 'outcome': entry('助手报告登录状态丢失已修复。', 0),
         'verification': entry('13项定向测试通过；生产端到端回归未执行。', 0), 'remaining': [entry('补充生产端到端回归。', 0)]},
        {'title': '修复订单筛选翻页', 'status': 'blocked', 'problem': entry('订单翻页导致筛选条件重置。', 1),
         'actions': [entry('尝试将筛选条件保存在路由参数。', 1)], 'outcome': None,
         'verification': entry('仍有2个回归用例失败，当前方案不能交付。', 1), 'remaining': [entry('修复清空筛选后的翻页行为，再跑回归。', 1)]},
        {'title': '评估桌面打包方案', 'status': 'discussed', 'problem': entry('评估桌面封装，不进行实现。', 2),
         'actions': [entry('讨论 Tauri 外壳复用现有服务。', 2)], 'outcome': entry('仅形成方案方向，没有实现或安装包。', 2),
         'verification': None, 'remaining': [entry('后续评估进程管理与签名。', 2)]},
    ]}
    result['overview']['evidence_ids'] = [row['id'] for row in evidence]
    data = {'evidence': evidence, 'coverage': {'allow_excerpts': True, 'sampled': False,
            'selected_events': 3, 'total_activity_events': 3, 'excerpts_available': 3}}
    result = validate_result(json.dumps(result, ensure_ascii=False), {row['id'] for row in evidence})
    validate_business_result(result, data)
    db = connect(path)
    with db:
        task_id = 'f' * 32
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',
                   (task_id, '虚构：三项工作待审阅', '2026-10-28', '[]', start, None, 'open', start))
        for job_id, target, output in [('a' * 32, task_id, result), ('b' * 32, f'{20:032x}', copy.deepcopy(result))]:
            if target != task_id:
                output['work_items'] = output['work_items'][:1]
                output['overview'] = entry('助手补充登录验证结果：定向测试通过，生产回归待进行。', 0)
                output = validate_result(json.dumps({key: output[key] for key in ('schema_version', 'overview', 'work_items')}),
                                         {row['id'] for row in evidence})
            db.execute('''INSERT INTO summaries (id,task_id,mode,fingerprint,status,input_json,output_json,
                          provider,model,created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (job_id, target, 'stage', job_id, 'success', json.dumps(data, ensure_ascii=False),
                        json.dumps(output, ensure_ascii=False), 'synthetic', 'no-model-called', start + 100, start + 200))
    db.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='worklog-report-quality-') as directory:
        path = Path(directory) / 'report.sqlite3'
        salt(path)
        populate(path)
        seed_drafts(path)
        service = TaskService(path, None, zone='Asia/Shanghai')
        server = make_server(path, '2026-10-28', 'Asia/Shanghai', 0, service)
        print(f'http://127.0.0.1:{server.server_port}/#token={server.access_token}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.close()
