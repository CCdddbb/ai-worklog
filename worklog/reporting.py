"""Business-first rendering shared by daily reports and incremental reviews."""

STATUS_LABELS = {'reported_resolved': '助手报告已解决，待人工核验', 'in_progress': '进行中',
                 'blocked': '存在阻塞', 'discussed': '仅讨论/方案，未证明实施'}


def safe_text(value):
    return str(value).replace('<', '＜').replace('>', '＞').replace('[', '［').replace(']', '］').replace('`', '｀').replace('\n', ' ')


def business_lines(result, heading='###'):
    lines = []
    for group in result.get('work_items', []):
        lines.extend([f'{heading} {safe_text(group["title"])}', '',
                      '- 状态：' + STATUS_LABELS[group['status']],
                      '- 问题/目标：' + safe_text(group['problem']['text']), '- 处理措施：'])
        for action in group['actions']:
            lines.append('  - ' + safe_text(action['text']))
        if not group['actions']:
            lines.append('  - 未提供具体实施证据，不能视为已完成。')
        outcome = group['outcome']
        lines.append('- 结果/解决的问题：' + (safe_text(outcome['text']) if outcome else '暂无可确认的结果证据。'))
        verification = group['verification']
        lines.append('- 验证情况：' + (safe_text(verification['text']) if verification else '未提供验证证据，不能推断测试通过。'))
        if group['remaining']:
            lines.append('- 未完成/下一步：')
            lines.extend('  - ' + safe_text(item['text']) for item in group['remaining'])
        entries = [group['problem'], *group['actions'], *group['remaining']]
        entries += [item for item in (outcome, verification) if item]
        ids = list(dict.fromkeys(cite for entry in entries for cite in entry['evidence_ids']))
        lines += ['- 证据索引：' + ', '.join(ids), '']
    return lines


def draft_lines(draft):
    lines = ['### ' + safe_text(draft['title']), '']
    quality = draft.get('quality', {})
    if quality and not quality.get('ready'):
        return lines + ['- 需要重新生成：' + safe_text(quality['message']),
                        '- 旧记录保留用于追溯，不作为有效工作总结或成果。', '']
    result = draft['result']
    lines += [safe_text(result['overview']['text']), '',
              '- 模型：' + safe_text(draft['provider'] + '/' + draft['model']),
              '- 结论性质：基于会话正文的草稿；助手报告不等于独立验证或人工验收。', '']
    if result.get('work_items'):
        lines += business_lines(result, '####')
    else:
        lines.append('- 旧版草稿，请重新生成以获得问题、措施、结果和验证说明。')
    return lines
