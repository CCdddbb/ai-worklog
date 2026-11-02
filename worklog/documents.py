"""Outcome-first report documents shared by preview and Markdown export."""
import calendar
import json
from datetime import date, timedelta

from .reporting import STATUS_LABELS, business_lines, safe_text
from .review_focus import review_focus

LABELS = {'confirmed': '人工已确认', 'in_progress': '进行中', 'blocked': '受阻', 'unreviewed': '待审阅'}


def period_days(day, period):
    selected = date.fromisoformat(day)
    if period == 'day':
        return selected, selected
    if period == 'week':
        first = selected - timedelta(days=selected.weekday())
        return first, first + timedelta(days=6)
    if period == 'month':
        return selected.replace(day=1), selected.replace(day=calendar.monthrange(selected.year, selected.month)[1])
    raise ValueError('Invalid report period')


def compact_records(records):
    """Collapse only identical saved text, never infer that two dates are one task."""
    groups, keyed = [], {}
    for record in records:
        review = record['review']
        key = None
        if review and not record['focus']['updated_since_review']:
            key = json.dumps([record['title'], record['status'],
                [review.get(field) for field in ('outcome', 'verification', 'learnings', 'next_steps', 'commits')],
                record['draft']], sort_keys=True, ensure_ascii=False)
        if key is not None and key in keyed and record['date'] not in keyed[key]['dates']:
            group = keyed[key]
            group['members'].append({'id': record['id'], 'date': record['date']})
            if record['date'] not in group['dates']:
                group['dates'].append(record['date'])
        else:
            group = dict(record, members=[{'id': record['id'], 'date': record['date']}], dates=[record['date']])
            groups.append(group)
            if key is not None:
                keyed[key] = group
    return groups


def date_label(record):
    return '、'.join(record.get('dates', [record['date']]))


def build_document(days, first, last, zone, period):
    records, timeline, actions = [], [], []
    sessions = set()
    totals = dict.fromkeys(['confirmed', 'in_progress', 'blocked', 'unreviewed', 'needs_review',
                          'attention', 'tokens', 'requests', 'events', 'recorded_days'], 0)
    coverage_notes = []
    for data in days:
        day = data['date']
        reviews = {item['id']: item for item in data.get('task_reviews', [])}
        drafts = {item.get('task_id', f'draft-{i}'): item for i, item in enumerate(data.get('task_summaries', []))}
        tasks = {item['id']: item for item in data.get('task_records', [])}
        for task_id, item in reviews.items():
            tasks.setdefault(task_id, item)
        for task_id, item in drafts.items():
            tasks.setdefault(task_id, item)
        states = dict.fromkeys(['confirmed', 'in_progress', 'blocked', 'unreviewed'], 0)
        for task_id, task in tasks.items():
            review, draft = reviews.get(task_id), drafts.get(task_id)
            quality = draft.get('quality', {}) if draft else None
            valid = bool(draft and quality and quality.get('ready'))
            result = draft.get('result') if valid else None
            focus = review_focus(review, result, quality,
                                 draft.get('draft_at') if draft else None,
                                 draft.get('coverage') if draft else None)
            status = review['status'] if review else 'unreviewed'
            record = {'id': task_id, 'date': day, 'title': task['title'], 'status': status,
                      'review': review, 'draft': result, 'draft_quality': quality,
                      'focus': focus, 'evidence': draft.get('evidence', []) if valid else [],
                      'draft_model': (draft.get('provider', '') + '/' + draft.get('model', '')) if valid else ''}
            records.append(record)
            states[status] += 1
            totals[status] += 1
            totals['needs_review'] += focus['needs_review']
            totals['attention'] += bool(focus['issues'])
            if review and review.get('next_steps', '').strip():
                actions.append({'task_id': task_id, 'date': day, 'title': task['title'],
                                'text': review['next_steps'], 'source': '人工记录'})
            elif not review and result:
                for group in result.get('work_items', []):
                    actions.extend({'task_id': task_id, 'date': day, 'title': group['title'],
                                    'text': entry['text'], 'source': 'AI 建议，待确认'} for entry in group['remaining'])
        recorded = bool(data.get('event_count') or tasks or data.get('coverage'))
        totals['recorded_days'] += recorded
        for key, source in [('tokens', 'tokens'), ('requests', 'request_count'), ('events', 'event_count')]:
            totals[key] += data.get(source, 0)
        sessions.update(item['session'] for item in data.get('sessions', []))
        timeline.append({'date': day, 'tasks': len(tasks), 'states': states,
                         'tokens': data.get('tokens', 0), 'requests': data.get('request_count', 0), 'recorded': recorded})
        for source, coverage in data.get('coverage', {}).items():
            gaps = sum(coverage.get(key, 0) for key in ('omitted_files', 'oversized_files', 'oversized_lines',
                'truncated_files', 'partial_lines', 'malformed_lines', 'unreadable_files', 'invalid_timestamps'))
            if gaps or coverage.get('more_commits'):
                coverage_notes.append(f'{day} · {source}：采集存在缺口，不能视为完整工作记录。')
    action_groups = {}
    for action in actions:
        key = (action['title'], action['text'], action['source'])
        if key in action_groups:
            group = action_groups[key]
            if action['date'] not in group['dates']:
                group['dates'].append(action['date'])
            group['record_count'] += 1
        else:
            action_groups[key] = dict(action, dates=[action['date']], record_count=1)
    actions = list(action_groups.values())
    totals['tasks'] = len(records)
    totals['sessions'] = len(sessions)
    title = {'day': 'AI 工作日报', 'week': 'AI 工作周报', 'month': 'AI 工作月报'}[period]
    return {'title': title, 'period': period, 'first': first, 'last': last, 'timezone': zone,
            'overview': f'人工已确认 {totals["confirmed"]} 项，进行中 {totals["in_progress"]} 项，受阻 {totals["blocked"]} 项；'
                        f'另有 {totals["unreviewed"]} 项尚未形成审阅结论；共 {totals["needs_review"]} 条记录待核对（含审阅后新增草稿）。',
            'totals': totals, 'records': records, 'display_records': compact_records(records),
            'focus_records': compact_records([record for record in records if record['focus']['issues']]),
            'timeline': timeline, 'next_steps': actions,
            'coverage_notes': coverage_notes,
            'limitations': '状态按记录所属日期和当前人工结论汇总，不代表实际完成时间。未记录不等于没有工作；AI 草稿不自动算成果，引用有效不保证结论真实。'}


def report_document(path, day, zone, period='day', detail='brief'):
    from .store import report_data
    if detail not in ('brief', 'full'):
        raise ValueError('Invalid report detail')
    first, last = period_days(day, period)
    days = []
    cursor = first
    while cursor <= last:
        days.append(report_data(path, cursor.isoformat(), zone))
        cursor += timedelta(days=1)
    document = build_document(days, first.isoformat(), last.isoformat(), zone, period)
    document['detail'] = detail
    document['markdown'] = document_markdown(document)
    return document


def document_markdown(document):
    first, last = document['first'], document['last']
    period = first if first == last else f'{first} 至 {last}'
    lines = [f'# {document["title"]} · {period}', '', '时区：' + document['timezone'], '',
             '## 一页概览', '', document['overview'], '', '## 已确认成果（人工审阅的任务）', '']
    records = document['records']
    for status, heading in [('confirmed', None), ('in_progress', '进行中的工作'), ('blocked', '阻塞与风险')]:
        if heading:
            lines += ['## ' + heading, '']
        selected = [record for record in document['display_records'] if record['status'] == status]
        if not selected:
            lines += ['- 暂无' + LABELS[status] + '记录。', '']
        for record in selected:
            review = record['review']
            lines += [f'### {date_label(record)} · {safe_text(record["title"])}', '',
                      *([f'- 同文记录：{len(record["members"])} 条内容相同，合并展示；不是自动合并业务任务。'] if len(record['members']) > 1 else []),
                      '- 工作结果：' + safe_text(review['outcome'] or '待补充'),
                      '- 验证：' + safe_text(review.get('verification') or '未填写验证说明，不推断验收通过。')]
            if review.get('learnings'):
                lines.append('- 经验：' + safe_text(review['learnings']))
            for commit in review.get('commits', []):
                lines.append('- 关联提交：' + safe_text(commit['id'][:12]) + ' · ' + safe_text(commit['subject']))
            if record['focus']['updated_since_review']:
                lines.append('- 注意：人工审阅后有新草稿，原结论不代表已确认新增内容。')
            lines.append('')
    lines += ['## 需关注的审阅重点', '']
    focused = document['focus_records']
    if not focused:
        lines.append('- 暂无待关注项；这不表示系统已独立验证工作成果。')
    for record in focused:
        for issue in record['focus']['issues']:
            lines.append(f'- {date_label(record)} · {safe_text(record["title"])}：{safe_text(issue["text"])}'
                         + ('；' + safe_text(issue['detail']) if issue['detail'] else ''))
    lines += ['', '## 下一步', '']
    for action in document['next_steps']:
        lines.append(f'- {"、".join(action["dates"])} · {safe_text(action["title"])}：{safe_text(action["text"])}（{action["source"]}'
                     + (f'；{action["record_count"]} 条同文事项合并展示' if action['record_count'] > 1 else '') + '）')
    if not document['next_steps']:
        lines.append('- 尚未记录下一步，不自动补造计划。')
    lines += ['', '## LLM任务总结（待人工审核）', '']
    candidates = [record for record in records if not record['review'] or record['focus']['updated_since_review']]
    for record in candidates:
        lines += [f'### {record["date"]} · {safe_text(record["title"])}', '']
        if record['draft']:
            if document.get('detail') == 'full':
                lines += business_lines(record['draft'], '####')
            else:
                for group in record['draft']['work_items']:
                    lines += [f'#### {safe_text(group["title"])}', '', '- 状态：' + STATUS_LABELS[group['status']],
                              '- 结果：' + safe_text(group['outcome']['text'] if group['outcome'] else '尚无可确认结果。'),
                              '- 验证：' + safe_text(group['verification']['text'] if group['verification'] else '缺少验证证据，不推断通过。'), '']
        elif record['draft_quality']:
            lines += ['- 需要重新生成：' + safe_text(record['draft_quality'].get('message') or '草稿结构或证据无效。'), '']
        else:
            lines += ['- 待人工补充；尚无有效 AI 草稿，不以使用统计代替工作内容。', '']
    if not candidates:
        lines += ['- 无新增待审草稿，已审阅内容不重复展开。', '']
    totals = document['totals']
    lines += ['## 活动事实（附录统计，不是工作成果）', '',
              f'- 记录范围：{totals["recorded_days"]}/{len(document["timeline"])} 天有导入或人工记录。',
              f'- {totals["sessions"]} 个会话，{totals["requests"]} 条模型回复，{totals["tokens"]:,} Token。']
    if not totals['events']:
        lines.append('- 本日期没有已导入的活动，不能推断没有工作。' if document['period'] == 'day'
                     else '- 本周期没有已导入的活动，不能推断没有工作。')
    if not records:
        lines.append('- 工作内容待人工补充。')
    if document['period'] != 'day':
        lines += ['', '| 日期 | 工作记录 | 已确认 | 受阻 | AI 回复 | Token |', '|---|---:|---:|---:|---:|---:|']
        for row in document['timeline']:
            lines.append(f'| {row["date"]} | {row["tasks"]} | {row["states"]["confirmed"]} | {row["states"]["blocked"]} | {row["requests"]} | {row["tokens"]:,} |'
                         if row['recorded'] else f'| {row["date"]} | 未记录 | — | — | — | — |')
    lines += ['', '## 数据覆盖与口径', '', *['- ' + note for note in document['coverage_notes']],
              '- ' + document['limitations'], '- Token 不是工时或工作成果，源日志用量不是实际账单；关联提交不等于测试或验收通过。']
    return '\n'.join(lines) + '\n'
