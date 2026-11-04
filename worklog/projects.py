"""Local repository identity, without executing Git, hooks or reading source files."""
from functools import lru_cache
from pathlib import Path

from .core import identity, label


def _small_text(path):
    with path.open('rb') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError('Oversized Git locator')
    return raw.decode('utf-8').strip()


@lru_cache(maxsize=256)
def repository(cwd):
    """Return a common Git directory and display name, or the original cwd fallback."""
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        return cwd, '未知项目' if cwd == 'unknown' else label(path.name)
    try:
        path = path.resolve()
        for parent in (path, *path.parents):
            marker = parent / '.git'
            if marker.is_dir():
                git = marker.resolve()
            elif marker.is_file():
                text = _small_text(marker)
                if not text.startswith('gitdir:'):
                    continue
                git = (parent / text[7:].strip()).resolve()
            else:
                continue
            if not git.is_dir():
                continue
            common_file = git / 'commondir'
            common = (git / _small_text(common_file)).resolve() if common_file.is_file() else git
            name = common.parent.name if common.name == '.git' else parent.name
            return 'repository:' + str(common), label(name)
    except (OSError, ValueError, UnicodeError):
        pass
    return str(path), label(path.name) or '未知项目'


def migrate(db):
    """Canonicalize old cwd identities without changing event/session IDs or original logs."""
    import json
    aliases = dict(db.execute('SELECT alias,project FROM project_aliases WHERE alias!=project'))
    if not aliases:
        return
    for alias, canonical in aliases.items():
        db.execute('UPDATE events SET project=? WHERE project=?', (canonical, alias))
        db.execute('UPDATE conversations SET project=? WHERE project=?', (canonical, alias))
        db.execute('UPDATE task_automation SET project=? WHERE project=?', (canonical, alias))
    for row in db.execute('SELECT id,projects_json FROM work_records'):
        original = json.loads(row['projects_json'])
        canonical = sorted({aliases.get(project, project) for project in original})
        if canonical != original:
            db.execute('UPDATE work_records SET projects_json=? WHERE id=?', (json.dumps(canonical), row['id']))


def project_metadata(key, cwd):
    canonical, name = repository(cwd)
    return {'project': identity(key, canonical)[:12], 'project_name': name,
            'project_alias': identity(key, cwd)[:12]}
