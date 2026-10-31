"""Structured fake results for plumbing tests, not summary quality acceptance."""
def business_result(data, text='整理了配置调整线索，成果仍需核验。'):
    evidence = next(entry for entry in data['evidence'] if entry.get('text'))
    item = {'text': text, 'evidence_ids': [evidence['id']]}
    return {'schema_version': 2, 'overview': item, 'work_items': [{
        'title': '配置问题排查与调整', 'problem': item, 'actions': [item],
        'outcome': None, 'verification': None, 'remaining': [], 'status': 'in_progress'}]}
