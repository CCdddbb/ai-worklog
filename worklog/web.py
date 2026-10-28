"""Loopback dashboard; authenticated task actions never execute submitted code."""
import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import bounds
from .store import markdown, report_data

STATIC = Path(__file__).resolve().parents[1] / 'static'


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
                      '/tasks.js': ('tasks.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if not self.access(private=url.path not in assets):
                return
            if url.path in assets:
                name, mime = assets[url.path]
                self.send(200, (STATIC / name).read_bytes(), mime + '; charset=utf-8')
                return
            if url.path not in ['/api/day', '/api/report', '/api/tasks', '/api/task-preview']:
                self.send(404, {'error': 'not_found'})
                return
            try:
                query = parse_qs(url.query)
                target_day = query.get('date', [day])[0]
                if len(target_day) != 10:
                    raise ValueError('Invalid date')
                bounds(target_day, zone)
                if url.path == '/api/tasks':
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
                        self.send(200, markdown(data), 'text/plain; charset=utf-8')
            except (ValueError, OverflowError):
                self.send(400, {'error': 'invalid_date_or_scope'})
            except (OSError, sqlite3.Error):
                self.send(503, {'error': 'local_data_unavailable'})

        def do_POST(self):
            if not self.access(mutation=True):
                return
            path = urlsplit(self.path).path
            if path not in ['/api/tasks', '/api/summarize']:
                self.send(404, {'error': 'not_found'})
                return
            if not task_service:
                self.send(409, {'error': 'summary_not_enabled'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.send(413, {'error': 'bounded_json_required'})
                    return
                # Bound client stalls; a source excerpt is never accepted from the browser.
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError('Invalid object')
                if path == '/api/tasks':
                    if set(body) - {'title', 'sessions', 'date', 'start_time'}:
                        raise ValueError('Unknown fields')
                    result = task_service.create(body.get('title'), body.get('sessions'),
                                                 body.get('date', day), body.get('start_time', '00:00'))
                    self.send(201, result)
                else:
                    if set(body) - {'task_id', 'mode', 'retry'} or type(body.get('retry', False)) is not bool:
                        raise ValueError('Unknown fields')
                    self.send(202, task_service.request(body.get('task_id'), body.get('mode', 'stage'), body.get('retry', False)))
            except (ValueError, TypeError, OverflowError, RecursionError):
                self.send(400, {'error': 'invalid_scope_or_limit', 'hint': '检查所选会话、起始时间、任务状态或每日任务上限。'})
            except (OSError, sqlite3.Error):
                self.send(503, {'error': 'local_data_unavailable'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.timeout = 1
    server.access_token = token
    return server
