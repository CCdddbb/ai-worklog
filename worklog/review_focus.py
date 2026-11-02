"""Deterministic review prompts, not a truth score or automated acceptance."""


def review_focus(review=None, result=None, quality=None, draft_at=None, coverage=None):
    issues = []
    stale = bool(review and draft_at and draft_at > review.get('updated_at', 0))
    needs_review = not review or stale

    def add(code, text, detail='', evidence_ids=None):
        issues.append({'code': code, 'text': text, 'detail': detail,
                       'evidence_ids': list(dict.fromkeys(evidence_ids or []))})

    if stale:
        add('new_draft', '人工审阅后有新草稿', '原人工结论仍保留；新草稿尚未经过本次确认。')
    if review and review.get('status') == 'confirmed' and not review.get('verification', '').strip():
        add('manual_verification_missing', '已确认成果缺少验证说明', '请补充验收方式，或明确记录尚未验证；不据此否定原人工确认。')
    if review and review.get('status') == 'blocked':
        add('manual_blocked', '存在人工记录的阻塞', review.get('outcome', ''))
    # Don't make the operator re-review the same AI warnings after saving a human decision.
    if needs_review:
        if quality and not quality.get('ready'):
            add('invalid_draft', '草稿需要重新生成', quality.get('message', '旧版结构或材料不满足要求。'))
        elif result:
            for item in result.get('work_items', []):
                cites = list(dict.fromkeys(cite for entry in [item['problem'], *item['actions'], *item['remaining'],
                    *[entry for entry in (item['outcome'], item['verification']) if entry]]
                    for cite in entry['evidence_ids']))
                if item['status'] == 'blocked':
                    add('blocked', item['title'] + '：有阻塞',
                        item['verification']['text'] if item['verification'] else '请核对阻塞原因和下一步。', cites)
                elif item['status'] == 'discussed':
                    add('discussion_only', item['title'] + '：仅讨论，不能算实施成果', '', cites)
                if item['status'] == 'reported_resolved' and not item['verification']:
                    add('verification_missing', item['title'] + '：报告已解决，但缺少验证', '先核对结果依据，不将助手报告当作独立验收。', cites)
            if coverage and coverage.get('sampled'):
                add('sampled', '工作材料经过抽样', '重点核对遗漏的中间反馈、失败和最终修正。')
        if not issues:
            add('unreviewed', '待核对工作结论', '检查结果、验证与下一步即可；无需重新阅读全部工具记录。')
    return {'needs_review': needs_review, 'updated_since_review': stale,
            'issues': issues, 'issue_count': len(issues)}
