"""Local display names; never rewrite original session files or infer outcomes."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .core import redact_title
from .triage import deferred_sessions, ignored_sessions


def topic_name(titles):
    for raw in titles:
        text = redact_title(raw).strip()
        if not text or text.startswith(('<', '#', '[', '/')):
            continue
        text = re.sub(r'^(?:请你|请帮我|帮我|请)\s*', '', text)
        text = re.split(r'[。！？\n]', text)[0].strip()
        if len(text) >= 3:
            return text[:36] + ('…' if len(text) > 36 else '')
    return ''


def session_label(db, session, titles, source, first, zone, linked=None):
    """Display name for a conversation: manual, linked task, topic excerpt, then source/time."""
    named = db.execute('SELECT name,origin FROM session_names WHERE session=?', (session,)).fetchone()
    if named and named['name']:
        return named['name'], named['origin']
    if linked:
        return linked, 'task'
    topic = topic_name(titles)
    if topic:
        return topic, 'excerpt'
    local_time = datetime.fromtimestamp(first, ZoneInfo(zone)).strftime('%H:%M')
    return f'{str(source).capitalize()} · {local_time} 的协作', 'metadata'


def decorate_sessions(db, data):
    deferred = deferred_sessions(db, data['date'], data['timezone'])
    ignored = ignored_sessions(db)
    linked = {}
    for row in db.execute('SELECT title,sessions FROM tasks WHERE day=? ORDER BY created_at', (data['date'],)):
        for session in json.loads(row['sessions']):
            linked[session] = row['title']
    for session in data['sessions']:
        session['ignored'] = session['session'] in ignored
        session['deferred'] = (session['session'] in deferred and session['session'] not in linked
                               and not session['ignored'])
        session['display_name'], session['name_source'] = session_label(
            db, session['session'], session['titles'], session['source'], session['first'],
            data['timezone'], linked.get(session['session']))
    return data
