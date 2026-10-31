"""Acceptance regressions: summarize actual problems/results, never empty tool metadata."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from worklog.adapters import collect
from worklog.core import bounds
from worklog.evidence import (
    EvidenceUnavailable,
    business_excerpt,
    evidence_readiness,
    snapshot,
    summary_quality,
)
from worklog.llm import SummaryError, validate_business_result, validate_result
from worklog.reporting import business_lines
from worklog.store import connect, markdown, report_data, salt, save
from worklog.tasks import TaskService
from worklog.web import make_server

DAY = '2026-01-02'
START, _ = bounds(DAY, 'UTC')


def structured_result(data):
    user = next(e for e in data['evidence'] if e['kind'] == 'user' and e['text'])
    assistant = next(e for e in data['evidence'] if e['kind'] == 'assistant' and e['text'])
    def item(text, evidence):
        return {'text': text, 'evidence_ids': [evidence['id']]}
    return {'schema_version': 2, 'overview': item('修复登录回调丢失参数的问题；助手报告定向测试通过，生产回归待补。', assistant),
            'work_items': [{'title': '修复登录回调参数丢失', 'problem': item('登录跳转后 state 参数丢失，导致回调失败。', user),
                            'actions': [item('助手报告补齐参数传递并新增回归用例。', assistant)],
                            'outcome': item('助手报告回调参数可以保留，原登录失败问题已修复。', assistant),
                            'verification': item('助手报告定向测试通过，未提供生产回归证据。', assistant),
                            'remaining': [item('还需要生产环境回归验证。', assistant)], 'status': 'reported_resolved'}]}


class BusinessSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'local.sqlite3'
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.key = salt(self.db)
        self.counter = 0
        self.path = self.logs / 'session.jsonl'
        self.path.write_text(json.dumps({'type': 'session', 'id': 'business', 'cwd': '/private/customer'}) + '\n')

    def tearDown(self):
        self.temp.cleanup()

    def message(self, role, text, status='stop', **extra):
        from datetime import datetime, timezone
        self.counter += 1
        data = {'type': 'message', 'id': f'event-{self.counter}',
                'timestamp': datetime.fromtimestamp(START + self.counter, timezone.utc).isoformat(),
                'message': {'role': role, 'content': text, 'stopReason': status, **extra}}
        with self.path.open('a') as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + '\n')

    def material(self, consent=True):
        rows, coverage = collect(self.logs, 'pi', self.key)
        save(self.db, rows, coverage, DAY, 'UTC')
        return rows, snapshot({'title': '多项功能排查'}, rows, {'pi': self.logs}, self.key, consent)

    def conversation(self):
        self.message('user', '登录跳转后 state 参数丢失导致回调失败，请修复。')
        self.message('assistant', '助手报告补齐回调参数传递并新增回归用例；定向测试通过，生产回归待补。')

    def test_no_consent_means_no_model_job_no_fake_success_or_task_completion(self):
        self.conversation()
        rows, data = self.material(False)
        self.assertFalse(data['readiness']['ready'])
        runner = Mock(provider='fake', model='no-call')
        service = TaskService(self.db, runner, {'pi': self.logs}, False, zone='UTC', clock=lambda: START + 1000)
        try:
            task = service.create('登录回调修复', [rows[0]['session']], DAY)
            with self.assertRaisesRegex(EvidenceUnavailable, '尚未授权正文'):
                service.request(task['id'], 'final')
            self.assertEqual(service._task(task['id'])['status'], 'open')
            db = connect(self.db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 0)
            db.close()
            runner.assert_not_called()
        finally:
            service.close()

    def test_http_rejects_empty_evidence_with_actionable_409(self):
        self.conversation()
        rows, _ = self.material(False)
        runner = Mock(provider='fake', model='no-call')
        service = TaskService(self.db, runner, {'pi': self.logs}, False, zone='UTC', clock=lambda: START + 1000)
        task = service.create('回调问题', [rows[0]['session']], DAY)
        server = make_server(self.db, DAY, 'UTC', 0, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            request = urllib.request.Request(base + '/api/summarize',
                data=json.dumps({'task_id': task['id']}).encode(),
                headers={'Authorization': 'Bearer ' + server.access_token, 'Origin': base,
                         'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 409)
            value = json.loads(caught.exception.read())
            self.assertEqual(value['error'], 'excerpt_consent_required')
            self.assertIn('授权', value['hint'])
            runner.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()

    def test_tool_noise_cannot_replace_user_problem_or_assistant_conclusion(self):
        self.conversation()
        for _ in range(300):
            self.message('assistant', [{'type': 'toolCall', 'name': 'Bash', 'arguments': {'secret': 'PRIVATE_ARGUMENT'}}], 'toolUse')
            self.message('toolResult', 'PRIVATE_TOOL_OUTPUT token=PRIVATE_TOOL_SECRET')
        self.message('user', '再解决列表翻页后筛选条件丢失。')
        self.message('assistant', '助手报告统一分页和筛选状态，切换页码仍保留筛选；定向用例通过，未发布。')
        _, data = self.material()
        combined = json.dumps(data, ensure_ascii=False)
        for expected in ('state 参数丢失', '回调参数传递', '翻页后筛选', '仍保留筛选'):
            self.assertIn(expected, combined)
        for private in ('PRIVATE_ARGUMENT', 'PRIVATE_TOOL_OUTPUT', 'PRIVATE_TOOL_SECRET'):
            self.assertNotIn(private, combined)
        self.assertTrue(all(e['kind'] in ('user', 'assistant') and e['status'] != 'toolUse' for e in data['evidence']))
        self.assertEqual(data['coverage']['tool_events_excluded'], 300)

    def test_long_final_response_keeps_result_tail_without_leaking_code_or_reasoning(self):
        self.message('user', '修复重复刷新导致刷新令牌失效的问题。')
        long = ('排查刷新令牌并发竞争。\n' + '背景分析说明。' * 1000 +
                '\n```text\nPRIVATE_CODE token=PRIVATE_CODE_SECRET\n```\n' +
                '最终结论：助手报告增加单次刷新锁，避免重复请求覆盖新令牌；并发用例通过，生产验证待补。')
        self.message('assistant', [{'type': 'thinking', 'thinking': 'PRIVATE_THINKING'}, {'type': 'text', 'text': long}])
        _, data = self.material()
        combined = json.dumps(data, ensure_ascii=False)
        self.assertIn('增加单次刷新锁', combined)
        self.assertIn('生产验证待补', combined)
        self.assertNotIn('[长消息已省略]', combined)
        for private in ('PRIVATE_CODE', 'PRIVATE_CODE_SECRET', 'PRIVATE_THINKING'):
            self.assertNotIn(private, combined)
        self.assertLess(len(combined.encode()), 48000)

    def test_many_long_messages_fit_the_total_byte_budget(self):
        for index in range(16):
            self.message('user', f'第{index}项问题：' + '页面筛选需要保留。' * 600)
            self.message('assistant', '助手报告分析筛选状态。' * 600 + f'结论{index}：已定位重置逻辑，仍需补回归。')
        _, data = self.material()
        self.assertTrue(data['readiness']['ready'])
        self.assertLess(len(json.dumps(data, ensure_ascii=False).encode()), 48000)
        self.assertTrue(any('结论' in entry['text'] for entry in data['evidence']))

    def test_late_user_rejection_is_kept_after_reported_fix(self):
        self.conversation()
        self.message('user', '实际回归仍然失败，不能说问题已经解决。')
        _, data = self.material()
        self.assertIn('实际回归仍然失败', json.dumps(data, ensure_ascii=False))

    def test_provider_inherited_from_model_change_resolves_response_excerpt(self):
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': 'model_change', 'provider': 'synthetic', 'modelId': 'model'}) + '\n')
        self.message('user', '排查回调参数传递遗漏。')
        self.message('assistant', '助手报告已补齐参数，等待回归验证。', responseId='synthetic-response')
        _, data = self.material()
        self.assertIn('已补齐参数', json.dumps(data, ensure_ascii=False))

    def test_full_business_contract_and_human_readable_report(self):
        self.conversation()
        _, data = self.material()
        raw = structured_result(data)
        result = validate_result(json.dumps(raw, ensure_ascii=False), {e['id'] for e in data['evidence']})
        validate_business_result(result, data)
        text = '\n'.join(business_lines(result))
        for expected in ('问题/目标', '处理措施', '结果/解决的问题', '验证情况', '未完成/下一步', 'state 参数丢失', '生产环境回归'):
            self.assertIn(expected, text)
        for metadata in ('调用Read', '调用Bash', '工具观察记录'):
            self.assertNotIn(metadata, text)
        self.assertEqual(validate_result(json.dumps(result), {e['id'] for e in data['evidence']}), result)

    def test_no_result_cannot_claim_resolved_and_tool_only_citations_rejected(self):
        self.conversation()
        _, data = self.material()
        raw = structured_result(data)
        raw['work_items'][0]['outcome'] = None
        with self.assertRaisesRegex(SummaryError, 'missing_outcome'):
            validate_result(json.dumps(raw), {e['id'] for e in data['evidence']})
        raw['work_items'][0]['status'] = 'in_progress'
        result = validate_result(json.dumps(raw), {e['id'] for e in data['evidence']})
        validate_business_result(result, data)
        data['evidence'][0]['text'] = ''
        with self.assertRaisesRegex(SummaryError, 'non_content_citation'):
            validate_business_result(result, data)

    def test_middle_business_request_and_response_survive_stratified_sampling(self):
        for index in range(40):
            self.message('user', f'第{index}项需求：修复列表查询条件在刷新后丢失的问题。')
            self.message('assistant', f'第{index}项结果：助手报告将查询条件保存在页面状态中，仍待回归。')
        _, data = self.material()
        self.assertEqual(len(data['evidence']), 24)
        combined = json.dumps(data, ensure_ascii=False)
        # The midpoint request/response pair, not merely the first and last messages.
        self.assertIn('第19项需求', combined)
        self.assertIn('第19项结果', combined)

    def test_known_failed_tests_cannot_be_lost_from_verification(self):
        self.conversation()
        _, data = self.material()
        raw = structured_result(data)
        group = raw['work_items'][0]
        group['actions'][0]['text'] = '尝试修复列表筛选，但仍有2个回归用例失败。'
        group['status'] = 'blocked'
        group['outcome'] = None
        group['verification'] = None
        result = validate_result(json.dumps(raw), {e['id'] for e in data['evidence']})
        with self.assertRaisesRegex(SummaryError, 'missing_verification'):
            validate_business_result(result, data)

    def test_old_empty_drafts_are_marked_invalid_not_shown_as_successful_work(self):
        old = {'overview': {'text': '调用了多次工具', 'evidence_ids': ['e_old']}}
        quality = summary_quality({'coverage': {'allow_excerpts': False}, 'evidence': []}, old)
        self.assertTrue(quality['needs_regeneration'])
        self.assertFalse(quality['ready'])
        report = report_data(self.db, DAY, 'UTC')
        report['task_summaries'] = [{'title': '旧任务', 'quality': quality, 'result': old}]
        text = markdown(report)
        self.assertIn('需要重新生成', text)
        self.assertNotIn('调用了多次工具', text)
        self.assertLess(text.index('LLM任务总结'), text.index('活动事实'))

    def test_only_placeholders_and_tool_narration_are_not_business_evidence(self):
        for body in ('[长消息已省略]', '[代码块已省略]', '调用Bash和Read工具，执行成功。'):
            data = {'coverage': {'allow_excerpts': True}, 'evidence': [{'kind': 'assistant', 'text': body}]}
            self.assertFalse(evidence_readiness(data)['ready'])
        clean = business_excerpt('问题是回调错误。 password=PRIVATE_VALUE\n最后报告已补齐参数。')
        self.assertIn('最后报告已补齐参数', clean)
        self.assertNotIn('PRIVATE_VALUE', clean)


if __name__ == '__main__':
    unittest.main()
