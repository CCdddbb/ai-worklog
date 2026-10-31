"""Normalization and aggregation shared by read-only session adapters."""
import hashlib
import hmac
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
TIMEOUT = 300


def count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(0, min(int(value), 10**12))


def mapping(value):
    return value if isinstance(value, dict) else {}


def timestamp(value):
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            d = datetime.fromtimestamp(value / 1000, UTC)
        elif isinstance(value, str):
            d = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if d.tzinfo is None:
                return None
        else:
            return None
        return d.astimezone(UTC).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def bounds(day, zone):
    parsed = date.fromisoformat(day)
    tz = ZoneInfo(zone)
    start = datetime.combine(parsed, time.min, tz)
    end = datetime.combine(parsed + timedelta(days=1), time.min, tz)
    return start.timestamp(), end.timestamp()


def identity(salt, *values):
    raw = '\0'.join(str(v) for v in values).encode('utf-8')
    return hmac.new(salt, raw, hashlib.sha256).hexdigest()


def label(value):
    if not isinstance(value, str) or len(value) > 100:
        return 'unknown'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@+-]*', value):
        return 'unknown'
    if re.search(r'(?i)^(sk-|waka_|bearer|eyJ|AIza)', value) or len(value) > 70:
        return 'unknown'
    return value


def redact_title(value):
    """Best-effort short-title redaction, not a general DLP guarantee."""
    if not isinstance(value, str):
        return ''
    value = value[:4000].splitlines()[0] if value else ''
    value = re.sub(r'https?://\S+', '[URL]', value)
    value = re.sub(r'(?i)(?:bearer\s+\S+|(?:api[_-]?key|token|password|secret|cookie|authorization)\s*[:=]\s*\S+)', '[REDACTED]', value)
    value = re.sub(r'[A-Za-z0-9_+/=.-]{32,}', '[REDACTED]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', value)
    value = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[PHONE]', value)
    value = re.sub(r'/(?:Users|home|private|tmp|var)/\S+', '[PATH]', value)
    return ''.join(c for c in value if c.isprintable())[:100]


def user_title(content):
    if isinstance(content, str):
        return redact_title(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') in ['text', 'input_text']:
                return redact_title(block.get('text'))
    return ''


def usage(value, inclusive=False):
    u = mapping(value)
    if inclusive:
        read = count(u.get('cached_input_tokens'))
        write = count(u.get('cache_write_input_tokens'))
        raw = count(u.get('input_tokens'))
        # Cap invalid cache metadata instead of inventing more input than recorded.
        read = min(read, raw)
        write = min(write, raw - read)
        result = {'input': raw - read - write, 'cache_read': read, 'cache_write': write,
                  'output': count(u.get('output_tokens'))}
    else:
        result = {'input': count(u.get('input')), 'cache_read': count(u.get('cacheRead')),
                  'cache_write': count(u.get('cacheWrite')), 'output': count(u.get('output'))}
    result['tokens'] = sum(result.values())
    cost = mapping(u.get('cost')).get('total')
    result['cost'] = cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
    return result


def union_seconds(intervals):
    end = None
    total = 0
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return round(total, 2)


def summarize(events, day, zone, coverage=None):
    start, end = bounds(day, zone)
    selected = [e for e in events if start <= e['ts'] < end]
    groups = {}
    models = {}
    intervals = []
    last = {}
    for e in sorted(events, key=lambda e: e['ts']):
        key = (e['source'], e['session'])
        # Auxiliary usage isn't an activity marker (e.g. cache warming).
        if e['kind'] != 'auxiliary':
            previous = last.get(key)
            if previous is not None and 0 <= e['ts'] - previous <= TIMEOUT:
                intervals.append((max(previous, start), min(e['ts'], end)))
            last[key] = e['ts']
    for e in selected:
        group = groups.setdefault(e['session'], {'session': e['session'], 'source': e['source'],
            'project': e['project'], 'first': e['ts'], 'last': e['ts'], 'events': 0,
            'requests': 0, 'errors': 0, 'tokens': 0, 'titles': [], 'models': set()})
        group['events'] += 1
        group['first'] = min(group['first'], e['ts'])
        group['last'] = max(group['last'], e['ts'])
        group['requests'] += e['kind'] == 'assistant'
        group['errors'] += e['status'] in ['error', 'aborted']
        group['tokens'] += e['tokens']
        if e['title'] and e['title'] not in group['titles']:
            group['titles'].append(e['title'])
        if e['kind'] in ['assistant', 'auxiliary', 'usage']:
            m = models.setdefault((e['provider'], e['model']), {'provider': e['provider'],
                'model': e['model'], 'requests': 0, 'tokens': 0, 'input': 0, 'output': 0,
                'cache_read': 0, 'cache_write': 0, 'recorded_cost': 0, 'cost_records': 0})
            m['requests'] += e['kind'] == 'assistant'
            for field in ['tokens', 'input', 'output', 'cache_read', 'cache_write']:
                m[field] += e[field]
            if e['cost'] is not None:
                m['recorded_cost'] += e['cost']
                m['cost_records'] += 1
            group['models'].add(e['provider'] + '/' + e['model'])
    hourly = [{'hour': hour, 'events': 0, 'requests': 0, 'tokens': 0} for hour in range(24)]
    for e in selected:
        bucket = hourly[datetime.fromtimestamp(e['ts'], ZoneInfo(zone)).hour]
        bucket['events'] += 1
        bucket['requests'] += e['kind'] == 'assistant'
        bucket['tokens'] += e['tokens']
    for group in groups.values():
        group['models'] = sorted(group['models'])
        group['titles'] = group['titles'][:10]
    return {'date': day, 'timezone': zone, 'sessions': sorted(groups.values(), key=lambda x: x['first']),
        'models': sorted(models.values(), key=lambda x: -x['tokens']), 'event_count': len(selected),
        'request_count': sum(e['kind'] == 'assistant' for e in selected),
        'error_count': sum(e['status'] in ['error', 'aborted'] for e in selected),
        'tokens': sum(e['tokens'] for e in selected), 'estimated_active_seconds': union_seconds(intervals),
        'hourly': hourly, 'coverage': coverage or {}, 'limitations': [
            '活跃时间为5分钟阈值下的活动标记区间并集，不是实际编码工时。',
            '正常结束和工具成功不等于任务完成；成果需要人工确认。',
            '成本仅为源日志记录值，不是实际账单；零值不代表免费。',
            '不含独立CodeBuddy桥接副本；跨工具同一请求未必能自动识别。',
            '默认没有对话正文，日报是事实摘要，不是大模型生成的业务结论。']}
