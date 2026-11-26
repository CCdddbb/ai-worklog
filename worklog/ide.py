"""JetBrains IDE (IntelliJ IDEA) activity: read-only, metadata-only.

IDEA keeps one row per recently opened project in ``options/recentProjects.xml`` with the
last open/activation time. Only those timestamps and the project directory are read; no
source code, file names or editor content. Because the IDE overwrites the timestamp, the
agent polling every few minutes is what turns it into a timeline.
"""
import glob
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .adapters import event
from .core import identity

MAX_XML = 4 * 1024 * 1024
STAMPS = ('activationTimestamp', 'projectOpenTimestamp')


def find_options(home):
    """recentProjects.xml of every installed JetBrains IDE (IDEA Ultimate and Community)."""
    base = Path(home) / 'Library/Application Support/JetBrains'
    found = []
    for pattern in ('IntelliJIdea*', 'IdeaIC*'):
        found.extend(glob.glob(str(base / pattern / 'options' / 'recentProjects.xml')))
    return sorted(found)


def discover(home):
    return find_options(home)


def _projects(path):
    if path.stat().st_size > MAX_XML:
        return []
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError):
        return []
    home = str(Path.home())
    rows = []
    for entry in root.iter('entry'):
        key = entry.get('key') or ''
        if not key:
            continue
        key = key.replace('$USER_HOME$', home)
        stamps = []
        for option in entry.iter('option'):
            if option.get('name') in STAMPS:
                try:
                    value = int(option.get('value', ''))
                except ValueError:
                    continue
                if value > 0:
                    stamps.append(value / 1000.0)
        if stamps:
            rows.append((key, stamps))
    return rows


def collect_idea(files, salt, window=None):
    """Events and coverage for IDEA; the same shape as adapters.collect()."""
    from .projects import repository
    repository.cache_clear()
    events, stats = [], {'source': 'idea', 'discovered': len(files), 'selected': 0, 'unreadable_files': 0}
    for name in files:
        path = Path(name)
        try:
            rows = _projects(path)
        except OSError:
            stats['unreadable_files'] += 1
            continue
        stats['selected'] += 1
        for project, stamps in rows:
            if not os.path.isabs(project):
                continue
            for ts in stamps:
                if window and not window[0] <= ts <= window[1]:
                    continue
                day = int(ts // 86400)
                events.append(event(salt, 'idea', f'{project}:{day}', project, f'{project}:{ts:.0f}',
                                    ts, 'tool', provider='jetbrains', model='idea'))
    return events, stats


def save_ide(db_path, events, day, zone):
    """IDE activity lives in its own table: it is a presence signal, never a conversation."""
    from .core import bounds
    from .store import connect
    start, end = bounds(day, zone)
    rows = [(e['id'], e.get('project_name') or '未知项目', e['ts']) for e in events if start <= e['ts'] < end]
    db = connect(db_path)
    try:
        with db:
            db.executemany('INSERT OR IGNORE INTO ide_activity VALUES (?,?,?)', rows)
    finally:
        db.close()
    return len(rows)


def day_activity(db_path, day, zone):
    """[(project, first_ts, last_ts)] of IDEA activity for the day."""
    from .core import bounds
    from .store import connect
    start, end = bounds(day, zone)
    db = connect(db_path)
    try:
        return [tuple(r) for r in db.execute(
            'SELECT project_name,MIN(ts),MAX(ts) FROM ide_activity WHERE ts>=? AND ts<? '
            'GROUP BY project_name ORDER BY MIN(ts)', (start, end))]
    finally:
        db.close()


def project_dirs(files):
    """Absolute project directories known to the IDE, for Git auto-discovery."""
    result = []
    for name in files:
        try:
            result.extend(p for p, _ in _projects(Path(name)) if os.path.isabs(p))
        except OSError:
            pass
    return list(dict.fromkeys(result))


__all__ = ['discover', 'collect_idea', 'save_ide', 'day_activity', 'project_dirs', 'identity']
