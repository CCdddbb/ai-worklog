"""Read selected JSONL files; never execute hooks or retain message bodies."""
import codecs
import json
import re
from pathlib import Path

from .core import identity, label, mapping, timestamp, usage, user_title

MAX_FILE = 64 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024


def prefix_timestamp(raw):
    """Read only top-level fields before a truncated value, never nested text matches."""
    try:
        text = codecs.getincrementaldecoder('utf-8')().decode(raw, final=False)
        decoder = json.JSONDecoder()
        index = 0
        def whitespace(pos):
            while pos < len(text) and text[pos] in ' \r\n\t':
                pos += 1
            return pos
        index = whitespace(index)
        if text[index] != '{':
            return None
        index += 1
        while True:
            name, index = decoder.raw_decode(text, whitespace(index))
            index = whitespace(index)
            if not isinstance(name, str) or text[index] != ':':
                return None
            value, index = decoder.raw_decode(text, whitespace(index + 1))
            if name == 'timestamp':
                return timestamp(value)
            index = whitespace(index)
            if text[index] != ',':
                return None
            index += 1
    except (ValueError, UnicodeDecodeError, IndexError, RecursionError):
        return None


def records(path, stats, window=None):
    with path.open('rb') as stream:
        consumed = 0
        number = 0
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            consumed += len(line)
            number += 1
            if consumed > MAX_FILE:
                stats['truncated_files'] += 1
                break
            if len(line) > MAX_LINE:
                ts = prefix_timestamp(line) if window else None
                if window and ts is not None and not window[0] <= ts <= window[1]:
                    name = 'out_of_window_oversized_lines'
                    stats[name] = stats.get(name, 0) + 1
                else:
                    stats['oversized_lines'] += 1
                while line and not line.endswith(b'\n'):
                    line = stream.readline(MAX_LINE + 1)
                    consumed += len(line)
                    if consumed > MAX_FILE:
                        stats['truncated_files'] += 1
                        return
                continue
            if not line.endswith(b'\n'):
                stats['partial_lines'] += 1
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError('Record must be an object')
                yield number, value
            except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
                stats['malformed_lines'] += 1


def event(salt, source, session, project, record_id, ts, kind, provider='unknown', model='unknown',
          status='observed', values=None, title='', file_id=''):
    row = {'id': identity(salt, source, record_id), 'source': source,
        'session': identity(salt, source, session)[:16], 'project': identity(salt, project)[:12],
        'ts': ts, 'kind': kind, 'provider': label(provider), 'model': label(model),
        'status': status, 'title': title, 'file_id': file_id,
        'error_code': '', **usage({})}
    row.update(values or {})
    return row


def pi_events(path, salt, stats, titles=False, window=None):
    session = str(path)
    project = 'unknown'
    provider, model = 'unknown', 'unknown'
    file_id = identity(salt, str(path))[:16]
    result = []
    for number, entry in records(path, stats, window):
        typ = entry.get('type')
        if typ == 'session':
            session = entry.get('id') if isinstance(entry.get('id'), str) else str(path)
            project = entry.get('cwd') if isinstance(entry.get('cwd'), str) else 'unknown'
            continue
        if typ == 'model_change':
            provider, model = label(entry.get('provider')), label(entry.get('modelId'))
            continue
        ts = timestamp(entry.get('timestamp'))
        if ts is None:
            stats['invalid_timestamps'] += 1
            continue
        msg = mapping(entry.get('message'))
        entry_id = entry.get('id')
        if not isinstance(entry_id, str):
            entry_id = 'line:' + str(number)
        record_id = session + ':' + entry_id
        if typ == 'message':
            role = msg.get('role')
            if role == 'user':
                result.append(event(salt, 'pi', session, project, record_id, ts, 'user', provider, model,
                    title=user_title(msg.get('content')) if titles else '', file_id=file_id))
            elif role == 'assistant' and msg.get('stopReason') != 'pending':
                response = msg.get('responseId')
                actual_provider = label(msg.get('provider', provider))
                if isinstance(response, str) and response:
                    record_id = 'response:' + actual_provider + ':' + response
                status = msg.get('stopReason')
                status = status if status in ['error', 'aborted', 'stop', 'toolUse', 'length', 'deferred'] else 'observed'
                row = event(salt, 'pi', session, project, record_id, ts, 'assistant', actual_provider,
                    msg.get('model', model), status, usage(msg.get('usage')), file_id=file_id)
                match = re.search(r'\b(401|403|429|500|502|503)\b', str(msg.get('errorMessage', '')))
                if match:
                    row['error_code'] = match.group(1)
                result.append(row)
            elif role == 'toolResult':
                result.append(event(salt, 'pi', session, project, record_id, ts, 'tool', provider, model,
                    'error' if msg.get('isError') is True else 'observed', file_id=file_id))
                if msg.get('usage'):
                    stats['nested_usage_omitted'] += 1
        elif typ in ['usage', 'compaction', 'branch_summary'] and isinstance(entry.get('usage'), dict):
            result.append(event(salt, 'pi', session, project, record_id, ts, 'auxiliary',
                entry.get('provider', 'unknown'), entry.get('model', 'unknown'),
                values=usage(entry['usage']), file_id=file_id))
    return result


def codex_events(path, salt, stats, titles=False, window=None):
    session, project = str(path), 'unknown'
    provider, model = 'codex', 'unknown'
    file_id = identity(salt, str(path))[:16]
    result, direct, legacy = [], [], []
    previous = None
    for number, entry in records(path, stats, window):
        typ = entry.get('type')
        payload = mapping(entry.get('payload'))
        if typ == 'session_meta':
            session = str(payload.get('session_id') or payload.get('id') or path)
            project = payload.get('cwd') if isinstance(payload.get('cwd'), str) else 'unknown'
            provider = label(payload.get('model_provider', 'codex'))
            continue
        if typ == 'turn_context':
            model = label(payload.get('model'))
            continue
        ts = timestamp(entry.get('timestamp'))
        if ts is None:
            stats['invalid_timestamps'] += 1
            continue
        ordinal = entry.get('ordinal', number)
        key = session + ':entry:' + str(ordinal)
        subtype = payload.get('type')
        if typ == 'response_item' and subtype == 'message' and payload.get('role') in ['user', 'assistant']:
            role = payload['role']
            row = event(salt, 'codex', session, project, key, ts, role, provider, model,
                status='stop' if role == 'assistant' and payload.get('phase') == 'final_answer' else 'observed',
                title=user_title(payload.get('content')) if titles and role == 'user' else '', file_id=file_id)
            result.append(row)
        elif typ == 'response_item' and subtype in ['function_call', 'custom_tool_call']:
            result.append(event(salt, 'codex', session, project, key, ts, 'tool', provider, model, file_id=file_id))
        elif typ == 'event_msg' and subtype in ['task_complete', 'task_completed']:
            result.append(event(salt, 'codex', session, project, key, ts, 'boundary', provider, model, 'stop', file_id=file_id))
        elif typ == 'event_msg' and subtype in ['error', 'task_aborted', 'turn_aborted']:
            result.append(event(salt, 'codex', session, project, key, ts, 'error', provider, model, 'error', file_id=file_id))
        elif typ == 'token_usage_record':
            rid = payload.get('response_id')
            if isinstance(rid, str) and rid:
                key = 'usage-response:' + provider + ':' + rid
            direct.append(event(salt, 'codex', session, project, key, ts, 'usage', provider, model,
                values=usage(payload.get('usage'), inclusive=True), file_id=file_id))
        elif typ == 'event_msg' and subtype == 'token_count':
            total = mapping(mapping(payload.get('info')).get('total_token_usage'))
            if not total:
                continue
            current = usage(total, inclusive=True)
            values = dict(current)
            if previous is None:
                last = mapping(mapping(payload.get('info')).get('last_token_usage'))
                values = usage(last, inclusive=True) if last else current
            elif any(current[k] < previous[k] for k in ['input', 'output', 'cache_read', 'cache_write']):
                stats['counter_resets'] += 1
                values = usage({})
            else:
                for k in ['input', 'output', 'cache_read', 'cache_write']:
                    values[k] = current[k] - previous[k]
                values['tokens'] = sum(values[k] for k in ['input', 'output', 'cache_read', 'cache_write'])
            previous = current
            if values['tokens']:
                legacy.append(event(salt, 'codex', session, project, key, ts, 'usage', provider, model,
                    values=values, file_id=file_id))
    if direct:
        stats['legacy_usage_suppressed'] += len(legacy)
    return result + (direct if direct else legacy)


def collect(root, source, salt, titles=False, max_files=100, window=None):
    root = Path(root).expanduser().resolve()
    if not root.is_dir() or source not in ['pi', 'codex']:
        raise ValueError('Choose an existing Pi/Codex session directory')
    stats = {k: 0 for k in ['discovered', 'selected', 'omitted_files', 'oversized_files', 'oversized_lines',
        'truncated_files', 'partial_lines', 'malformed_lines', 'invalid_timestamps', 'unreadable_files',
        'symlinks_skipped', 'counter_resets', 'nested_usage_omitted', 'legacy_usage_suppressed']}
    paths = []
    for path in root.rglob('*.jsonl'):
        if path.is_symlink():
            stats['symlinks_skipped'] += 1
            continue
        try:
            if not path.is_file() or root not in path.resolve().parents:
                continue
            paths.append((path.stat().st_mtime, path))
        except OSError:
            stats['unreadable_files'] += 1
    paths.sort(key=lambda item: item[0], reverse=True)
    stats['discovered'] = len(paths)
    stats['omitted_files'] = max(0, len(paths) - max_files)
    events = []
    for _, path in paths[:max_files]:
        try:
            if path.stat().st_size > MAX_FILE:
                stats['oversized_files'] += 1
                continue
            stats['selected'] += 1
            parser = pi_events if source == 'pi' else codex_events
            events.extend(parser(path, salt, stats, titles, window))
        except OSError:
            stats['unreadable_files'] += 1
    stats['source'] = source
    stats['title_capture'] = titles
    return events, stats
