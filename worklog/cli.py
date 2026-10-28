import argparse
import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import collect, event
from .core import bounds, usage
from .store import connect, markdown, report_data, salt, save
from .web import make_server


@contextmanager
def summary_write_lock(path):
    import fcntl
    descriptor = os.open(str(path) + '.summary.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Stop the summary service before purging') from None
        yield
    finally:
        os.close(descriptor)


def import_sources(args, day):
    if not args.pi_dir and not args.codex_dir:
        raise ValueError('Specify --pi-dir and/or --codex-dir explicitly')
    key = salt(args.db)
    summaries = []
    for source, directory in [('pi', args.pi_dir), ('codex', args.codex_dir)]:
        if directory:
            events, stats = collect(directory, source, key, args.titles, args.max_files)
            summaries.append(save(args.db, events, stats, day, args.timezone))
    return summaries


def demo(args, day):
    if Path(args.db).exists():
        raise ValueError('Demo requires a new database; never mix demo with real data')
    key = salt(args.db)
    start, _ = bounds(day, args.timezone)
    events = []
    for i, (provider, model) in enumerate([('demo', 'Model-A'), ('demo', 'Model-B')]):
        for j, kind in enumerate(['user', 'assistant', 'tool', 'assistant']):
            ts = start + 9 * 3600 + i * 600 + j * 60
            values = usage({'input': 100, 'cacheRead': 900, 'output': 50}) if kind == 'assistant' else None
            events.append(event(key, 'demo', str(i), 'fictional', f'{i}:{j}', ts, kind,
                provider, model, values=values,
                title='虚构演示：验证接口与错误处理' if kind == 'user' else ''))
    save(args.db, events, {'source': 'demo', 'selected': 0, 'synthetic': True}, day, args.timezone)
    return {'status': 'demo-created', 'events': len(events), 'date': day}


def serve(args, day):
    if not 0 <= args.port <= 65535 or not 10 <= args.interval <= 3600:
        raise ValueError('Invalid port or polling interval')
    if args.auto_summarize and (not args.enable_llm or not args.watch):
        raise ValueError('Automatic summaries require --enable-llm and --watch')
    if args.allow_excerpts and not args.enable_llm:
        raise ValueError('Excerpts require explicit --enable-llm')
    connect(args.db).close()
    stop = threading.Event()
    worker = None
    service = None
    server = None
    try:
        if args.watch:
            import_sources(args, day)
        if args.enable_llm:
            from .llm import PiRunner
            from .tasks import TaskService
            runner = PiRunner(args.summary_provider, args.summary_model)
            roots = {name: value for name, value in [('pi', args.pi_dir), ('codex', args.codex_dir)] if value}
            service = TaskService(args.db, runner, roots, args.allow_excerpts, args.auto_summarize,
                                  args.idle_minutes * 60, args.daily_summary_limit, args.timezone)
        if args.watch:
            def poll():
                while not stop.wait(args.interval):
                    try:
                        target = args.date or datetime.now(ZoneInfo(args.timezone)).date().isoformat()
                        import_sources(args, target)
                    except (OSError, ValueError, sqlite3.Error):
                        print('Import failed; private diagnostics suppressed.', file=sys.stderr)
            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
        server = make_server(args.db, day, args.timezone, args.port, service)
        print(f'Local dashboard: http://127.0.0.1:{server.server_port}/#token={server.access_token}', flush=True)
        print('Keep the full URL private. Ctrl+C stops server, polling and summary worker.', flush=True)
        if service:
            print(f'LLM: {service.runner.provider}/{service.runner.model}; excerpts={args.allow_excerpts}; '
                  f'auto={args.auto_summarize}; daily job limit={args.daily_summary_limit}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if server:
            server.server_close()
        if service:
            service.close()
        if worker:
            worker.join(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI Worklog — local activity analytics; optional explicit LLM summaries')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['import', 'report', 'serve', 'demo', 'purge']:
        sub = commands.add_parser(name)
        sub.add_argument('--db', default='data/worklog.sqlite3')
        sub.add_argument('--date')
        sub.add_argument('--timezone', default='Asia/Shanghai')
        if name in ['import', 'serve']:
            sub.add_argument('--pi-dir')
            sub.add_argument('--codex-dir')
            sub.add_argument('--titles', action='store_true')
            sub.add_argument('--max-files', type=int, default=20)
        if name == 'serve':
            sub.add_argument('--port', type=int, default=8766)
            sub.add_argument('--watch', action='store_true')
            sub.add_argument('--interval', type=int, default=30)
            sub.add_argument('--enable-llm', action='store_true', help='Allow summary calls via isolated Pi CLI')
            sub.add_argument('--allow-excerpts', action='store_true', help='Opt in to redacted user/assistant excerpts sent to chosen model')
            sub.add_argument('--summary-provider')
            sub.add_argument('--summary-model')
            sub.add_argument('--auto-summarize', action='store_true', help='Stage summaries for new idle activity only')
            sub.add_argument('--idle-minutes', type=int, default=10)
            sub.add_argument('--daily-summary-limit', type=int, default=10)
        if name == 'report':
            sub.add_argument('--out', help='New Markdown file only')
        if name == 'purge':
            choice = sub.add_mutually_exclusive_group(required=True)
            choice.add_argument('--before')
            choice.add_argument('--titles-only', action='store_true')
            choice.add_argument('--summaries-only', action='store_true', help='Delete local tasks and excerpt snapshots; stop LLM service first')
    args = parser.parse_args(argv)
    os.umask(0o077)
    day = args.date or datetime.now(ZoneInfo(args.timezone)).date().isoformat()
    bounds(day, args.timezone)
    if hasattr(args, 'max_files') and not 1 <= args.max_files <= 500:
        raise ValueError('--max-files must be between 1 and 500')
    if args.command == 'import':
        print(json.dumps(import_sources(args, day), ensure_ascii=False, indent=2))
    elif args.command == 'demo':
        print(json.dumps(demo(args, day), ensure_ascii=False))
    elif args.command == 'report':
        text = markdown(report_data(args.db, day, args.timezone))
        if args.out:
            with Path(args.out).open('x', encoding='utf-8') as stream:
                stream.write(text)
        else:
            print(text, end='')
    elif args.command == 'purge':
        db = connect(args.db)
        try:
            with summary_write_lock(args.db), db:
                if args.summaries_only:
                    changed = db.execute('DELETE FROM summaries').rowcount
                    db.execute('DELETE FROM tasks')
                elif args.titles_only:
                    changed = db.execute("UPDATE events SET title='' WHERE title != ''").rowcount
                else:
                    cutoff, _ = bounds(args.before, args.timezone)
                    changed = db.execute('DELETE FROM events WHERE ts < ?', (cutoff,)).rowcount
                    expired = [r[0] for r in db.execute('SELECT id FROM tasks WHERE start_ts<?', (cutoff,))]
                    for task_id in expired:
                        db.execute('DELETE FROM summaries WHERE task_id=?', (task_id,))
                        db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
            print(json.dumps({'changed': changed, 'scope': 'local worklog database only'}))
        finally:
            db.close()
    else:
        serve(args, day)
    return 0


def run():
    try:
        return main()
    except (ValueError, OSError, ZoneInfoNotFoundError, sqlite3.Error) as error:
        print(f'Command failed ({type(error).__name__}); check options and local service state.', file=sys.stderr)
        return 1
