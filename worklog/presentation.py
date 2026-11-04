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


def decorate_sessions(db, data):
    names = {row['session']: dict(row) for row in db.execute('SELECT * FROM session_names')}
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
        named = names.get(session['session'])
        topic = topic_name(session['titles'])
        local_time = datetime.fromtimestamp(session['first'], ZoneInfo(data['timezone'])).strftime('%H:%M')
        session['display_name'] = (named['name'] if named else linked.get(session['session']) or topic
                                   or f'{session["source"].capitalize()} · {local_time} 的协作')
        session['name_source'] = (named['origin'] if named else 'task' if session['session'] in linked
                                  else 'excerpt' if topic else 'metadata')
    return data
