"""Declarative purge specification.

Purging is a privacy promise, so it is described as data instead of a long
sequence of hand-written ``DELETE`` statements. A table that gains a column
pointing at tasks, work records, summaries or commits without being listed here
makes ``tests/test_purge.py`` fail, rather than silently surviving
``purge --summaries-only``.
"""
from .core import bounds

#: Every column that points at a parent row this module is responsible for.
REFERENCES = {
    'automatic_coverage': ('task_id', 'event_id'),
    'conversation_part_records': ('part_id', 'record_id'),
    'conversation_progress': ('part_id', 'job_id'),
    'conversation_projection_jobs': ('job_id',),
    'conversation_sources': ('task_id',),
    'legacy_context_jobs': ('job_id',),
    'legacy_shared_records': ('record_id',),
    'review_seen': ('event_id', 'review_id'),
    'summaries': ('task_id',),
    'task_automation': ('task_id',),
    'task_git': ('task_id', 'commit_id'),
    'task_reviews': ('task_id',),
    'work_record_history': ('record_id', 'job_id'),
    'work_record_sessions': ('record_id',),
    'work_records': ('source_task_id', 'source_job_id'),
}

#: How each table above is cleaned. ``wipe`` removes every row, ``orphan``
#: removes only rows whose parent row is gone.
POLICY = {
    'automatic_coverage': 'orphan',
    'conversation_part_records': 'wipe',
    'conversation_progress': 'wipe',
    'conversation_projection_jobs': 'wipe',
    'conversation_sources': 'orphan',
    'legacy_context_jobs': 'orphan',
    'legacy_shared_records': 'orphan',
    'review_seen': 'wipe',
    'summaries': 'task',
    'task_automation': 'orphan',
    'task_git': 'task',
    'task_reviews': 'task',
    'work_record_history': 'orphan',
    'work_record_sessions': 'wipe',
    'work_records': 'task',
}

#: Reference-like columns that point outside the purgeable scope (source logs).
IGNORED_REFERENCES = {('events', 'file_id')}

#: Tables built from conversation material; they hold no independent content.
CONVERSATION_TABLES = (
    'conversations',
    'conversation_parts',
    'conversation_part_records',
    'conversation_progress',
    'conversation_projection_jobs',
    'conversation_sources',
    'work_record_sessions',
    'legacy_shared_records',
    'legacy_context_jobs',
    'local_migrations',
)

#: Tables that exist only for the optional AI / automation feature set.
AUTOMATION_TABLES = ('task_automation', 'automatic_coverage', 'automation_scans', 'automation_settings')

#: Command-line review checkpoints kept by `bin/worklog-review`.
REVIEW_TABLES = ('reviews', 'review_cache', 'review_seen', 'review_state')

#: Tables whose rows are children of a task row.
TASK_CHILD_TABLES = ('summaries', 'task_reviews', 'task_git', 'task_automation', 'automatic_coverage',
                     'conversation_sources')


def _count(db, sql, params=()):
    return db.execute(sql, params).rowcount


def summaries_only(db):
    """Remove every AI-derived row, task and excerpt snapshot; keep source logs."""
    changed = _count(db, 'DELETE FROM summaries')
    changed += _count(db, "DELETE FROM session_names WHERE origin='summary'")
    changed += _count(db, 'DELETE FROM task_reviews')
    changed += _count(db, 'DELETE FROM task_git')
    db.execute('DELETE FROM tasks')
    db.execute('DELETE FROM work_records')
    db.execute('DELETE FROM work_record_history')
    for table in CONVERSATION_TABLES:
        db.execute('DELETE FROM ' + table)
    for table in AUTOMATION_TABLES:
        db.execute('DELETE FROM ' + table)
    for table in REVIEW_TABLES:
        changed += _count(db, 'DELETE FROM ' + table)
    return changed


def titles_only(db):
    """Drop automatic titles; manual names survive."""
    changed = _count(db, "UPDATE events SET title='' WHERE title != ''")
    changed += _count(db, "DELETE FROM session_names WHERE origin!='manual'")
    db.execute("UPDATE conversations SET title='未命名对话'")
    return changed


def before(db, day, zone):
    """Drop everything recorded before `day`, then rebuild what remains."""
    cutoff, _ = bounds(day, zone)
    changed = _count(db, 'DELETE FROM events WHERE ts < ?', (cutoff,))
    db.execute('DELETE FROM session_names WHERE session NOT IN (SELECT DISTINCT session FROM events)')
    changed += _count(db, 'DELETE FROM git_commits WHERE ts < ?', (cutoff,))
    db.execute('DELETE FROM task_git WHERE commit_id NOT IN (SELECT id FROM git_commits)')
    db.execute('DELETE FROM work_records WHERE day<?', (day,))
    db.execute('DELETE FROM work_record_history WHERE record_id NOT IN (SELECT id FROM work_records)')
    db.execute('DELETE FROM reviews WHERE created_at < ?', (cutoff,))
    db.execute('DELETE FROM import_days WHERE day < ?', (day,))
    db.execute('DELETE FROM session_triage WHERE day < ?', (day,))
    db.execute('DELETE FROM review_cache')
    expired = [row[0] for row in db.execute('SELECT id FROM tasks WHERE start_ts<?', (cutoff,))]
    for task_id in expired:
        db.execute('DELETE FROM summaries WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM task_reviews WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM task_git WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
    db.execute('DELETE FROM task_automation WHERE task_id NOT IN (SELECT id FROM tasks)')
    db.execute('DELETE FROM automatic_coverage WHERE task_id NOT IN (SELECT id FROM tasks)')
    db.execute('DELETE FROM activity_revisions WHERE day<?', (day,))
    db.execute('DELETE FROM automation_scans WHERE day<?', (day,))
    db.execute('DELETE FROM conversation_sources WHERE task_id NOT IN (SELECT id FROM tasks)')
    db.execute('DELETE FROM legacy_shared_records WHERE record_id NOT IN (SELECT id FROM work_records)')
    db.execute('DELETE FROM legacy_context_jobs WHERE job_id NOT IN (SELECT id FROM summaries)')
    db.execute('DELETE FROM automatic_coverage WHERE event_id NOT IN (SELECT id FROM events)')
    db.execute('DELETE FROM review_seen WHERE event_id NOT IN (SELECT id FROM events)')
    db.execute('DELETE FROM review_seen WHERE review_id NOT IN (SELECT id FROM reviews)')
    for table in ('conversations', 'conversation_parts', 'conversation_part_records', 'conversation_progress',
                  'conversation_projection_jobs', 'work_record_sessions'):
        db.execute('DELETE FROM ' + table)
    from .conversations import backfill
    backfill(db)
    db.execute('DELETE FROM projects WHERE id NOT IN (SELECT DISTINCT project FROM events)')
    db.execute('DELETE FROM project_aliases WHERE project NOT IN (SELECT id FROM projects)')
    return changed


def uncovered_references(db):
    """Return (table, column) pairs that reference parents but declare no policy.

    Used by tests to keep the specification honest as the schema grows.
    """
    missing = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})')]
        known = set(REFERENCES.get(table, ()))
        for column in columns:
            if column in known or (table, column) in IGNORED_REFERENCES:
                continue
            if column.endswith('_id') and column not in ('id',):
                missing.append((table, column))
    for table in REFERENCES:
        if table not in POLICY:
            missing.append((table, '<policy>'))
    return sorted(missing)


def purge(db, mode, day=None, zone='Asia/Shanghai'):
    """Entry point used by the CLI so every mode shares one specification."""
    with db:
        if mode == 'summaries':
            return summaries_only(db)
        if mode == 'titles':
            return titles_only(db)
        if mode == 'before':
            return before(db, day, zone)
    raise ValueError('Unknown purge mode')


__all__ = [
    'AUTOMATION_TABLES',
    'CONVERSATION_TABLES',
    'POLICY',
    'REFERENCES',
    'REVIEW_TABLES',
    'TASK_CHILD_TABLES',
    'before',
    'purge',
    'summaries_only',
    'titles_only',
    'uncovered_references',
]
