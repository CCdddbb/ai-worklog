"""Reversible, day-scoped inbox snoozes; new activity returns to the inbox."""
import json
import re

from .core import bounds
from .evidence import activity_digest


def ignored_sessions(db):
    """Conversations explicitly excluded from automatic capture until restored."""
    return {row[0] for row in db.execute('SELECT session FROM ignored_sessions')}


def ignore_session(db, session, ignored, now):
    if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{16}', session):
        raise ValueError('Invalid session')
    if type(ignored) is not bool:
        raise TypeError('Explicit ignore state required')
    if ignored and not db.execute('SELECT 1 FROM events WHERE session=? LIMIT 1', (session,)).fetchone():
        raise ValueError('Unknown session')
    with db:
        if ignored:
            db.execute('INSERT OR REPLACE INTO ignored_sessions VALUES (?,?)', (session, now))
            db.execute('DELETE FROM session_triage WHERE session=?', (session,))
        else:
            db.execute('DELETE FROM ignored_sessions WHERE session=?', (session,))
    return {'session': session, 'ignored': ignored}


def deferred_sessions(db, day, zone):
    start, end = bounds(day, zone)
    saved = {row['session']: row['digest'] for row in db.execute(
        'SELECT session,digest FROM session_triage WHERE day=? AND zone=?', (day, zone))}
    if not saved:
        return set()
    groups = {}
    for row in db.execute('SELECT id,session,ts,status,kind FROM events WHERE ts>=? AND ts<?', (start, end)):
        if row['session'] in saved:
            groups.setdefault(row['session'], []).append(dict(row))
    return {session for session, rows in groups.items() if activity_digest(rows) == saved[session]}


def defer_session(db, session, day, zone, deferred, now):
    if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{16}', session):
        raise ValueError('Invalid session')
    if type(deferred) is not bool:
        raise TypeError('Explicit inbox state required')
    start, end = bounds(day, zone)
    rows = [dict(row) for row in db.execute(
        'SELECT id,session,ts,status,kind FROM events WHERE session=? AND ts>=? AND ts<?',
        (session, start, end))]
    if not rows:
        raise ValueError('Unknown session in selected day')
    if deferred and any(session in json.loads(row['sessions']) for row in db.execute(
            'SELECT sessions FROM tasks WHERE day=?', (day,))):
        raise ValueError('Assigned session cannot be snoozed')
    with db:
        if deferred:
            db.execute('INSERT OR REPLACE INTO session_triage VALUES (?,?,?,?,?)',
                       (session, day, zone, activity_digest(rows), now))
        else:
            db.execute('DELETE FROM session_triage WHERE session=? AND day=? AND zone=?',
                       (session, day, zone))
    return {'session': session, 'deferred': deferred, 'date': day}
