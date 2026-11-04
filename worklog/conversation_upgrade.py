"""One-time local upgrade: retain legacy audit text, recheck mixed conversation evidence."""
import json

from .conversations import backfill, group_sessions
from .ledger import classify, recompute_times


def cites(group):
    items = [group['problem'], *group['actions'], *group['remaining'],
             *[item for item in (group['outcome'], group['verification']) if item]]
    return {cite for item in items for cite in item['evidence_ids']}


def upgrade(db):
    name = 'independent-conversation-projection-v3'
    if db.execute('SELECT 1 FROM local_migrations WHERE name=?', (name,)).fetchone():
        return set()
    snapshots = {}
    ambiguous = set()
    histories = {}
    for row in db.execute("SELECT h.*,s.input_json,s.task_id,s.finished_at,s.created_at FROM work_record_history h "
                          "JOIN summaries s ON s.id=h.job_id WHERE s.status='success'"):
        data, group = json.loads(row['input_json']), json.loads(row['group_json'])
        sessions = set(group_sessions(group, data))
        histories.setdefault(row['record_id'], []).append((dict(row), data, group, sessions))
        snapshots[row['job_id']] = data
    for record_id, entries in histories.items():
        sessions = set().union(*(entry[3] for entry in entries))
        if len(sessions) > 1:
            db.execute('INSERT OR IGNORE INTO legacy_shared_records VALUES (?)', (record_id,))
            for _, _, group, _ in entries:
                ambiguous.update(cites(group))
    unsafe_parts = {row[0] for row in db.execute('SELECT DISTINCT part_id FROM conversation_part_records '
                   'WHERE record_id IN (SELECT record_id FROM legacy_shared_records)')}
    changed = True
    while changed:
        changed = False
        for job_id, data in snapshots.items():
            if db.execute('SELECT 1 FROM legacy_context_jobs WHERE job_id=?', (job_id,)).fetchone():
                continue
            if any(item.get('id') in unsafe_parts for item in data.get('conversation_context', [])):
                changed = True
                db.execute('INSERT OR IGNORE INTO legacy_context_jobs VALUES (?)', (job_id,))
                unsafe_parts.update(row[0] for row in db.execute('SELECT part_id FROM conversation_progress WHERE job_id=?', (job_id,)))
                for record_id, entries in histories.items():
                    for row, _, group, _ in entries:
                        if row['job_id'] == job_id:
                            db.execute('INSERT OR IGNORE INTO legacy_shared_records VALUES (?)', (record_id,))
                            ambiguous.update(cites(group))
    # Select the latest evidence time, not the time a historical backfill happened to run.
    for record_id, entries in histories.items():
        if db.execute('SELECT 1 FROM legacy_shared_records WHERE record_id=?', (record_id,)).fetchone():
            continue
        candidates = []
        for row, data, group, _ in entries:
            stamps = [item['ts'] for item in data['evidence'] if item['id'] in cites(group)]
            if stamps:
                candidates.append((max(stamps), row['recorded_at'], row, group))
        if not candidates:
            continue
        _, _, source, group = max(candidates, key=lambda value: value[:2])
        current = db.execute('SELECT * FROM work_records WHERE id=?', (record_id,)).fetchone()
        if not current:
            continue
        archived = current['archived_at'] if current['status'] == group['status'] else (
            source['finished_at'] or source['created_at']) if group['status'] in ('reported_resolved', 'discussed') else None
        category = current['category'] if current['category_override'] else classify(group)
        db.execute('UPDATE work_records SET title=?,status=?,group_json=?,category=?,source_job_id=?,source_task_id=?,archived_at=? WHERE id=?',
                   (group['title'], group['status'], json.dumps(group, ensure_ascii=False), category,
                    source['job_id'], source['task_id'], archived, record_id))
    for table in ('conversations', 'conversation_parts', 'conversation_part_records', 'conversation_progress',
                  'conversation_projection_jobs', 'work_record_sessions'):
        db.execute('DELETE FROM ' + table)
    backfill(db)
    # Do not inherit old mixed-context coverage: the new worker must inspect each dialogue separately once.
    for evidence_id in ambiguous:
        if evidence_id.startswith('e_'):
            db.execute('DELETE FROM automatic_coverage WHERE event_id LIKE ?', (evidence_id[2:] + '%',))
    queued = {row[0] for row in db.execute("SELECT task_id FROM summaries WHERE status='queued' "
                                         'AND task_id IN (SELECT task_id FROM conversation_sources)')}
    for task_id in queued:
        db.execute("UPDATE summaries SET status='interrupted',error='conversation_contract_migrated' WHERE task_id=? AND status='queued'", (task_id,))
    db.execute("UPDATE task_automation SET attempt_digest=NULL,last_error=NULL WHERE task_id IN (SELECT task_id FROM conversation_sources)")
    db.execute('DELETE FROM automation_scans')
    for row in db.execute('SELECT DISTINCT day FROM work_records'):
        recompute_times(db, row[0])
    db.execute('INSERT INTO local_migrations VALUES (?)', (name,))
    return queued
