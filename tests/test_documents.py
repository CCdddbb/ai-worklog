import copy
import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from worklog.documents import (
    build_document,
    clipboard_digest,
    document_markdown,
    period_days,
    report_document,
)
from worklog.review_focus import review_focus
from worklog.store import report_data, save
from worklog.web import make_server


def item(text):
    return {'text': text, 'evidence_ids': ['e_synthetic']}


def draft_result():
    return {'schema_version': 2, 'overview': item('助手报告登录问题修复，生产回归待进行。'), 'work_items': [{
        'title': '登录回调修复', 'status': 'reported_resolved', 'problem': item('登录丢失状态参数'),
        'actions': [item('增加跳转前保存与回调校验')], 'outcome': item('助手报告登录恢复'),
        'verification': None, 'remaining': [item('生产回归待进行')]}]}


def day_data():
    return {'date': '2026-10-28', 'timezone': 'UTC', 'task_records': [], 'task_reviews': [],
            'task_summaries': [], 'event_count': 0, 'request_count': 0, 'tokens': 0, 'sessions': [], 'coverage': {}}


class DocumentTests(unittest.TestCase):
    def document(self, data):
        return build_document([data], data['date'], data['date'], 'UTC', 'day')

    def test_clipboard_digest_is_paste_ready_and_shares_report_data(self):
        data = day_data()
        data['task_reviews'] = [{'id': 'one', 'title': '登录修复', 'status': 'confirmed',
                                 'outcome': '人工核对登录恢复', 'verification': '合成测试通过',
                                 'next_steps': '补生产回归', 'updated_at': 20}]
        doc = self.document(data)
        doc['automatic_work'] = {'stats': {'total': 1}, 'records': [
            {'title': '订单筛选翻页', 'status': 'blocked', 'archived': False,
             'work': {'work_items': [{'outcome': item('仍有2个回归用例失败')}]}}]}
        text = clipboard_digest(doc)
        self.assertIn('【AI 工作日报】', text)
        self.assertIn('人工核对登录恢复', text)
        self.assertIn('仍有2个回归用例失败', text)
        self.assertIn('补生产回归', text)
        for excluded in ('## 活动事实', '数据覆盖与口径', 'Token', 'LLM任务总结'):
            self.assertNotIn(excluded, text)
        self.assertIn('自动归档不等于人工验收', text)

    def test_manual_outcome_precedes_drafts_and_statistics(self):
        data = day_data()
        data['task_reviews'] = [{'id': 'one', 'title': '登录修复', 'status': 'confirmed', 'outcome': '人工核对登录恢复',
                                 'verification': '合成测试通过', 'next_steps': '补生产回归', 'updated_at': 20}]
        data['task_summaries'] = [{'task_id': 'one', 'title': '登录修复', 'result': draft_result(),
                                  'quality': {'ready': True}, 'draft_at': 10}]
        doc = self.document(data)
        text = document_markdown(doc)
        self.assertLess(text.index('人工核对登录恢复'), text.index('LLM任务总结'))
        self.assertLess(text.index('人工核对登录恢复'), text.index('活动事实'))
        self.assertNotIn('助手报告登录恢复', text)
        self.assertEqual(doc['totals']['needs_review'], 0)
        self.assertEqual(doc['totals']['attention'], 0)
        self.assertEqual(len(doc['records']), 1)
        self.assertEqual(doc['next_steps'][0]['text'], '补生产回归')

    def test_new_draft_is_not_silently_covered_by_old_review(self):
        data = day_data()
        data['task_reviews'] = [{'id': 'one', 'title': '登录修复', 'status': 'confirmed', 'outcome': '旧结论',
                                 'verification': '旧验证', 'next_steps': '', 'updated_at': 10}]
        data['task_summaries'] = [{'task_id': 'one', 'title': '登录修复', 'result': draft_result(),
                                  'quality': {'ready': True}, 'draft_at': 20}]
        doc = self.document(data)
        self.assertTrue(doc['records'][0]['focus']['updated_since_review'])
        self.assertEqual(doc['totals']['confirmed'], 1)
        self.assertEqual(doc['totals']['needs_review'], 1)
        self.assertIn('原结论不代表已确认新增内容', document_markdown(doc))
        self.assertEqual(doc['next_steps'], [])  # Don't resurrect AI plans a human removed.

    def test_unreviewed_resolved_claim_is_not_a_confirmed_outcome(self):
        data = day_data()
        data['task_summaries'] = [{'task_id': 'one', 'title': '登录修复', 'result': draft_result(), 'quality': {'ready': True}}]
        doc = self.document(data)
        self.assertEqual(doc['totals']['confirmed'], 0)
        self.assertEqual(doc['totals']['unreviewed'], 1)
        self.assertIn('verification_missing', [issue['code'] for issue in doc['records'][0]['focus']['issues']])
        self.assertIn('AI 建议', doc['next_steps'][0]['source'])

    def test_brief_and_full_preserve_results_but_only_full_expands_process(self):
        data = day_data()
        result = draft_result()
        result['work_items'][0]['status'] = 'blocked'
        result['work_items'][0]['verification'] = item('仍有2个回归用例失败')
        data['task_summaries'] = [{'task_id': 'one', 'title': '登录修复', 'result': result, 'quality': {'ready': True}}]
        doc = self.document(data)
        brief = document_markdown(doc)
        full = document_markdown(dict(doc, detail='full'))
        self.assertIn('仍有2个回归用例失败', brief)
        self.assertIn('仍有2个回归用例失败', full)
        self.assertNotIn('增加跳转前保存与回调校验', brief)
        self.assertIn('增加跳转前保存与回调校验', full)
        self.assertIn('助手报告登录恢复', brief)
        self.assertLess(len(brief), len(full))

    def test_manual_confirmation_without_verification_is_visible_not_invalidated(self):
        focus = review_focus({'status': 'confirmed', 'verification': '', 'updated_at': 20}, draft_result(), {'ready': True}, 10)
        self.assertFalse(focus['needs_review'])
        self.assertEqual(focus['issues'][0]['code'], 'manual_verification_missing')

    def test_blocked_discussion_and_sampling_have_targeted_prompts(self):
        result = draft_result()
        blocked = copy.deepcopy(result['work_items'][0])
        blocked.update(title='筛选条件回归', status='blocked', verification=item('仍有2项失败用例'))
        discussed = copy.deepcopy(blocked)
        discussed.update(title='桌面方案评估', status='discussed')
        result['work_items'] = [blocked, discussed]
        focus = review_focus(None, result, {'ready': True}, coverage={'sampled': True})
        self.assertEqual([issue['code'] for issue in focus['issues']], ['blocked', 'discussion_only', 'sampled'])
        self.assertEqual(focus['issues'][0]['evidence_ids'], ['e_synthetic'])

    def test_invalid_legacy_body_is_not_exported(self):
        data = day_data()
        data['task_summaries'] = [{'title': '旧版', 'quality': {'ready': False, 'message': '结构不合格'},
                                  'result': {'overview': item('不应出现的工具流水账')}}]
        text = document_markdown(self.document(data))
        self.assertIn('需要重新生成', text)
        self.assertNotIn('不应出现的工具流水账', text)

    def test_unreviewed_manual_task_and_no_data_are_not_hidden(self):
        data = day_data()
        data['task_records'] = [{'id': 'one', 'title': '线下沟通'}]
        doc = self.document(data)
        self.assertEqual(doc['totals']['tasks'], 1)
        self.assertEqual(doc['totals']['recorded_days'], 1)
        self.assertIn('线下沟通', document_markdown(doc))
        empty = self.document(day_data())
        self.assertEqual(empty['totals']['recorded_days'], 0)
        self.assertIn('本日期没有已导入', document_markdown(empty))

    def test_only_identical_manual_text_is_compacted_without_changing_counts(self):
        first = day_data()
        first['task_reviews'] = [{'id': 'one', 'title': '同一标题', 'status': 'confirmed',
            'outcome': '相同结论', 'verification': '相同验证', 'next_steps': '相同后续', 'updated_at': 1}]
        second = copy.deepcopy(first)
        second['date'] = '2026-10-29'
        second['task_reviews'][0].update(id='two', updated_at=2)
        doc = build_document([first, second], first['date'], second['date'], 'UTC', 'week')
        self.assertEqual(doc['totals']['confirmed'], 2)
        self.assertEqual(len(doc['records']), 2)
        self.assertEqual(len(doc['display_records']), 1)
        self.assertEqual(len(doc['display_records'][0]['members']), 2)
        self.assertEqual(len(doc['next_steps']), 1)
        self.assertEqual(doc['next_steps'][0]['record_count'], 2)
        self.assertIn('不是自动合并业务任务', document_markdown(doc))
        second['task_reviews'][0]['outcome'] = '不同结论'
        changed = build_document([first, second], first['date'], second['date'], 'UTC', 'week')
        self.assertEqual(len(changed['display_records']), 2)
        second['date'] = first['date']
        second['task_reviews'][0]['outcome'] = '相同结论'
        same_day = build_document([first, second], first['date'], second['date'], 'UTC', 'day')
        self.assertEqual(len(same_day['display_records']), 2)

    def test_task_review_focus_matches_report_flags(self):
        from report_fixture import seed_drafts
        from weekly_fixture import populate

        from worklog.store import salt
        from worklog.tasks import TaskService
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.sqlite3'
            salt(path)
            populate(path)
            seed_drafts(path)
            service = TaskService(path, None, zone='Asia/Shanghai')
            try:
                listing = service.list('2026-10-28')['tasks']
                candidate = next(task for task in listing if task['id'] == 'f' * 32)
                self.assertEqual(candidate['saved_summary_job_id'], 'a' * 32)
                self.assertEqual([issue['code'] for issue in candidate['review_focus']['issues']], ['blocked', 'discussion_only'])
                old = next(task for task in listing if task['id'] == f'{20:032x}')
                self.assertTrue(old['review_focus']['updated_since_review'])
            finally:
                service.close()

    def test_month_and_week_boundaries(self):
        self.assertEqual(tuple(str(d) for d in period_days('2024-02-28', 'month')), ('2024-02-01', '2024-02-29'))
        self.assertEqual(tuple(str(d) for d in period_days('2026-01-01', 'week')), ('2025-12-29', '2026-01-04'))
        with self.assertRaises(ValueError):
            period_days('2026-01-01', 'year')

    def test_coverage_from_other_days_is_not_reused_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.sqlite3'
            save(path, [], {'source': 'pi', 'omitted_files': 2}, '2026-10-27', 'UTC')
            save(path, [], {'source': 'pi'}, '2026-10-28', 'UTC')
            self.assertEqual(report_data(path, '2026-10-26', 'UTC')['coverage'], {})
            doc = report_document(path, '2026-10-28', 'UTC', 'week')
            self.assertEqual(doc['totals']['recorded_days'], 2)
            self.assertEqual(len(doc['coverage_notes']), 1)
            self.assertIn('2026-10-27', doc['coverage_notes'][0])

    def test_preview_is_authenticated_and_matches_export(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory) / 'report.sqlite3', '2026-10-28', 'UTC', 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                headers = {'Authorization': 'Bearer ' + server.access_token}
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/api/report-view')
                self.assertEqual(error.exception.code, 401)
                with urlopen(Request(base + '/api/report-view?period=week', headers=headers)) as response:
                    doc = json.load(response)
                with urlopen(Request(base + '/api/report?period=week', headers=headers)) as response:
                    self.assertEqual(doc['markdown'], response.read().decode())
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/report-view?period=year', headers=headers))
                self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
