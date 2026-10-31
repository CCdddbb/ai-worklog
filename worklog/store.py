"""Private local metadata storage. Source sessions are never modified."""
import json
import os
import sqlite3
from pathlib import Path

from .core import TIMEOUT, bounds, summarize

FIELDS = ['id', 'source', 'session', 'project', 'ts', 'kind', 'provider', 'model', 'status',
          'title', 'file_id', 'error_code', 'input', 'output', 'cache_read', 'cache_write', 'tokens', 'cost']


def connect(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError('Database symlinks are not supported')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(path), timeout=10)
    existing = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    application_id = db.execute('PRAGMA application_id').fetchone()[0]
    if existing and application_id != 0x41574C47:
        db.close()
        raise ValueError('Refusing to modify a database not owned by AI Worklog')
    db.execute('PRAGMA application_id=1096240199')
    db.execute('PRAGMA secure_delete=ON')
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, source TEXT, session TEXT, project TEXT, ts REAL,
        kind TEXT, provider TEXT, model TEXT, status TEXT, title TEXT, file_id TEXT,
        error_code TEXT, input INTEGER, output INTEGER, cache_read INTEGER,
        cache_write INTEGER, tokens INTEGER, cost REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS events_time ON events(ts)')
    db.execute('CREATE TABLE IF NOT EXISTS imports (source TEXT, imported_at TEXT, coverage TEXT)')
    db.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, title TEXT, day TEXT, sessions TEXT, start_ts REAL,
        end_ts REAL, status TEXT, created_at REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS summaries (
        id TEXT PRIMARY KEY, task_id TEXT, mode TEXT, fingerprint TEXT,
        activity_digest TEXT, status TEXT, input_json TEXT, output_json TEXT,
        error TEXT, provider TEXT, model TEXT, created_at REAL, finished_at REAL,
        usage_json TEXT, started_at REAL, UNIQUE(task_id, mode, fingerprint))''')
    if 'started_at' not in {row[1] for row in db.execute('PRAGMA table_info(summaries)')}:
        db.execute('ALTER TABLE summaries ADD COLUMN started_at REAL')
    db.execute('CREATE INDEX IF NOT EXISTS summaries_task ON summaries(task_id, created_at)')
    db.execute('''CREATE TABLE IF NOT EXISTS task_reviews (
        task_id TEXT PRIMARY KEY, status TEXT NOT NULL, outcome TEXT NOT NULL,
        learnings TEXT NOT NULL, next_steps TEXT NOT NULL, verification TEXT NOT NULL,
        updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS git_commits (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, ts REAL NOT NULL,
        subject TEXT NOT NULL, files_changed INTEGER NOT NULL,
        additions INTEGER NOT NULL, deletions INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS git_commits_time ON git_commits(ts)')
    db.execute('''CREATE TABLE IF NOT EXISTS task_git (
        task_id TEXT NOT NULL, commit_id TEXT NOT NULL,
        PRIMARY KEY(task_id, commit_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_state (
        scope TEXT PRIMARY KEY, start_ts REAL, cursor_ts REAL, excerpts INTEGER)''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_seen (
        scope TEXT, event_id TEXT, digest TEXT, review_id TEXT,
        PRIMARY KEY(scope, event_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_cache (
        fingerprint TEXT PRIMARY KEY, output_json TEXT, usage_json TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, scope TEXT, start_ts REAL, end_ts REAL,
        created_at REAL, output TEXT, coverage_json TEXT, provider TEXT, model TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS session_names (
        session TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL)''')
    return db


def salt(path):
    key = Path(path).expanduser().parent / 'identity.key'
    if key.is_symlink():
        raise ValueError('Identity key symlinks are not supported')
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not key.exists() and Path(path).expanduser().exists():
        db = connect(path)
        try:
            if (db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM git_commits').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM review_state').fetchone()[0]
                    or db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]):
                raise ValueError('Identity key missing for existing events; restore it instead of re-keying')
        finally:
            db.close()
    try:
        with key.open('xb') as stream:
            os.chmod(key, 0o600)
            stream.write(os.urandom(32))
    except FileExistsError:
        pass
    raw = key.read_bytes()
    if len(raw) != 32:
        raise ValueError('Invalid local identity key; do not silently change event identities')
    os.chmod(key, 0o600)
    return raw


def save(path, events, coverage, day, zone):
    start, end = bounds(day, zone)
    selected = [e for e in events if start - TIMEOUT <= e['ts'] < end + TIMEOUT]
    placeholders = ','.join('?' for _ in FIELDS)
    updates = ','.join(f'{k}=excluded.{k}' for k in FIELDS if k != 'id')
    db = connect(path)
    try:
        before = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with db:
            db.executemany(f'INSERT INTO events ({",".join(FIELDS)}) VALUES ({placeholders}) '
                           f'ON CONFLICT(id) DO UPDATE SET {updates}', [[e[k] for k in FIELDS] for e in selected])
            # Keep only latest coverage per source; no source paths or raw errors.
            coverage = dict(coverage, date=day, timezone=zone, selected_events=len(selected))
            db.execute('DELETE FROM imports WHERE source=?', (coverage['source'],))
            db.execute("INSERT INTO imports VALUES (?,datetime('now'),?)", (coverage['source'], json.dumps(coverage)))
        after = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        return {'selected_events': len(selected), 'new_events': after - before, 'coverage': coverage}
    finally:
        db.close()


def report_data(path, day, zone):
    start, end = bounds(day, zone)
    db = connect(path)
    try:
        events = [dict(r) for r in db.execute('SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts',
                                            (start - TIMEOUT, end + TIMEOUT))]
        coverage = {r['source']: dict(json.loads(r['coverage']), imported_at=r['imported_at'])
                    for r in db.execute('SELECT * FROM imports')}
        from .presentation import decorate_sessions
        data = decorate_sessions(db, summarize(events, day, zone, coverage))
        data['incremental_reviews'] = [dict(row) for row in db.execute(
            'SELECT output,created_at,provider,model FROM reviews WHERE created_at>=? AND created_at<? '
            'ORDER BY created_at DESC LIMIT 10', (start, end))]
        drafts = []
        for task in db.execute('SELECT * FROM tasks WHERE day=? ORDER BY created_at', (day,)):
            job = db.execute("SELECT * FROM summaries WHERE task_id=? AND status='success' ORDER BY created_at DESC,rowid DESC LIMIT 1", (task['id'],)).fetchone()
            if job:
                captured = json.loads(job['input_json'])
                drafts.append({'task_id': task['id'], 'title': task['title'], 'mode': job['mode'],
                               'status': task['status'], 'provider': job['provider'], 'model': job['model'],
                               'coverage': captured['coverage'], 'result': json.loads(job['output_json'])})
                from .evidence import summary_quality
                drafts[-1]['quality'] = summary_quality(captured, drafts[-1]['result'])
        data['task_summaries'] = drafts
        reviews = []
        for row in db.execute('''SELECT t.id, t.title, r.status, r.outcome, r.learnings,
                                      r.next_steps, r.verification, r.updated_at
                               FROM tasks t JOIN task_reviews r ON r.task_id=t.id
                               WHERE t.day=? ORDER BY t.start_ts, t.id''', (day,)):
            item = dict(row)
            item['commits'] = [dict(commit) for commit in db.execute(
                '''SELECT c.id, c.subject, c.files_changed, c.additions, c.deletions
                   FROM task_git l JOIN git_commits c ON c.id=l.commit_id
                   WHERE l.task_id=? ORDER BY c.ts, c.id''', (row['id'],))]
            reviews.append(item)
        data['task_reviews'] = reviews
        if drafts:
            data['limitations'][-1] = 'LLM总结为证据范围内的待审草稿，引用有效不等于成果已验证。'
        return data
    finally:
        db.close()


def markdown(data):
    from .reporting import draft_lines
    lines = [f'# AI 工作日报 · {data["date"]}', '', f'时区：{data["timezone"]}', '',
             '## LLM任务总结（待人工审核）', '']
    if not data.get('task_summaries'):
        lines += ['尚无有效工作总结。请授权正文摘录并生成任务总结；下面的活动统计不能替代工作结论。', '']
    for draft in data.get('task_summaries', []):
        lines += draft_lines(draft)
    lines += ['## 活动事实（附录统计，不是工作成果）', '', f'- 观察到 {len(data["sessions"])} 个会话，{data["request_count"]} 条模型回复记录。',
        f'- 活跃区间估计 {data["estimated_active_seconds"] / 60:.1f} 分钟（并行去重，非真实编码工时）。',
        f'- 记录 Token 合计 {data["tokens"]:,}，运行异常/取消记录 {data["error_count"]} 条。', '',
        '## 模型用量', '', '| Provider / Model | 回复记录 | 输入(非缓存) | 缓存读 | 缓存写 | 输出 |',
        '|---|---:|---:|---:|---:|---:|']
    for m in data['models']:
        lines.append(f'| {m["provider"]}/{m["model"]} | {m["requests"]} | {m["input"]} | '
                     f'{m["cache_read"]} | {m["cache_write"]} | {m["output"]} |')
    lines += ['', '## 会话线索（不是成果确认）', '']
    for session in data['sessions']:
        lines.append(f'- {safe_markdown(session.get("display_name", session["session"]))} · 匿名项目 {session["project"]} · '
                     f'{session["source"]} · {session["tokens"]:,} Token · 异常 {session["errors"]}')
        for title in session['titles']:
            # Render untrusted titles as quoted text, not links/HTML.
            safe = title.replace('<', '＜').replace('>', '＞').replace('[', '［').replace(']', '］').replace('`', '｀')
            lines.append('  - 讨论标题：' + safe)
    if not data['sessions']:
        lines.append('- 本日期没有已导入的活动，不能推断当天没有工作。')
    lines += ['', '## 人工审阅的任务', '']
    if data.get('task_reviews'):
        for review in data['task_reviews']:
            status = {'confirmed': '已确认', 'in_progress': '进行中', 'blocked': '受阻'}[review['status']]
            lines.append(f'- {safe_markdown(review["title"])}（{status}）：{safe_markdown(review["outcome"])}')
            for key, label in [('learnings', '经验'), ('verification', '验证'), ('next_steps', '下一步')]:
                if review[key]:
                    lines.append(f'  - {label}：{safe_markdown(review[key])}')
            for commit in review['commits']:
                lines.append(f'  - 关联 Git 提交：{commit["id"][:12]} · {safe_markdown(commit["subject"])}；'
                             f'{commit["files_changed"]} 个文件，+{commit["additions"]}/-{commit["deletions"]} 行。'
                             '关联由用户选择，不代表测试或验收通过。')
    else:
        lines.append('- 待人工补充；AI草稿不自动算作成果。')
    lines += ['', '## 数据覆盖', '']
    for source, coverage in data['coverage'].items():
        if source.startswith('git:'):
            lines.append(f'- {source}：最近导入日期 {coverage.get("date")}，扫描 {coverage.get("selected", 0)} 个提交，'
                         f'超过上限：{"是" if coverage.get("more_commits") else "否"}。')
            continue
        lines.append(f'- {source}：最近导入日期 {coverage.get("date")}，读取 {coverage.get("selected", 0)} 个文件，'
                     f'未选 {coverage.get("omitted_files", 0)} 个，过大 {coverage.get("oversized_files", 0)} 个，'
                     f'格式错误 {coverage.get("malformed_lines", 0)} 行，未完整写入 {coverage.get("partial_lines", 0)} 行。')
        notices = {'oversized_lines': '过长跳过行', 'truncated_files': '读取被截断文件',
                   'unreadable_files': '不可读文件', 'invalid_timestamps': '无效时间戳',
                   'counter_resets': '累计计数回退', 'nested_usage_omitted': '未合并的工具嵌套用量',
                   'legacy_usage_suppressed': '为避免重复而抑制的旧格式用量', 'symlinks_skipped': '跳过符号链接'}
        for key, name in notices.items():
            if coverage.get(key):
                lines.append(f'  - {name}：{coverage[key]}。')
    lines += ['', '## 口径与限制', ''] + ['- ' + s for s in data['limitations']]
    return '\n'.join(lines) + '\n'


def safe_markdown(value):
    return str(value).replace('<', '＜').replace('>', '＞').replace('[', '［').replace(']', '］').replace('`', '｀').replace('\n', ' ')


def period_markdown(path, day, zone, period):
    import calendar
    from datetime import date, timedelta
    selected = date.fromisoformat(day)
    if period == 'week':
        first = selected - timedelta(days=selected.weekday())
        last = first + timedelta(days=6)
        label = '周报'
    elif period == 'month':
        first = selected.replace(day=1)
        last = selected.replace(day=calendar.monthrange(selected.year, selected.month)[1])
        label = '月报'
    else:
        raise ValueError('Invalid report period')
    days = []
    cursor = first
    while cursor <= last:
        days.append(report_data(path, cursor.isoformat(), zone))
        cursor += timedelta(days=1)
    lines = [f'# AI 工作{label} · {first} 至 {last}', '', '## 人工审阅的任务', '']
    count = 0
    for data in days:
        for review in data['task_reviews']:
            count += 1
            lines.extend([f'### {data["date"]} · {safe_markdown(review["title"])}', ''])
            lines.append('- 状态：' + {'confirmed': '已确认', 'in_progress': '进行中',
                                      'blocked': '受阻'}[review['status']])
            lines.append(f'- 工作结果：{safe_markdown(review["outcome"])}')
            for key, label_name in [('learnings', '经验收获'), ('verification', '验证方式'), ('next_steps', '下一步')]:
                if review[key]:
                    lines.append(f'- {label_name}：{safe_markdown(review[key])}')
            for commit in review['commits']:
                lines.append(f'- 关联提交：{commit["id"][:12]} · {safe_markdown(commit["subject"])}')
            lines.append('')
    if not count:
        lines.append('- 本周期没有人工审阅的任务。')
    from .reporting import draft_lines
    lines += ['', '## 工作总结草稿（待人工审核）', '']
    for data in days:
        for draft in data['task_summaries']:
            lines.append('日期：' + data['date'])
            lines.extend(draft_lines(draft))
    lines += ['', '## 活动统计（仅供参考）', '',
              f'- 已导入会话活动 {sum(day["event_count"] for day in days)} 条，模型回复 {sum(day["request_count"] for day in days)} 条。',
              f'- 记录 Token {sum(day["tokens"] for day in days):,}；此数字不代表工作成果或实际费用。',
              '', '未记录在 AI 会话中的工作以及未导入的日期不在统计范围内；人工确认状态由用户填写。']
    return '\n'.join(lines) + '\n'
