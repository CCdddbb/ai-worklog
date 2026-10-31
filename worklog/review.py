"""Incremental terminal reviews: verified batches, private cache, atomic checkpoints."""
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import collect
from .evidence import (
    EvidenceUnavailable,
    activity_digest,
    redact,
    require_evidence,
    snapshot,
    source_scope,
)
from .llm import (
    SYSTEM,
    PiRunner,
    SummaryError,
    validate_business_result,
    validate_result,
)
from .reporting import business_lines
from .store import FIELDS, connect, salt

GAPS = ('omitted_files', 'oversized_files', 'oversized_lines', 'truncated_files',
        'partial_lines', 'malformed_lines', 'unreadable_files', 'invalid_timestamps')


REVIEW_INSTRUCTIONS = ('\n这是跨会话增量工作回顾。work_items应按首次出现的时间列出不同任务，'
                       '同一任务的多轮尝试合并，不把重复的retry当作新成果。'
                       '将用户要求、助手报告、未验证结果区分清楚；不要把全部任务挤成一句笼统总结。')


class ReviewRunner(PiRunner):
    def command(self):
        command = super().command()
        command[-1] += REVIEW_INSTRUCTIONS
        return command


def default_review_runner(provider, model):
    if provider == 'codebuddy':
        from .codebuddy import CodeBuddyRunner
        return CodeBuddyRunner(model=model, system_prompt=SYSTEM + REVIEW_INSTRUCTIONS)
    return ReviewRunner(provider, model)


class ReviewError(ValueError):
    pass


@contextmanager
def review_write_lock(path):
    import fcntl
    fd = os.open(str(Path(path).expanduser()) + '.review.lock',
                 os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReviewError('另一个回顾正在运行；请等待它结束后重试。') from None
        yield
    finally:
        os.close(fd)


def since_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('offset required')
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise ReviewError('--since 需要带时区的时间，例如 2026-10-29T09:00:00+08:00。') from None


def batches(rows):
    """Do not sample away middle tasks; every selected event belongs to one batch."""
    result, current, sessions = [], [], set()
    for row in sorted(rows, key=lambda r: (r['ts'], r['id'])):
        if len(current) == 24 or (len(sessions) == 12 and row['session'] not in sessions):
            result.append(current)
            current, sessions = [], set()
        current.append(row)
        sessions.add(row['session'])
    if current:
        result.append(current)
    return result


def safe(value):
    return redact(value, 600).replace('\n', ' ').replace('\t', ' ').replace('<', '＜').replace(
        '>', '＞').replace('[', '［').replace(']', '］').replace('`', '｀')


def stamp(ts, zone):
    return datetime.fromtimestamp(ts, ZoneInfo(zone)).isoformat(timespec='seconds')


def write_new(path, text):
    # Never replace an existing report, database, session or symlink target.
    with Path(path).expanduser().open('x', encoding='utf-8') as stream:
        os.chmod(stream.name, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def review(args, emit, progress, runner_factory=default_review_runner, clock=time.time):
    roots = {name: path for name, path in [('pi', args.pi_dir), ('codex', args.codex_dir)] if path}
    if not 1 <= args.batch_limit <= 12:
        raise ReviewError('--batch-limit 需要在 1 到 12 之间。')
    cutoff = clock()  # New activity appearing during model calls waits until the next invocation.
    db = connect(args.db)
    try:
        with review_write_lock(args.db):
            if args.last:
                row = db.execute('SELECT output FROM reviews ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
                if not row:
                    raise ReviewError('还没有成功保存的回顾。')
                if args.out:
                    write_new(args.out, row['output'])
                emit(row['output'])
                return {'status': 'last'}
            if not roots:
                raise ReviewError('请显式指定 --pi-dir 和/或 --codex-dir。')
            for root in roots.values():
                if not Path(root).expanduser().is_dir():
                    raise ReviewError('日志目录不存在；请检查来源参数。')
            key = salt(args.db)
            scope = source_scope(roots, key)
            state = db.execute('SELECT * FROM review_state WHERE scope=?', (scope,)).fetchone()
            if args.start_now and (args.preview or args.out):
                raise ReviewError('--start-now 不能与 --preview 或 --out 一起使用。')
            if state and (args.start_now or args.since):
                raise ReviewError('此来源已有记录点，不能重置跳过未回顾活动；请直接 review，或另选新数据库。')
            if not state:
                if not args.start_now and not args.since:
                    raise ReviewError('首次请用 --start-now 建立起点，或用 --since 明确指定历史起点。')
                start = cutoff if args.start_now else since_timestamp(args.since)
                if start > cutoff:
                    raise ReviewError('起点不能在未来。')
                with db:
                    db.execute('INSERT INTO review_state VALUES (?,?,?,?)',
                               (scope, start, start, int(bool(args.allow_excerpts))))
            elif args.allow_excerpts is not None:
                with db:
                    db.execute('UPDATE review_state SET excerpts=? WHERE scope=?',
                               (int(args.allow_excerpts), scope))
            state = dict(db.execute('SELECT * FROM review_state WHERE scope=?', (scope,)).fetchone())
            if args.start_now:
                emit('已建立回顾起点：' + stamp(state['start_ts'], args.timezone) + '\n'
                     + '未调用模型；摘录授权：' + ('开启' if state['excerpts'] else '关闭') + '。\n')
                return {'status': 'initialized'}

            all_rows, coverage = [], []
            for source, root in roots.items():
                rows, stats = collect(root, source, key, False, args.max_files,
                                      window=(state['start_ts'], cutoff))
                all_rows.extend(r for r in rows if state['start_ts'] <= r['ts'] <= cutoff)
                coverage.append(stats)
            # Only this exact import scope is considered, never unrelated database events.
            by_id = {r['id']: r for r in all_rows}
            rows = list(by_id.values())
            fields = ','.join(FIELDS)
            updates = ','.join(f'{k}=excluded.{k}' for k in FIELDS if k != 'id')
            with db:
                db.executemany(f'INSERT INTO events ({fields}) VALUES ({",".join("?" for _ in FIELDS)}) '
                               f'ON CONFLICT(id) DO UPDATE SET {updates}', [[r[k] for k in FIELDS] for r in rows])
            seen = {r['event_id']: r['digest'] for r in db.execute('SELECT * FROM review_seen WHERE scope=?', (scope,))}
            meaningful = [r for r in rows if (r['kind'] in ['user', 'error', 'boundary']
                          or r['kind'] == 'assistant' and r['status'] != 'toolUse'
                          or r['kind'] == 'tool' and r['status'] == 'error')]
            unseen = [r for r in meaningful if seen.get(r['id']) != activity_digest([r])]
            groups = batches(unseen)
            selected = groups[:args.batch_limit]
            gaps = sum(stats.get(name, 0) for stats in coverage for name in GAPS)
            header = ['# AI 增量回顾（待人工审核）', '',
                      '记录起点：' + stamp(state['start_ts'], args.timezone),
                      '本次读取截止：' + stamp(cutoff, args.timezone),
                      f'新活动 {len(unseen)} 条；本次最多处理 {len(selected)}/{len(groups)} 批。',
                      '按事件去重，包含起点之后的迟到日志；不是整机工作记录。',
                      '仅归纳用户请求和助手报告，不表示业务已验收。', '']
            for stats in coverage:
                detail = ', '.join(f'{name}={stats[name]}' for name in GAPS if stats.get(name)) or '未检测到读取缺口'
                if stats.get('out_of_window_oversized_lines'):
                    detail += f'；窗口外过长行={stats["out_of_window_oversized_lines"]}（按顶层头部时间排除）'
                header.append(f'- 来源 {stats["source"]}：读取 {stats["selected"]} 个文件；{detail}。')
            if gaps and not args.preview and not args.allow_partial:
                progress('\n'.join(header) + '\n')
                raise ReviewError('日志读取存在缺口，未调用模型或推进记录点。可 --preview 检查，调大 --max-files；'
                                  '只有明确接受不完整回顾时才用 --allow-partial。')
            if gaps:
                header.append('警告：来源不完整，不能把本次结果视为全部工作。')
            if not unseen:
                emit('\n'.join(header) + '\n\n没有观察到新的可回顾活动；记录点未推进。\n')
                return {'status': 'empty'}
            if args.preview:
                emit('\n'.join(header) + '\n\n## 将发送的材料预览（不调用模型、不标记已回顾）\n')
                for index, group in enumerate(selected, 1):
                    data = snapshot({'title': f'增量工作批次 {index}'}, group, roots, key, bool(state['excerpts']))
                    emit(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
                return {'status': 'preview', 'events': len(unseen)}

            materials = [snapshot({'title': f'增量工作批次 {index}'}, group, roots, key, bool(state['excerpts']))
                         for index, group in enumerate(selected, 1)]
            for material in materials:
                require_evidence(material)
            runner = runner_factory(args.summary_provider, args.summary_model)
            header += [f'模型：{safe(runner.provider)}/{safe(runner.model)}；摘录授权：'
                       + ('开启' if state['excerpts'] else '关闭（只能生成有限的活动概括）') + '。', '']
            output = '\n'.join(header) + '\n'
            emit(output)
            consumed = []
            for index, group in enumerate(selected, 1):
                data = materials[index - 1]
                fingerprint = hashlib.sha256(json.dumps([2, data, runner.provider, runner.model],
                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                cached = db.execute('SELECT * FROM review_cache WHERE fingerprint=?', (fingerprint,)).fetchone()
                progress(f'[{index}/{len(selected)}] ' + ('复用已验证的结果' if cached else '正在生成并验证回顾') + '\n')
                ids = {e['id'] for e in data['evidence']}
                if cached:
                    result = validate_result(cached['output_json'], ids)
                else:
                    result, usage = runner(data, 'stage', threading.Event())
                    result = validate_result(json.dumps(result, ensure_ascii=False), ids)
                    validate_business_result(result, data)
                    with db:
                        db.execute('INSERT OR REPLACE INTO review_cache VALUES (?,?,?)',
                                   (fingerprint, json.dumps(result, ensure_ascii=False), json.dumps(usage)))
                validate_business_result(result, data)
                text = (f'## 工作概述 · {safe(result["overview"]["text"])}\n'
                        f'活动：{stamp(group[0]["ts"], args.timezone)} → {stamp(group[-1]["ts"], args.timezone)}\n')
                text += '\n'.join(business_lines(result)) + '\n'
                text += '\n'
                emit(text)  # Flush only redacted, schema/citation-validated results, never raw model tokens.
                output += text
                consumed.extend(group)
            remaining = len(unseen) - len(consumed)
            footer = f'本次回顾 {len(consumed)} 条活动；还有 {remaining} 条留待下次。\n'
            if remaining:
                footer += '请再次运行同一命令继续；本次不是全部积压活动。\n'
            output += footer
            emit(footer)
            if args.out:
                write_new(args.out, output)
            review_id = uuid.uuid4().hex
            # Atomic commit: output/export failure or any model error leaves all events unconsumed.
            with db:
                db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)',
                           (review_id, scope, state['cursor_ts'], cutoff, cutoff, output,
                            json.dumps(coverage), runner.provider, runner.model))
                db.executemany('INSERT OR REPLACE INTO review_seen VALUES (?,?,?,?)',
                               [(scope, r['id'], activity_digest([r]), review_id) for r in consumed])
                db.execute('UPDATE review_state SET cursor_ts=? WHERE scope=?',
                           (max(state['cursor_ts'], max(r['ts'] for r in consumed)), scope))
            return {'status': 'success', 'events': len(consumed), 'remaining': remaining, 'review_id': review_id}
    finally:
        db.close()


def command(args):
    import sys

    def emit(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def progress(text):
        sys.stderr.write(text)
        sys.stderr.flush()

    try:
        review(args, emit, progress)
        return 0
    except (ReviewError, EvidenceUnavailable) as error:
        progress(str(error) + '\n')
        return 1
    except SummaryError:
        # SummaryError codes are internal, but never reflect arbitrary external diagnostics.
        progress('模型总结失败；记录点未推进。可重试同一命令，已验证的批次会被复用。\n')
        return 1
    except KeyboardInterrupt:
        progress('已取消；记录点未推进，可重试。\n')
        return 130
