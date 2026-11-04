"""Bounded, opt-in business evidence. Never include tool output, arguments or thinking."""
import hashlib
import json
import re
from pathlib import Path

from .adapters import MAX_FILE, records
from .core import identity, mapping

MAX_ITEMS = 24
MAX_CANDIDATES = 512
EVIDENCE_VERSION = 2
TEXT_BUDGET = 32000


class EvidenceUnavailable(ValueError):
    def __init__(self, code, message, coverage=None):
        super().__init__(message)
        self.code = code
        self.coverage = coverage or {}


def _clean(text):
    text = re.sub(r'```[\s\S]*?(?:```|$)', '[代码块已省略]', text)
    text = re.sub(r'<summary>[\s\S]*?(?:</summary>|$)', '[历史摘要已省略]', text, flags=re.IGNORECASE)
    text = re.sub(r'<(?:system-reminder|environment_context|developer_instructions)\b[^>]*>[\s\S]*?(?:</(?:system-reminder|environment_context|developer_instructions)>|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?(?:-----END[^\n]*PRIVATE KEY-----|$)', '[REDACTED]', text)
    text = re.sub(r'https?://\S+', '[URL]', text)
    text = re.sub(r'(?i)bearer\s+\S+', '[REDACTED]', text)
    text = re.sub(r'''(?ix)["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|authorization|密码|密钥|令牌)["']?\s*(?:[:=：]|是|为)\s*["']?[^\s,"'，}\n]+["']?''', '[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9_+/=.-]{32,}', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[PHONE]', text)
    text = re.sub(r'/(?:Users|home|private|tmp|var)/[^\s，。]+', '[PATH]', text)
    return ''.join(c for c in text if c.isprintable() or c in '\n\t')


def redact(text, limit=900):
    if not isinstance(text, str):
        return ''
    if len(text) > 4000:
        return '[长消息已省略]'
    return _clean(text)[:limit]


def clip_text(text, byte_limit):
    """Keep intent at the beginning AND conclusions at the end, on UTF-8 boundaries."""
    raw = text.encode('utf-8')
    if len(raw) <= byte_limit:
        return text
    marker = '\n[中段已省略]\n'
    room = max(0, byte_limit - len(marker.encode('utf-8')))
    head = room * 2 // 5
    tail = room - head
    return raw[:head].decode('utf-8', errors='ignore') + marker + (raw[-tail:].decode('utf-8', errors='ignore') if tail else '')


def business_excerpt(text):
    # Raw JSONL lines are bounded by the adapter. Clean whole blocks BEFORE clipping,
    # so slicing cannot expose the middle of a code block/private key.
    return clip_text(_clean(text), 3600) if isinstance(text, str) else ''


def readable(text):
    if not isinstance(text, str):
        return False
    stripped = re.sub(r'\[[^\]\n]*\]', '', text).strip()
    if len(stripped) < 4 or stripped.startswith('/'):
        return False
    substance = re.sub(r'(?<![A-Za-z])(?:Read|Bash|Edit|Write|Grep|Glob|TodoWrite)(?![A-Za-z])', '', stripped, flags=re.IGNORECASE)
    substance = re.sub(r'工具调用|工具|调用|执行|返回|成功|观察|多次|若干|记录|会话|事件|进行了|使用了|已经|完成了|我会先|我先|我已|以及|和', '', substance)
    return len(re.sub(r'[^\w\u4e00-\u9fff]', '', substance)) >= 4


def text_blocks(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(str(b.get('text', '')) for b in content if isinstance(b, dict)
                     and b.get('type') in ['text', 'input_text', 'output_text'])


def activity_digest(rows):
    selected = [(r['id'], r['ts'], r['status']) for r in rows if r['kind'] not in ['usage', 'auxiliary']]
    return hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest()


def excerpts(rows, roots, key):
    wanted = {r['id'] for r in rows}
    file_ids = {r['file_id'] for r in rows}
    found = {}
    for source, directory in roots.items():
        if source not in ['pi', 'codex']:
            continue
        root = Path(directory).expanduser().resolve()
        for path in root.rglob('*.jsonl'):
            if path.is_symlink() or root not in path.resolve().parents:
                continue
            if identity(key, str(path))[:16] not in file_ids or path.stat().st_size > MAX_FILE:
                continue
            session = str(path)
            provider = 'unknown'
            stats = dict.fromkeys(['truncated_files', 'oversized_lines', 'partial_lines', 'malformed_lines'], 0)
            for number, entry in records(path, stats):
                typ = entry.get('type')
                payload = mapping(entry.get('payload'))
                if source == 'pi' and typ == 'session':
                    session = str(entry.get('id') or path)
                    continue
                if source == 'pi' and typ == 'model_change':
                    provider = str(entry.get('provider') or 'unknown')
                    continue
                if source == 'codex' and typ == 'session_meta':
                    session = str(payload.get('session_id') or payload.get('id') or path)
                    continue
                if source == 'pi' and typ != 'message':
                    continue
                if source == 'codex' and (typ != 'response_item' or payload.get('type') != 'message'):
                    continue
                msg = mapping(entry.get('message')) if source == 'pi' else payload
                if msg.get('role') not in ['user', 'assistant']:
                    continue
                rid = session + ':' + str(entry.get('id') or 'line:' + str(number))
                if source == 'pi' and msg.get('role') == 'assistant' and msg.get('responseId'):
                    rid = 'response:' + str(msg.get('provider', provider)) + ':' + str(msg['responseId'])
                if source == 'codex':
                    rid = session + ':entry:' + str(entry.get('ordinal', number))
                eid = identity(key, source, rid)
                if eid in wanted:
                    found[eid] = business_excerpt(text_blocks(msg.get('content')))
    return found


def source_scope(roots, key):
    selected = sorted((name, str(Path(root).expanduser().resolve())) for name, root in roots.items())
    return identity(key, 'summary-sources', json.dumps(selected))


def distributed(rows, limit):
    """Reserve each session's intent/conclusion, then fill evenly across its timeline."""
    groups = {}
    for row in rows:
        groups.setdefault(row['session'], []).append(row)
    chosen = {}
    for group in groups.values():
        first = next((r for r in group if r['kind'] == 'user'), group[0])
        last = next((r for r in reversed(group) if r['kind'] == 'assistant'), group[-1])
        for row in (first, last):
            if len(chosen) < limit:
                chosen[row['id']] = row
    for group in groups.values():
        if len(chosen) < limit:
            chosen[group[-1]['id']] = group[-1]  # Preserve late user rejection/corrections too.
    # Breadth-first bisection: middle work is not erased by a long final tool loop.
    queues = {key: [(0, len(group) - 1)] for key, group in groups.items()}
    while len(chosen) < min(limit, len(rows)) and any(queues.values()):
        for key, group in groups.items():
            if not queues[key] or len(chosen) >= limit:
                continue
            lo, hi = queues[key].pop(0)
            mid = (lo + hi) // 2
            chosen[group[mid]['id']] = group[mid]
            # Keep a request and its response together instead of orphaned snippets.
            mate = None
            if group[mid]['kind'] == 'assistant':
                mate = next((r for r in reversed(group[:mid]) if r['kind'] == 'user'), None)
            elif group[mid]['kind'] == 'user':
                for following in group[mid + 1:]:
                    if following['kind'] == 'user':
                        break
                    if following['kind'] == 'assistant':
                        mate = following
                        break
            if mate is not None and len(chosen) < limit:
                chosen[mate['id']] = mate
            if lo < mid:
                queues[key].append((lo, mid - 1))
            if mid < hi:
                queues[key].append((mid + 1, hi))
    return sorted(chosen.values(), key=lambda r: (r['ts'], r['id']))


def evidence_readiness(data):
    coverage = data.get('coverage', {})
    texts = [e for e in data.get('evidence', []) if e.get('kind') in ('user', 'assistant') and readable(e.get('text'))]
    if not coverage.get('allow_excerpts'):
        return {'ready': False, 'code': 'excerpt_consent_required',
                'message': '尚未授权正文摘录，无法理解具体工作。请在总结设置授权并预览材料；不会用元数据生成业务总结。'}
    if not texts:
        return {'ready': False, 'code': 'no_readable_evidence',
                'message': '未找到可读的用户需求或助手结论。请检查来源、日期、任务范围及材料预览；不会生成工具流水账。'}
    if coverage.get('selected_sessions', 0) < coverage.get('total_sessions', 0):
        return {'ready': False, 'code': 'incomplete_session_evidence',
                'message': '部分关联会话没有可读正文。请检查材料预览、来源与任务范围，或移除缺失会话后再生成，避免遗漏工作。'}
    return {'ready': True, 'code': 'ready', 'message': '',
            'readable_messages': len(texts), 'conclusions': sum(e['kind'] == 'assistant' for e in texts)}


def require_evidence(data):
    state = evidence_readiness(data)
    if not state['ready']:
        raise EvidenceUnavailable(state['code'], state['message'], data.get('coverage'))


def summary_quality(data, result):
    state = evidence_readiness(data)
    if not state['ready']:
        return dict(state, needs_regeneration=True)
    if (result or {}).get('schema_version') != 2:
        return {'ready': False, 'code': 'legacy_summary', 'needs_regeneration': True,
                'message': '旧版草稿未按问题、措施和结果组织，请使用新版重新生成。'}
    return {'ready': True, 'code': 'ready', 'needs_regeneration': False, 'message': ''}


def snapshot(task, rows, roots, key, allow_excerpts):
    ordered = sorted((r for r in rows if r['kind'] not in ['usage', 'auxiliary']), key=lambda r: (r['ts'], r['id']))
    if not ordered:
        raise ValueError('No activity in selected task scope')
    dialogue = [r for r in ordered if r['kind'] in ['user', 'assistant'] and r['status'] != 'toolUse']
    candidates = distributed(dialogue or ordered, MAX_CANDIDATES)
    bodies = excerpts(candidates, roots, key) if allow_excerpts else {}
    meaningful = [r for r in candidates if readable(bodies.get(r['id']))]
    selected = distributed(meaningful or candidates, MAX_ITEMS)
    # Fair UTF-8 allocation, with spare capacity redistributed to longer messages.
    lengths = {r['id']: len(bodies.get(r['id'], '').encode('utf-8')) for r in selected}
    caps = dict.fromkeys(lengths, 0)
    remaining = TEXT_BUDGET
    while remaining and (waiting := [k for k in caps if caps[k] < lengths[k]]):
        share = max(1, remaining // len(waiting))
        for item_id in waiting:
            added = min(share, lengths[item_id] - caps[item_id], remaining)
            caps[item_id] += added
            remaining -= added
    items = []
    for row in selected:
        item = {k: row[k] for k in ['source', 'session', 'kind', 'status', 'ts', 'error_code']}
        item['id'] = 'e_' + row['id'][:16]
        item['text'] = clip_text(bodies.get(row['id'], ''), caps[row['id']])
        items.append(item)
    result = {'evidence_version': EVIDENCE_VERSION, 'task': redact(task['title'], 100), 'evidence': items,
              'scope': {'first_ts': ordered[0]['ts'], 'last_ts': ordered[-1]['ts'],
                        'sessions': {session: {'first_ts': min(r['ts'] for r in ordered if r['session'] == session),
                                               'last_ts': max(r['ts'] for r in ordered if r['session'] == session)}
                                     for session in {r['session'] for r in ordered}}},
              'coverage': {'total_activity_events': len(ordered), 'selected_events': len(items),
                           'dialogue_events': len(dialogue), 'candidate_events': len(candidates),
                           'readable_candidates': len(meaningful), 'tool_events_excluded': sum(r['kind'] == 'tool' for r in ordered),
                           'excerpts_available': sum(readable(i['text']) for i in items),
                           'sampled': len(dialogue or ordered) > len(items), 'allow_excerpts': allow_excerpts,
                           'total_sessions': len({r['session'] for r in ordered}),
                           'selected_sessions': len({r['session'] for r in selected})},
              'tokens': sum(r['tokens'] for r in rows), 'activity_digest': activity_digest(rows),
              'source_scope': source_scope(roots, key)}
    result['readiness'] = evidence_readiness(result)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 48000:
        raise ValueError('Evidence snapshot exceeds byte limit')
    return result
