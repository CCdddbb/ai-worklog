"""Explicit, read-only Git commit metadata import. No diffs or repository paths are stored."""
import hashlib
import hmac
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .core import bounds, redact_title
from .store import connect


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError('Git repository is unavailable')
    if len(result.stdout) > 2_000_000:
        raise ValueError('Git metadata exceeds local import limit')
    return result.stdout


def import_repo(path, db_path, key, day, zone):
    repo = Path(path).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise ValueError('Git repository must be a directory')
    root = Path(_git(repo, 'rev-parse', '--show-toplevel').strip()).resolve(strict=True)
    if root != repo:
        raise ValueError('Select the Git repository root explicitly')
    start, end = bounds(day, zone)
    project = hmac.new(key, str(root).encode(), hashlib.sha256).hexdigest()[:16]
    earliest = datetime.fromtimestamp(start, timezone.utc).isoformat()
    latest = datetime.fromtimestamp(end, timezone.utc).isoformat()
    output = _git(repo, 'log', '--no-ext-diff', '--no-show-signature', '-n', '101',
                  '--since=' + earliest, '--until=' + latest, '--format=%H%x1f%ct%x1f%s')
    lines = output.splitlines()
    db = connect(db_path)
    try:
        known = {row[0] for row in db.execute('SELECT id FROM git_commits')}
    finally:
        db.close()
    rows = []
    for line in lines[:100]:
        parts = line.split('\x1f', 2)
        if len(parts) != 3 or not re.fullmatch(r'[a-f0-9]{40,64}', parts[0]):
            continue
        commit_id, raw_ts, subject = parts
        ts = float(raw_ts)
        if not start <= ts < end:
            continue
        if commit_id in known:
            continue
        stat = _git(repo, 'show', '--no-ext-diff', '--no-renames', '--format=', '--shortstat', commit_id)
        files = re.search(r'(\d+) files? changed', stat)
        added = re.search(r'(\d+) insertions?\(\+\)', stat)
        deleted = re.search(r'(\d+) deletions?\(-\)', stat)
        rows.append((commit_id, project, ts, redact_title(subject),
                     int(files.group(1)) if files else 0,
                     int(added.group(1)) if added else 0,
                     int(deleted.group(1)) if deleted else 0))
    db = connect(db_path)
    try:
        with db:
            db.executemany('''INSERT INTO git_commits VALUES (?,?,?,?,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET subject=excluded.subject,
                              files_changed=excluded.files_changed,additions=excluded.additions,
                              deletions=excluded.deletions''', rows)
            coverage = {'source': 'git:' + project, 'date': day, 'timezone': zone, 'selected': min(len(lines), 100),
                        'more_commits': len(lines) > 100, 'imported_commits': len(rows)}
            db.execute('DELETE FROM imports WHERE source=?', (coverage['source'],))
            db.execute("INSERT INTO imports VALUES (?,datetime('now'),?)",
                       (coverage['source'], json.dumps(coverage)))
            db.execute('INSERT OR REPLACE INTO import_days VALUES (?,?,?,?)',
                       (coverage['source'], day, zone, json.dumps(coverage)))
    finally:
        db.close()
    return coverage
