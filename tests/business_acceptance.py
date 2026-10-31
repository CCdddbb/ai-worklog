"""Opt-in real-model acceptance with synthetic multi-topic conversations only.
Run: PYTHONPATH=. python3 tests/business_acceptance.py --live
Without --live this only checks the local evidence selection; no model is called.
"""
import argparse
import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from worklog.adapters import collect
from worklog.codebuddy import CodeBuddyRunner
from worklog.core import bounds
from worklog.evidence import snapshot
from worklog.reporting import business_lines

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


def scenario_material(root):
    counter = 0
    def write(name, entries):
        nonlocal counter
        records = [{'type': 'session', 'id': name, 'cwd': '/private/synthetic-project'},
                   {'type': 'model_change', 'provider': 'synthetic', 'modelId': 'fixture'}]
        for role, text in entries:
            counter += 1
            records.append({'type': 'message', 'id': f'synthetic-{counter}',
                            'timestamp': datetime.fromtimestamp(START + counter, timezone.utc).isoformat(),
                            'message': {'role': role, 'content': text,
                                        'stopReason': 'toolUse' if role == 'assistant' and not isinstance(text, str) else 'stop'}})
        (root / (name + '.jsonl')).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records))
    login = [('user', '虚构工作：OAuth 登录回调丢失 state 参数，导致用户无法完成登录，需要修复。'),
             ('assistant', '先提出把 state 拼到登录 URL 的方案，尚未验证。'),
             ('user', '实际回归仍失败，只拼 URL 没解决，回调页依然取不到 state。')]
    for _ in range(70):
        login.extend([('assistant', [{'type': 'toolCall', 'name': 'Bash', 'arguments': {'secret': 'PRIVATE_TOOL_ARGUMENT'}}]),
                      ('toolResult', 'PRIVATE_TOOL_OUTPUT 不可发送')])
    login.append(('assistant', '排查 OAuth 登录回调流程。\n' + '背景排查说明。' * 700 +
                  '\n最终结果：助手报告放弃只拼 URL 的方案，改为跳转前保存 state，在回调页读取并校验后再清理，修复状态参数丢失导致的登录失败。新增的13项定向测试通过；尚未进行生产环境端到端回归，不能视为已上线或业务验收。'))
    write('login', login)
    write('pagination-and-app', [
        ('user', '虚构工作：订单列表切换页码后筛选条件被重置，请修复并补回归测试。'),
        ('assistant', '助手报告尝试把筛选条件保存在路由参数中，但清空筛选再翻页仍有2个回归用例失败，问题未解决，不能算完成。'),
        ('user', '先解决这两个失败用例，不要把当前方案写成已经修复。'),
        ('assistant', '当前只定位到筛选重置逻辑，没有完成进一步修复；这两个失败用例仍阻塞交付。'),
        ('user', '另外讨论工作台将来能否打包成本地桌面 App，先评估方案，不进行实现。'),
        ('assistant', '讨论了 Tauri 外壳复用前端、Python 服务和 SQLite 的方案；仍需处理进程管理、数据迁移、签名与安装包。此轮仅讨论，没有编写桌面端代码，也没有制作安装包。')])
    key = b's' * 32
    rows, _ = collect(root, 'pi', key)
    return snapshot({'title': '虚构多会话工作复盘'}, rows, {'pi': root}, key, True)


def check_result(result):
    groups = result['work_items']
    def find(word):
        return next(group for group in groups if word in group['title'] + group['problem']['text'])
    login = find('登录')
    paging = find('筛选')
    app = find('桌面')
    assert login['status'] == 'reported_resolved' and login['outcome'] and login['verification']
    assert paging['status'] in ('blocked', 'in_progress')
    assert paging['verification'] and '失败' in paging['verification']['text'], 'Known failing tests must be reported under verification.'
    assert app['status'] == 'discussed', 'A proposal must not be marked as implementation.'
    if app['outcome']:
        assert any(word in app['outcome']['text'] for word in ('方案', '讨论', '评估'))
        assert not any(word in app['outcome']['text'] for word in ('已实现桌面', '已打包', '已制作安装包'))
    assert '生产' in json.dumps(login, ensure_ascii=False)
    assert 'PRIVATE_TOOL' not in json.dumps(result, ensure_ascii=False)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly call CodeBuddy with synthetic evidence; may consume credits')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='worklog-business-acceptance-') as directory:
        data = scenario_material(Path(directory))
        assert data['readiness']['ready']
        assert data['coverage']['tool_events_excluded'] == 70
        assert '修复状态参数丢失' in json.dumps(data, ensure_ascii=False)
        assert 'PRIVATE_TOOL' not in json.dumps(data, ensure_ascii=False)
        if not args.live:
            print('Synthetic evidence acceptance passed; no model called.')
            return
        result, usage = CodeBuddyRunner(timeout=180)(data, 'stage', threading.Event())
        out = Path('data/business-quality-verification')
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifacts = {'evidence.json': data, 'result.json': result, 'usage.json': usage}
        for name, value in artifacts.items():
            path = out / name
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
            path.chmod(0o600)
        report = '# DeepSeek 业务总结验收（全部为虚构材料）\n\n' + result['overview']['text'] + '\n\n' + '\n'.join(business_lines(result))
        (out / 'report.md').write_text(report)
        (out / 'report.md').chmod(0o600)
        print(report)
        print('\nSynthetic model-call tokens:', usage['tokens'])
        check_result(result)
        print('Verified: resolved login, blocked filtering regression, proposal-only desktop app.')


if __name__ == '__main__':
    main()
