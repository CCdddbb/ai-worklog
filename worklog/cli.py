import argparse
import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import collect, event
from .core import TIMEOUT, bounds, usage
from .store import connect, markdown, period_markdown, report_data, salt, save
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
    workbuddy_dir = getattr(args, 'workbuddy_dir', None)
    if not args.pi_dir and not args.codex_dir and not workbuddy_dir and not args.git_repo:
        raise ValueError('Specify --pi-dir, --codex-dir, --workbuddy-dir and/or --git-repo explicitly')
    key = salt(args.db)
    # The same window save() keeps: files untouched since then cannot hold that day's events.
    start, end = bounds(day, args.timezone)
    window = (start - TIMEOUT, end + TIMEOUT)
    summaries = []
    for source, directory in [('pi', args.pi_dir), ('codex', args.codex_dir),
                              ('workbuddy', workbuddy_dir)]:
        if directory:
            events, stats = collect(directory, source, key, args.titles, args.max_files, window)
            summaries.append(save(args.db, events, stats, day, args.timezone))
    if args.git_repo:
        from .git_evidence import import_repo
        summaries.append(import_repo(args.git_repo, args.db, key, day, args.timezone))
    return summaries


def week_days(day):
    """Monday-to-Sunday calendar days that contain the anchor date."""
    anchor = date.fromisoformat(day)
    first = anchor - timedelta(days=anchor.weekday())
    return [(first + timedelta(days=offset)).isoformat() for offset in range(7)]


def refresh_week(args, day):
    """Re-scan elapsed days of the anchor week so old coverage gaps get corrected.

    Today is already imported at startup; future days are never scanned, because a
    coverage row would make an untouched day look like a day that was worked on.
    """
    for target in week_days(day):
        if target < day:
            import_sources(args, target)


def discover_sources(home):
    home = Path(home)
    pi = home / '.pi/agent/sessions'
    codex = home / '.codex/sessions'
    workbuddy = home / '.workbuddyydkj/projects'
    return {'pi_dir': str(pi) if pi.is_dir() else None,
            'codex_dir': str(codex) if codex.is_dir() else None,
            'workbuddy_dir': str(workbuddy) if workbuddy.is_dir() else None}


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
        from .tasks import TaskService
        runner = None
        if args.enable_llm:
            from .llm import create_runner
            runner = create_runner(args.summary_provider, args.summary_model)
        roots = {name: value for name, value in [('pi', args.pi_dir), ('codex', args.codex_dir),
                                                 ('workbuddy', getattr(args, 'workbuddy_dir', None))] if value}
        service = TaskService(args.db, runner, roots, args.allow_excerpts, args.auto_summarize,
                              args.idle_minutes * 60, args.daily_summary_limit, args.timezone)
        agent_config = {
            'db': args.db, 'timezone': args.timezone, 'git_repos': [], 'git_scan': getattr(args, 'git_scan', None) or [],
            'idea': not getattr(args, 'no_idea', False), 'digest_dir': getattr(args, 'digest_dir', None) or str(Path(args.db).parent / 'digests'),
            'digest_hour': getattr(args, 'digest_hour', None) or 18, 'notify': True}
        if args.watch:
            def poll():
                # One bounded pass repairs week rows written before scans were window-aware.
                try:
                    refresh_week(args, day)
                except (OSError, ValueError, sqlite3.Error):
                    print('Coverage refresh failed; private diagnostics suppressed.', file=sys.stderr)
                from . import agent as personal_agent
                while True:
                    try:
                        personal_agent.extras_cycle(agent_config)
                    except (OSError, ValueError, sqlite3.Error):
                        print('Agent cycle failed; private diagnostics suppressed.', file=sys.stderr)
                    if stop.wait(args.interval):
                        break
                    try:
                        target = args.date or datetime.now(ZoneInfo(args.timezone)).date().isoformat()
                        import_sources(args, target)
                    except (OSError, ValueError, sqlite3.Error):
                        print('Import failed; private diagnostics suppressed.', file=sys.stderr)
            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
        server = make_server(args.db, day, args.timezone, args.port, service, agent_config if args.watch else None)
        print(f'Local dashboard: http://127.0.0.1:{server.server_port}/#token={server.access_token}', flush=True)
        print('Keep the full URL private. Ctrl+C stops server, polling and summary worker.', flush=True)
        if service.runner:
            print(f'LLM: {service.runner.provider}/{service.runner.model}; excerpts={service.allow_excerpts}; '
                  f'auto={service.auto}; summaries=unlimited', flush=True)
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


def agent_command(args):
    from . import agent
    os.umask(0o077)
    if args.command == 'uninstall-agent':
        target, existed = agent.uninstall(args.plist)
        print(('Removed ' if existed else 'Not installed: ') + str(target))
        print('If it is running: launchctl bootout gui/%d/%s' % (os.getuid(), agent.LABEL))
        return 0
    found = discover_sources(Path.home())
    config = {
        'db': args.db or 'data/worklog.sqlite3', 'timezone': args.timezone or 'Asia/Shanghai',
        'pi_dir': args.pi_dir or found['pi_dir'], 'codex_dir': args.codex_dir or found['codex_dir'],
        'workbuddy_dir': args.workbuddy_dir or found['workbuddy_dir'],
        'git_repos': args.git_repo, 'git_scan': args.git_scan, 'idea': not args.no_idea,
        'interval': int(args.interval or 300), 'digest_dir': args.digest_dir or 'data/digests',
        'digest_hour': int(args.digest_hour or 18), 'notify': not getattr(args, 'no_notify', False),
        'enable_llm': args.enable_llm, 'allow_excerpts': args.allow_excerpts,
        'summary_provider': getattr(args, 'summary_provider', None),
        'summary_model': getattr(args, 'summary_model', None),
        'idle_minutes': getattr(args, 'idle_minutes', 3),
    }
    ZoneInfo(config['timezone'])
    if not 30 <= config['interval'] <= 3600 or not 0 <= config['digest_hour'] <= 23:
        raise ValueError('Invalid interval or digest hour')
    if args.command == 'install-agent':
        target = agent.install(config, args.plist)
        print('Wrote ' + str(target))
        print('Start it with:\n  ' + '\n  '.join(agent.load_commands(target)))
        return 0
    print('Sources: ' + ', '.join(n for n, v in [('Pi', config['pi_dir']), ('Codex', config['codex_dir']),
          ('WorkBuddy', config['workbuddy_dir']), ('IDEA', config['idea']),
          ('Git', config['git_repos'] or config['git_scan'])] if v), file=sys.stderr, flush=True)
    agent.run(config, once=args.once)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI Worklog — local activity analytics; optional explicit LLM summaries')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['import', 'report', 'serve', 'start', 'demo', 'purge', 'review']:
        sub = commands.add_parser(name)
        sub.add_argument('--db', default='data/worklog.sqlite3')
        if name != 'review':
            sub.add_argument('--date')
        sub.add_argument('--timezone', default='Asia/Shanghai')
        if name in ['import', 'serve', 'start', 'review']:
            sub.add_argument('--pi-dir', help='Pi session log directory, e.g. ~/.pi/agent/sessions')
            sub.add_argument('--codex-dir', help='Codex CLI session log directory, e.g. ~/.codex/sessions')
            sub.add_argument('--workbuddy-dir', help='WorkBuddy (YDKJ) session log directory, e.g. ~/.workbuddyydkj/projects')
            if name != 'review':
                sub.add_argument('--git-repo', help='Explicit repository root for read-only commit metadata')
            if name != 'review':
                sub.add_argument('--titles', action='store_true')
            sub.add_argument('--max-files', type=int, default=500 if name == 'review' else 100)
        if name in ['serve', 'start']:
            sub.add_argument('--port', type=int, default=8766)
            if name == 'serve':
                sub.add_argument('--watch', action='store_true')
            sub.add_argument('--interval', type=int, default=30)
            sub.add_argument('--git-scan', action='append', default=[], help='Folder whose child directories are scanned for Git repositories')
            sub.add_argument('--no-idea', action='store_true', help='Do not read IntelliJ IDEA recent-project activity')
            sub.add_argument('--digest-dir', help='Where daily/weekly digests are written; default <db folder>/digests')
            sub.add_argument('--digest-hour', type=int, help='Local hour after which the daily digest is written (default 18)')
            sub.add_argument('--enable-llm', action='store_true', help='Allow summary calls via isolated Pi or CodeBuddy CLI')
            sub.add_argument('--allow-excerpts', action='store_true', help='Opt in to redacted user/assistant excerpts sent to chosen model')
            sub.add_argument('--summary-provider')
            sub.add_argument('--summary-model')
            sub.add_argument('--auto-summarize', action='store_true', help='Stage summaries for new idle activity only')
            sub.add_argument('--idle-minutes', type=int, default=3,
                             help='Minutes of quiet after a finished turn before recording; same default as the UI')
            sub.add_argument('--daily-summary-limit', type=int, default=None, help='Deprecated compatibility flag; ignored, summaries are unlimited')
        if name == 'review':
            boundary = sub.add_mutually_exclusive_group()
            boundary.add_argument('--start-now', action='store_true', help='Create initial checkpoint without reading logs or calling a model')
            boundary.add_argument('--since', help='First historical start, ISO timestamp with timezone offset')
            boundary.add_argument('--last', action='store_true', help='Show latest saved review, without importing or model calls')
            consent = sub.add_mutually_exclusive_group()
            consent.add_argument('--allow-excerpts', action='store_true', default=None, help='Persist consent for bounded redacted excerpts in this source scope')
            consent.add_argument('--metadata-only', action='store_false', dest='allow_excerpts', help='Revoke excerpt consent for this source scope')
            sub.add_argument('--preview', action='store_true', help='Preview bounded material; never call a model or consume events')
            sub.add_argument('--allow-partial', action='store_true', help='Explicitly accept source coverage gaps, which remain disclosed')
            sub.add_argument('--batch-limit', type=int, default=4, help='1–12 batches of at most 24 evidence events per invocation')
            sub.add_argument('--summary-provider')
            sub.add_argument('--summary-model')
            sub.add_argument('--out', help='Also export to a new Markdown file; never overwrite')
        if name == 'report':
            sub.add_argument('--out', help='New Markdown file only')
            sub.add_argument('--period', choices=['day', 'week', 'month'], default='day')
            sub.add_argument('--detail', choices=['brief', 'full'], default='brief', help='Brief delivery or full review report')
        if name == 'purge':
            choice = sub.add_mutually_exclusive_group(required=True)
            choice.add_argument('--before')
            choice.add_argument('--titles-only', action='store_true')
            choice.add_argument('--summaries-only', action='store_true', help='Delete local tasks and excerpt snapshots; stop LLM service first')
    agent = commands.add_parser('agent', help='Resident headless agent: collect, summarize, deliver digests')
    agent.add_argument('--db', default='data/worklog.sqlite3')
    agent.add_argument('--timezone', default='Asia/Shanghai')
    agent.add_argument('--pi-dir')
    agent.add_argument('--codex-dir')
    agent.add_argument('--workbuddy-dir')
    agent.add_argument('--git-repo', action='append', default=[], help='Repository root; repeatable')
    agent.add_argument('--git-scan', action='append', default=[], help='Folder whose child directories are scanned for repositories')
    agent.add_argument('--no-idea', action='store_true', help='Do not read IntelliJ IDEA recent-project activity')
    agent.add_argument('--interval', type=int, default=300, help='Seconds between collection cycles')
    agent.add_argument('--digest-dir', default='data/digests')
    agent.add_argument('--digest-hour', type=int, default=18, help='Local hour after which the daily digest is written')
    agent.add_argument('--no-notify', action='store_true')
    agent.add_argument('--once', action='store_true', help='Run one cycle and exit')
    agent.add_argument('--enable-llm', action='store_true')
    agent.add_argument('--allow-excerpts', action='store_true')
    agent.add_argument('--summary-provider')
    agent.add_argument('--summary-model')
    agent.add_argument('--idle-minutes', type=int, default=3)
    for name in ['install-agent', 'uninstall-agent']:
        sub = commands.add_parser(name, help='Register/remove the agent as a macOS login service (launchd)')
        sub.add_argument('--plist', help='Target plist path; default ~/Library/LaunchAgents')
        if name == 'install-agent':
            for flag in ['--db', '--timezone', '--pi-dir', '--codex-dir', '--workbuddy-dir', '--interval',
                         '--digest-dir', '--digest-hour']:
                sub.add_argument(flag)
            sub.add_argument('--git-repo', action='append', default=[])
            sub.add_argument('--git-scan', action='append', default=[])
            sub.add_argument('--no-idea', action='store_true')
            sub.add_argument('--enable-llm', action='store_true')
            sub.add_argument('--allow-excerpts', action='store_true')
    args = parser.parse_args(argv)
    if args.command in ('agent', 'install-agent', 'uninstall-agent'):
        return agent_command(args)
    if args.command == 'start':
        found = discover_sources(Path.home())
        args.pi_dir = args.pi_dir or found['pi_dir']
        args.codex_dir = args.codex_dir or found['codex_dir']
        args.workbuddy_dir = args.workbuddy_dir or found['workbuddy_dir']
        args.watch = True
        print('Detected sources: ' + ', '.join(name for name, path in
              [('Pi', args.pi_dir), ('Codex', args.codex_dir), ('WorkBuddy', args.workbuddy_dir),
               ('Git', args.git_repo)] if path), flush=True)
    os.umask(0o077)
    day = getattr(args, 'date', None) or datetime.now(ZoneInfo(args.timezone)).date().isoformat()
    bounds(day, args.timezone)
    if hasattr(args, 'max_files') and not 1 <= args.max_files <= 500:
        raise ValueError('--max-files must be between 1 and 500')
    if args.command == 'review':
        from .review import command
        return command(args)
    if args.command == 'import':
        print(json.dumps(import_sources(args, day), ensure_ascii=False, indent=2))
    elif args.command == 'demo':
        print(json.dumps(demo(args, day), ensure_ascii=False))
    elif args.command == 'report':
        text = (markdown(report_data(args.db, day, args.timezone), args.detail, args.db) if args.period == 'day'
                else period_markdown(args.db, day, args.timezone, args.period, args.detail))
        if args.out:
            with Path(args.out).open('x', encoding='utf-8') as stream:
                stream.write(text)
        else:
            print(text, end='')
    elif args.command == 'purge':
        from .purge import purge
        from .review import review_write_lock
        db = connect(args.db)
        try:
            with summary_write_lock(args.db), review_write_lock(args.db), db:
                if args.summaries_only:
                    mode = 'summaries'
                elif args.titles_only:
                    mode = 'titles'
                else:
                    mode = 'before'
                changed = purge(db, mode, args.before, args.timezone)
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
