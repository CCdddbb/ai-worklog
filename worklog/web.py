"""Loopback dashboard; authenticated task actions never execute submitted code."""
import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import bounds
from .documents import report_document
from .evidence import EvidenceUnavailable
from .store import markdown, period_markdown, report_data
from .weekly import week_data

STATIC = Path(__file__).resolve().parents[1] / 'static'
# Four bounded 2,000-character review fields can exceed 16 KiB in UTF-8.
MAX_JSON_BODY = 65536


def make_server(db_path, day, zone, port=8766, task_service=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, status, content, content_type='application/json; charset=utf-8'):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, allow_nan=False)
            body = content.encode('utf-8') if isinstance(content, str) else content
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def access(self, private=True, mutation=False):
            host = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            origin = self.headers.get('Origin')
            if host not in allowed or (origin and origin != 'http://' + host) or (mutation and not origin):
                self.send(403, {'error': 'forbidden_origin'})
                return False
            if private and not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.send(401, {'error': 'local_token_required'})
                return False
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                      '/tasks.js': ('tasks.js', 'text/javascript'), '/dashboard.js': ('dashboard.js', 'text/javascript'),
                      '/conversations.js': ('conversations.js', 'text/javascript'), '/ledger.js': ('ledger.js', 'text/javascript'), '/weekly.js': ('weekly.js', 'text/javascript'), '/reports.js': ('reports.js', 'text/javascript'),
                      '/ledger.css': ('ledger.css', 'text/css'), '/style.css': ('style.css', 'text/css'), '/workbench.css': ('workbench.css', 'text/css'),
                      '/logo.svg': ('logo.svg', 'image/svg+xml'),
                      '/fonts/Manrope-Variable.ttf': ('fonts/Manrope-Variable.ttf', 'font/ttf')}
            if not self.access(private=url.path not in assets):
                return
            if url.path in assets:
                name, mime = assets[url.path]
                self.send(200, (STATIC / name).read_bytes(), mime + '; charset=utf-8')
                return
            if url.path not in ['/api/day', '/api/week', '/api/report', '/api/report-view', '/api/tasks', '/api/task-preview', '/api/work-records', '/api/work-record-history', '/api/conversations', '/api/conversation-progress', '/api/onboarding']:
                self.send(404, {'error': 'not_found'})
                return
            try:
                query = parse_qs(url.query)
                target_day = query.get('date', [day])[0]
                if len(target_day) != 10:
                    raise ValueError('Invalid date')
                bounds(target_day, zone)
                if url.path == '/api/onboarding':
                    from .onboarding import onboarding_state
                    self.send(200, onboarding_state(db_path, target_day, zone))
                elif url.path == '/api/conversations':
                    from .conversations import tree_data
                    self.send(200, tree_data(db_path, target_day, zone))
                elif url.path == '/api/conversation-progress':
                    from .conversations import progress_data
                    self.send(200, progress_data(db_path, query.get('session', [''])[0]))
                elif url.path == '/api/work-record-history':
                    from .ledger import record_history
                    self.send(200, record_history(db_path, query.get('id', [''])[0]))
                elif url.path == '/api/work-records':
                    from .ledger import work_record_data
                    self.send(200, work_record_data(db_path, target_day, zone, query.get('period', ['day'])[0],
                        query.get('category', [None])[0], query.get('state', ['all'])[0]))
                elif url.path == '/api/week':
                    self.send(200, week_data(db_path, target_day, zone))
                elif url.path == '/api/report-view':
                    self.send(200, report_document(db_path, target_day, zone, query.get('period', ['day'])[0], query.get('detail', ['brief'])[0]))
                elif url.path == '/api/tasks':
                    self.send(200, task_service.list(target_day) if task_service else {'enabled': False, 'tasks': []})
                elif url.path == '/api/task-preview':
                    if not task_service:
                        self.send(409, {'error': 'summary_not_enabled'})
                    else:
                        self.send(200, task_service.preview(query.get('id', [''])[0], query.get('job', [None])[0]))
                else:
                    data = report_data(db_path, target_day, zone)
                    if url.path == '/api/day':
                        self.send(200, data)
                    else:
                        period = query.get('period', ['day'])[0]
                        if period not in ('day', 'week', 'month'):
                            raise ValueError('Invalid period')
                        self.send(200, markdown(data, query.get('detail', ['brief'])[0]) if period == 'day' else
                                  period_markdown(db_path, target_day, zone, period, query.get('detail', ['brief'])[0]),
                                  'text/plain; charset=utf-8')
            except (ValueError, OverflowError):
                self.send(400, {'error': 'invalid_date_or_scope'})
            except (OSError, sqlite3.Error):
                self.send(503, {'error': 'local_data_unavailable'})

        def do_POST(self):
            if not self.access(mutation=True):
                return
            path = urlsplit(self.path).path
            if path not in ['/api/tasks', '/api/summarize', '/api/task-review', '/api/task-archive', '/api/task-delete',
                            '/api/summary-settings', '/api/session-names', '/api/session-rename', '/api/session-triage', '/api/session-ignore', '/api/automation', '/api/work-record', '/api/automation-retry']:
                self.send(404, {'error': 'not_found'})
                return
            if not task_service:
                self.send(409, {'error': 'summary_not_enabled'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_JSON_BODY or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.send(413, {'error': 'bounded_json_required'})
                    return
                # Bound client stalls; a source excerpt is never accepted from the browser.
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError('Invalid object')
                if path == '/api/automation-retry':
                    if body != {'confirm': True}:
                        raise ValueError('Explicit retry required')
                    self.send(200, task_service.retry_automation())
                elif path == '/api/automation':
                    if set(body) - {'enabled', 'scope', 'consent', 'idle_minutes'} or 'enabled' not in body:
                        raise ValueError('Invalid automation settings')
                    self.send(200, task_service.configure_automation(body['enabled'], body.get('scope', 'now'),
                        body.get('consent', False), body.get('idle_minutes', 3)))
                elif path == '/api/work-record':
                    if set(body) - {'id', 'category', 'archived'} or 'id' not in body or len(body) < 2:
                        raise ValueError('Invalid work-record correction')
                    if ('category' in body and not isinstance(body['category'], str)) or ('archived' in body and type(body['archived']) is not bool):
                        raise ValueError('Invalid correction values')
                    self.send(200, task_service.revise_work_record(body['id'], body.get('category'), body.get('archived')))
                elif path == '/api/summary-settings':
                    if set(body) - {'allow_excerpts', 'backend'} or 'allow_excerpts' not in body:
                        raise ValueError('Invalid consent')
                    self.send(200, task_service.configure_summary(body['allow_excerpts'], body.get('backend')))
                elif path == '/api/session-names':
                    if set(body) != {'date', 'consent'} or body['consent'] is not True:
                        raise ValueError('Explicit consent required')
                    self.send(200, task_service.name_sessions(body['date']))
                elif path == '/api/session-triage':
                    if set(body) != {'session', 'date', 'deferred'}:
                        raise ValueError('Invalid inbox fields')
                    self.send(200, task_service.defer_session(body['session'], body['date'], body['deferred']))
                elif path == '/api/session-rename':
                    if set(body) != {'session', 'name'}:
                        raise ValueError('Invalid name fields')
                    self.send(200, task_service.rename_session(body['session'], body['name']))
                elif path == '/api/tasks':
                    if set(body) - {'title', 'sessions', 'date', 'start_time'}:
                        raise ValueError('Unknown fields')
                    result = task_service.create(body.get('title'), body.get('sessions'),
                                                 body.get('date', day), body.get('start_time', '00:00'))
                    self.send(201, result)
                elif path == '/api/task-review':
                    if set(body) - {'archive'} != {'task_id', 'status', 'outcome', 'learnings', 'next_steps', 'verification', 'commit_ids'}:
                        raise ValueError('Invalid review fields')
                    if 'archive' in body and type(body['archive']) is not bool:
                        raise ValueError('Invalid archive state')
                    self.send(200, task_service.review(body['task_id'], body['status'], body['outcome'],
                        body['learnings'], body['next_steps'], body['verification'], body['commit_ids'],
                        body.get('archive')))
                elif path == '/api/task-archive':
                    if set(body) != {'task_ids', 'archived'} or type(body['archived']) is not bool:
                        raise ValueError('Invalid archive request')
                    self.send(200, task_service.archive(body['task_ids'], body['archived']))
                elif path == '/api/task-delete':
                    if set(body) != {'task_ids'}:
                        raise ValueError('Invalid delete request')
                    self.send(200, task_service.delete(body['task_ids']))
                elif path == '/api/session-ignore':
                    if set(body) != {'session', 'ignored'} or type(body['ignored']) is not bool:
                        raise ValueError('Invalid ignore request')
                    self.send(200, task_service.ignore_session(body['session'], body['ignored']))
                else:
                    if set(body) - {'task_id', 'mode', 'retry'} or type(body.get('retry', False)) is not bool:
                        raise ValueError('Unknown fields')
                    self.send(202, task_service.request(body.get('task_id'), body.get('mode', 'stage'), body.get('retry', False)))
            except EvidenceUnavailable as error:
                self.send(409, {'error': error.code, 'hint': str(error), 'coverage': error.coverage})
            except (ValueError, TypeError, OverflowError, RecursionError):
                hints = {
                    '/api/automation': '自动总结需明确授权脱敏正文和有效来源，范围仅支持今天或从现在开始；请检查模型与空闲阈值。',
                    '/api/work-record': '只能更正已存在记录的分类或归档状态，分类需在预设范围内。',
                    '/api/summary-settings': '无法更新模型设置。请确认没有排队/运行中的总结，并检查 CodeBuddy CLI 登录或 Pi CLI 默认模型；摘录需要已配置来源。',
                    '/api/session-rename': '名称需为 1–80 个字符，且会话必须已导入。',
                    '/api/task-review': '结果、验证、下一步和经验每项最多 2000 字；已确认状态需填写结果，关联提交须属于任务当日。',
                    '/api/task-archive': '一次可归档 1–50 项已存在的任务；归档状态必须是明确的布尔值。',
                    '/api/task-delete': '一次可删除 1–50 项已存在的任务；删除会一并清除草稿、自动记录并忽略其来源对话。',
                    '/api/session-ignore': '只能忽略已导入的会话；忽略状态必须是明确的布尔值。',
                    '/api/session-triage': '只能暂存所选日期内尚未关联任务的会话；可随时恢复，有新活动会重新提醒。',
                    '/api/session-names': '请确认本地主题提取授权、日期和来源目录。',
                }
                self.send(400, {'error': 'invalid_scope_or_limit', 'hint': hints.get(path,
                    '检查所选会话、起始时间、任务状态或每日任务上限。')})
            except (OSError, sqlite3.Error):
                self.send(503, {'error': 'local_data_unavailable'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.timeout = 1
    server.access_token = token
    return server
