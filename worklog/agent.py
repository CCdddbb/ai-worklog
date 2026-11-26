"""Resident, headless work-log agent.

Every cycle it imports Pi / Codex / WorkBuddy / IDEA / Git evidence, optionally lets the
existing summary service stage task records, and writes a daily (and weekly) digest into a
folder once the configured evening time has passed. It runs under launchd, so it keeps
working when the dashboard App is closed.
"""
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import bounds, TIMEOUT
from .store import markdown, period_markdown, report_data, salt, save

LABEL = 'com.ai-worklog.agent'
DIGEST_NAME = re.compile(r'\d{4}-\d{2}-\d{2}(-week)?\.md')


def log(message):
    print(datetime.now().strftime('%H:%M:%S ') + message, file=sys.stderr, flush=True)


def git_repos(config, idea_files):
    """Explicit repositories plus Git roots found under --git-scan and IDEA's project list."""
    from .ide import project_dirs
    candidates = [Path(p).expanduser() for p in config.get('git_repos', [])]
    for root in config.get('git_scan', []):
        root = Path(root).expanduser()
        if root.is_dir():
            candidates.extend(child for child in sorted(root.iterdir()) if child.is_dir())
    candidates.extend(Path(p) for p in project_dirs(idea_files))
    repos = []
    for path in candidates:
        try:
            if (path / '.git').exists():
                repos.append(str(path.resolve()))
        except OSError:
            continue
    return list(dict.fromkeys(repos))


def import_day(config, day, sessions=True):
    """One bounded import pass for a day. Each source fails on its own.

    sessions=False skips Pi/Codex/WorkBuddy for callers (the dashboard) that import them already.
    """
    from .adapters import collect
    from .git_evidence import import_repo
    from .ide import collect_idea, discover as idea_discover, save_ide
    zone, db = config['timezone'], config['db']
    key = salt(db)
    start, end = bounds(day, zone)
    window = (start - TIMEOUT, end + TIMEOUT)
    done = []
    for source in (('pi', 'codex', 'workbuddy') if sessions else ()):
        directory = config.get(source + '_dir')
        if not directory:
            continue
        try:
            events, stats = collect(directory, source, key, False, config.get('max_files', 100), window)
            done.append(save(db, events, stats, day, zone)['new_events'])
        except (OSError, ValueError, sqlite3.Error):
            log(f'{source} import failed for {day}')
    idea_files = idea_discover(config.get('home', Path.home())) if config.get('idea', True) else []
    if idea_files:
        try:
            events, _ = collect_idea(idea_files, key, window)
            done.append(save_ide(db, events, day, zone))
        except (OSError, ValueError, sqlite3.Error):
            log(f'idea import failed for {day}')
    for repo in git_repos(config, idea_files):
        try:
            import_repo(repo, db, key, day, zone)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            pass  # Not a repository root, or Git unavailable: skip silently, never guess.
    return done


def notify(title, text):
    if sys.platform != 'darwin':
        return
    script = 'display notification {} with title {}'.format(_quote(text), _quote(title))
    try:
        subprocess.run(['osascript', '-e', script], timeout=10, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_digest(config, name, text):
    folder = Path(config['digest_dir']).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    try:
        with path.open('x', encoding='utf-8') as stream:  # Never overwrite a delivered digest.
            stream.write(text)
    except FileExistsError:
        return None
    return path


def ide_section(config, day):
    from .ide import day_activity
    rows = day_activity(config['db'], day, config['timezone'])
    if not rows:
        return ''
    zone = ZoneInfo(config['timezone'])
    clock = lambda ts: datetime.fromtimestamp(ts, zone).strftime('%H:%M')
    lines = ['', '## IDEA 项目活动（打开/激活时间，不是工时）', '']
    lines += [f'- {name}：{clock(first)}–{clock(last)}' for name, first, last in rows]
    return '\n'.join(lines) + '\n'


def has_activity(db_path, day, zone):
    from .store import connect
    start, end = bounds(day, zone)
    db = connect(db_path)
    try:
        return (db.execute('SELECT 1 FROM events WHERE ts>=? AND ts<? LIMIT 1', (start, end)).fetchone() is not None
                or db.execute('SELECT 1 FROM ide_activity WHERE ts>=? AND ts<? LIMIT 1', (start, end)).fetchone() is not None)
    finally:
        db.close()


def deliver(config, now):
    """Write today's digest after digest_hour, the weekly one on Sunday, and catch up yesterday."""
    zone = config['timezone']
    today = now.date()
    jobs = []
    yesterday = today - timedelta(days=1)
    jobs.append(('day', yesterday.isoformat()))
    if now.hour >= config['digest_hour']:
        jobs.append(('day', today.isoformat()))
        if today.weekday() == 6:
            jobs.append(('week', today.isoformat()))
    for period, day in jobs:
        suffix = '' if period == 'day' else '-week'
        name = f'{day}{suffix}.md'
        if (Path(config['digest_dir']).expanduser() / name).exists():
            continue
        if period == 'day':
            if not has_activity(config['db'], day, zone):
                continue  # No evidence, no digest: never fabricate an empty workday.
            text = markdown(report_data(config['db'], day, zone), 'brief', config['db']) + ide_section(config, day)
        else:
            text = period_markdown(config['db'], day, zone, 'week', 'brief')
        path = write_digest(config, name, text)
        if path:
            log(f'digest written: {path}')
            if config.get('notify', True):
                notify('AI Worklog', f'{"日报" if period == "day" else "周报"}已生成：{name}')


def extras_cycle(config, now=None):
    """Dashboard hook: IDEA + Git scan + digest delivery on top of the existing session import."""
    now = now or datetime.now(ZoneInfo(config['timezone']))
    for day in ((now.date() - timedelta(days=1)).isoformat(), now.date().isoformat()):
        import_day(config, day, sessions=False)
    deliver(config, now)


def list_digests(config):
    folder = Path(config['digest_dir']).expanduser()
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.glob('*.md') if DIGEST_NAME.fullmatch(p.name)), reverse=True)[:60]


def read_digest(config, name):
    if not DIGEST_NAME.fullmatch(name or ''):
        raise ValueError('Invalid digest name')
    path = Path(config['digest_dir']).expanduser() / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError('Digest unavailable')
    return path.read_text(encoding='utf-8')


def cycle(config, now=None):
    now = now or datetime.now(ZoneInfo(config['timezone']))
    today = now.date()
    import_day(config, (today - timedelta(days=1)).isoformat())
    import_day(config, today.isoformat())
    deliver(config, now)


def run(config, once=False):
    config = dict(config)
    connect_db(config['db'])
    service = None
    if config.get('enable_llm'):
        from .llm import create_runner
        from .tasks import TaskService
        runner = create_runner(config.get('summary_provider'), config.get('summary_model'))
        roots = {s: config[s + '_dir'] for s in ('pi', 'codex', 'workbuddy') if config.get(s + '_dir')}
        service = TaskService(config['db'], runner, roots, config.get('allow_excerpts', False), False,
                              config.get('idle_minutes', 3) * 60, None, config['timezone'])
        if config.get('allow_excerpts'):
            # The CLI flags are the explicit consent; "today" bounds history to this day onward,
            # and a restart restores the persisted policy instead of widening it.
            if not service.auto:
                service.configure_automation(True, 'today', True, config.get('idle_minutes', 3))
            log('LLM auto-summary enabled (redacted excerpts, from today)')
        else:
            log('LLM runner ready, but automatic summaries need --allow-excerpts consent')
    stop = threading.Event()
    try:
        while True:
            try:
                cycle(config)
            except (OSError, ValueError, sqlite3.Error):
                log('cycle failed; private diagnostics suppressed')
            if once or stop.wait(config['interval']):
                break
    except KeyboardInterrupt:
        pass
    finally:
        if service:
            service.close()


def connect_db(path):
    from .store import connect
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    connect(path).close()


def plist(config, python=None, script=None, workdir=None):
    """launchd definition that starts the agent at login and restarts it if it dies."""
    root = Path(workdir or Path(__file__).resolve().parent.parent)
    args = [python or sys.executable, str(script or root / 'app.py'), 'agent',
            '--db', str(Path(config['db']).expanduser().resolve()),
            '--digest-dir', str(Path(config['digest_dir']).expanduser().resolve()),
            '--timezone', config['timezone'], '--interval', str(config['interval']),
            '--digest-hour', str(config['digest_hour'])]
    for source in ('pi', 'codex', 'workbuddy'):
        if config.get(source + '_dir'):
            args += [f'--{source}-dir', config[source + '_dir']]
    for repo in config.get('git_repos', []):
        args += ['--git-repo', repo]
    for scan in config.get('git_scan', []):
        args += ['--git-scan', scan]
    if not config.get('idea', True):
        args.append('--no-idea')
    if config.get('enable_llm'):
        args += ['--enable-llm']
        if config.get('allow_excerpts'):
            args.append('--allow-excerpts')
    logfile = str(root / 'data' / 'agent.log')
    home = str(Path.home())
    path_env = ':'.join([
        f'{home}/.local/bin', f'{home}/.npm-global/bin',
        '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin',
    ])
    return {'Label': LABEL, 'ProgramArguments': args, 'WorkingDirectory': str(root),
            'EnvironmentVariables': {'PATH': path_env},
            'RunAtLoad': True, 'KeepAlive': True, 'ProcessType': 'Background',
            'StandardOutPath': logfile, 'StandardErrorPath': logfile}


def install(config, target=None):
    target = Path(target or Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist'))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('wb') as stream:
        plistlib.dump(plist(config), stream)
    return target


def uninstall(target=None):
    target = Path(target or Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist'))
    existed = target.exists()
    if existed:
        target.unlink()
    return target, existed


def load_commands(target):
    uid = os.getuid()
    return [f'launchctl bootstrap gui/{uid} "{target}"', f'launchctl kickstart -k gui/{uid}/{LABEL}']


__all__ = ['run', 'cycle', 'install', 'uninstall', 'plist', 'time']
