"""Bounded, opt-in excerpts. Never include tool output, arguments or thinking."""
import hashlib
import json
import re
from pathlib import Path

from .adapters import MAX_FILE, records
from .core import identity, mapping

MAX_ITEMS = 24


def redact(text, limit=900):
    if not isinstance(text, str):
        return ''
    if len(text) > 4000:
        return '[长消息已省略]'
    text = re.sub(r'```[\s\S]*?(?:```|$)', '[代码块已省略]', text)
    text = re.sub(r'<summary>[\s\S]*?(?:</summary>|$)', '[历史摘要已省略]', text, flags=re.IGNORECASE)
    text = re.sub(r'-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?(?:-----END[^\n]*PRIVATE KEY-----|$)', '[REDACTED]', text)
    text = re.sub(r'https?://\S+', '[URL]', text)
    text = re.sub(r'(?i)bearer\s+\S+', '[REDACTED]', text)
    text = re.sub(r'''(?ix)["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|authorization|密码|密钥|令牌)["']?\s*(?:[:=：]|是|为)\s*["']?[^\s,"'，}\n]+["']?''', '[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9_+/=.-]{32,}', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[PHONE]', text)
    text = re.sub(r'/(?:Users|home|private|tmp|var)/[^\s，。]+', '[PATH]', text)
    return ''.join(c for c in text if c.isprintable() or c in '\n\t')[:limit]


def text_blocks(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(str(b.get('text', '')) for b in content if isinstance(b, dict)
                     and b.get('type') in ['text', 'input_text', 'output_text'])


def activity_digest(rows):
    selected = [(r['id'], r['ts'], r['status']) for r in rows
                if r['kind'] not in ['usage', 'auxiliary']]
    return hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest()


def excerpts(rows, roots, key):
    """Resolve only file hashes present in a user-selected, already imported scope."""
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
            if identity(key, str(path))[:16] not in file_ids:
                continue
            if path.stat().st_size > MAX_FILE:
                continue
            session = str(path)
            stats = dict.fromkeys(['truncated_files', 'oversized_lines', 'partial_lines', 'malformed_lines'], 0)
            for number, entry in records(path, stats):
                typ = entry.get('type')
                payload = mapping(entry.get('payload'))
                if source == 'pi' and typ == 'session':
                    session = str(entry.get('id') or path)
                    continue
                if source == 'codex' and typ == 'session_meta':
                    session = str(payload.get('session_id') or payload.get('id') or path)
                    continue
                msg = mapping(entry.get('message')) if source == 'pi' else payload
                rid = session + ':' + str(entry.get('id') or 'line:' + str(number))
                if source == 'pi' and msg.get('role') == 'assistant' and msg.get('responseId'):
                    rid = 'response:' + str(msg.get('provider', 'unknown')) + ':' + str(msg['responseId'])
                if source == 'codex':
                    rid = session + ':entry:' + str(entry.get('ordinal', number))
                eid = identity(key, source, rid)
                if eid not in wanted:
                    continue
                if msg.get('role') in ['user', 'assistant']:
                    found[eid] = redact(text_blocks(msg.get('content')), 500)
                # Tool arguments/output, reasoning, system prompts and error payloads are ignored.
    return found


def source_scope(roots, key):
    selected = sorted((name, str(Path(root).expanduser().resolve())) for name, root in roots.items())
    return identity(key, 'summary-sources', json.dumps(selected))


def snapshot(task, rows, roots, key, allow_excerpts):
    active = [r for r in rows if r['kind'] not in ['usage', 'auxiliary']]
    if not active:
        raise ValueError('No activity in selected task scope')
    ordered = sorted(active, key=lambda r: (r['ts'], r['id']))
    # Reserve initial intent and a recent conclusion for every linked session.
    # At most 12 sessions are accepted, so the 24-item cap cannot erase a middle session.
    chosen = {}
    session_ids = list(dict.fromkeys(r['session'] for r in ordered))
    for session_id in session_ids:
        group = [r for r in ordered if r['session'] == session_id]
        first = next((r for r in group if r['kind'] == 'user'), group[0])
        last = group[-1]
        if not (last['kind'] in ['tool', 'error'] and last['status'] == 'error'):
            last = next((r for r in reversed(group) if r['kind'] == 'assistant'), last)
        for row in [first, last]:
            chosen[row['id']] = row
    for row in reversed(ordered):
        if len(chosen) >= MAX_ITEMS:
            break
        chosen[row['id']] = row
    selected = sorted(chosen.values(), key=lambda r: (r['ts'], r['id']))
    bodies = excerpts(selected, roots, key) if allow_excerpts else {}
    items = []
    for row in selected:
        item = {k: row[k] for k in ['source', 'session', 'kind', 'status', 'ts', 'error_code']}
        item['id'] = 'e_' + row['id'][:16]
        item['text'] = bodies.get(row['id'], '')
        items.append(item)
    result = {'task': redact(task['title'], 100), 'evidence': items,
              'coverage': {'total_activity_events': len(ordered), 'selected_events': len(items),
                           'excerpts_available': sum(bool(i['text']) for i in items),
                           'sampled': len(ordered) > len(items), 'allow_excerpts': allow_excerpts,
                           'total_sessions': len(session_ids), 'selected_sessions': len({r['session'] for r in selected})},
              'tokens': sum(r['tokens'] for r in rows), 'activity_digest': activity_digest(rows),
              'source_scope': source_scope(roots, key)}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 48000:
        raise ValueError('Evidence snapshot exceeds byte limit')
    return result
