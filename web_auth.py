"""Public accounts with stable identities and sessions that expire on restart."""
import hashlib
from contextlib import contextmanager
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import unicodedata

from paths import initialize


class Auth:
    def __init__(self, directory, secure=False):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.directory.parent
        self.path = self.directory / 'users.sqlite3'
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        self.hash_slots = threading.BoundedSemaphore(4)
        self.secure = secure
        with self.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL,
                username_key TEXT NOT NULL UNIQUE, salt TEXT NOT NULL,
                password TEXT NOT NULL, created_at REAL NOT NULL)''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    @staticmethod
    def username(value):
        if not isinstance(value, str):
            raise ValueError('用户名须为 1–64 字符')
        value = unicodedata.normalize('NFKC', value).strip()
        if not 1 <= len(value) <= 64 or any(unicodedata.category(c).startswith('C') for c in value):
            raise ValueError('用户名须为 1–64 字符，不能包含控制字符')
        return value, value.casefold()

    @staticmethod
    def password_ok(password):
        return isinstance(password, str) and 12 <= len(password) <= 256

    def rate_limit(self, operation, client):
        now = time.monotonic()
        window, limit = (3600, 10) if operation == 'register' else (300, 10)
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if v and now - v[-1] < 3600}
            key = (operation, client)
            attempts = [t for t in self.attempts.get(key, []) if now - t < window]
            if len(attempts) >= limit or (key not in self.attempts and len(self.attempts) >= 4096):
                raise ValueError('尝试过多，请稍后重试')
            self.attempts[key] = attempts + [now]

    def user(self, user_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return dict(row) if row else None

    def find(self, username):
        _, key = self.username(username)
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE username_key = ?', (key,)).fetchone()
        return dict(row) if row else None

    def user_ids(self):
        with self.connection() as db:
            return [row['id'] for row in db.execute('SELECT id FROM users')]

    def register(self, body, client='local'):
        self.rate_limit('register', client)
        username, key = self.username(body.get('username'))
        password = body.get('password')
        if not self.password_ok(password):
            raise ValueError('密码须为 12–256 字符')
        salt = secrets.token_hex(16)
        with self.hash_slots:
            digest = self.digest(password, salt)
        user_id = secrets.token_hex(16)
        with self.lock, self.connection() as db:
            try:
                db.execute('INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)',
                           (user_id, username, key, salt, digest, time.time()))
            except sqlite3.IntegrityError:
                raise ValueError('用户名已被使用') from None
            # A new account always starts with an empty, independent workspace.
            initialize(self.root / 'users' / user_id)
        return user_id

    def login(self, body, client='local'):
        self.rate_limit('login', client)
        try:
            account = self.find(body.get('username'))
        except ValueError:
            account = None
        password = body.get('password', '')
        if not isinstance(password, str) or len(password) > 256:
            password = ''
        with self.hash_slots:
            digest = self.digest(password, account['salt'] if account else '00' * 16)
        if not account or not hmac.compare_digest(digest, account['password']):
            raise ValueError('账号或密码错误')
        with self.lock:
            # Don't publish a session if the password changed while hashing.
            current = self.user(account['id'])
            if not current or current['password'] != account['password']:
                raise ValueError('账号或密码已变更，请重新登录')
            now = time.monotonic()
            self.attempts.pop(('login', client), None)
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            own = [k for k, v in self.sessions.items() if v['user_id'] == account['id']]
            for key in own[:-31]:
                self.sessions.pop(key, None)
            sid = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(sid.encode()).hexdigest()] = dict(
                user_id=account['id'], token=secrets.token_urlsafe(32), expires=now + 43200)
            return sid

    def session(self, cookie):
        try:
            parsed = SimpleCookie()
            parsed.load(cookie or '')
            key = hashlib.sha256(parsed['thunder_session'].value.encode()).hexdigest()
        except (KeyError, ValueError, CookieError):
            return None, None
        with self.lock:
            session = self.sessions.get(key)
            return (key, session) if session and session['expires'] > time.monotonic() else (None, None)

    def change(self, user_id, body):
        account = self.user(user_id)
        old, new = body.get('old_password', ''), body.get('password')
        if not isinstance(old, str) or len(old) > 256 or not account:
            raise ValueError('原密码错误')
        with self.hash_slots:
            if not hmac.compare_digest(self.digest(old, account['salt']), account['password']):
                raise ValueError('原密码错误')
            if not self.password_ok(new):
                raise ValueError('新密码须为 12–256 字符')
            username, key = self.username(body.get('username', account['username']))
            salt = secrets.token_hex(16)
            digest = self.digest(new, salt)
        with self.lock, self.connection() as db:
            try:
                result = db.execute('UPDATE users SET username=?, username_key=?, salt=?, password=? '
                                    'WHERE id=? AND password=?',
                                    (username, key, salt, digest, user_id, account['password']))
            except sqlite3.IntegrityError:
                raise ValueError('用户名已被使用') from None
            if result.rowcount != 1:
                raise ValueError('账号已变更，请重新登录')
            self.sessions = {k: v for k, v in self.sessions.items() if v['user_id'] != user_id}

    def cookie(self, sid='', clear=False):
        return ('thunder_session=' + sid + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' +
                ('0' if clear else '43200') + ('; Secure' if self.secure else ''))


def protected_handler(base, auth, control_token, workspaces=None):
    """Select the workspace exclusively from the authenticated session."""
    class Protected(base):
        @property
        def monitor(self):
            if workspaces is not None:
                return workspaces.for_user(self.auth_session['user_id'])
            return super().monitor

        def end_headers(self):
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if getattr(self, 'new_cookie', None):
                self.send_header('Set-Cookie', self.new_cookie)
            super().end_headers()

        def local_request(self):
            _, session = auth.session(self.headers.get('Cookie'))
            return bool(session and hmac.compare_digest(self.headers.get('X-Monitor-Token', ''), session['token']))

        def do_GET(self):
            _, session = auth.session(self.headers.get('Cookie'))
            self.auth_session = session
            if self.path == '/api/auth/status':
                self.json_response(dict(registration_open=True, authenticated=bool(session)))
                return
            if self.path == '/api/session':
                account = auth.user(session['user_id']) if session else None
                self.json_response(dict(token=session['token'], username=account['username'])
                                   if account else {'error': '请先登录'}, 200 if account else 401)
                return
            if self.path in ('/login', '/login.js'):
                from paths import CODE
                data = (CODE / 'web' / ('login.html' if self.path == '/login' else 'login.js')).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/login' else 'text/javascript; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(data)
                return
            if not session and self.path != '/style.css':
                if self.path == '/':
                    self.send_response(302)
                    self.send_header('Location', '/login')
                    self.end_headers()
                else:
                    self.json_response({'error': '请先登录'}, 401)
                return
            self.local_request = lambda: True
            super().do_GET()

        def do_POST(self):
            token = self.headers.get('X-Monitor-Token', '')
            if (self.path == '/api/shutdown' and self.client_address[0] in ('127.0.0.1', '::1')
                    and control_token and hmac.compare_digest(token, control_token)
                    and not self.headers.get('Origin')):
                if workspaces is not None:
                    workspaces.request_stop()
                    self.json_response({'stopping': True}, 202)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self.local_request = lambda: True
                return super().do_POST()
            if self.path in ('/api/auth/register', '/api/auth/login', '/api/auth/logout', '/api/auth/password'):
                try:
                    from urllib.parse import urlsplit
                    origin = self.headers.get('Origin')
                    if (self.headers.get('Sec-Fetch-Site') == 'cross-site' or
                            (origin and urlsplit(origin).netloc != self.headers.get('Host')) or
                            self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                        self.json_response({'error': '请求来源无效'}, 403)
                        return
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16384:
                        raise ValueError('请求长度无效')
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError('请求格式无效')
                    client = self.client_address[0]
                    if self.path == '/api/auth/register':
                        auth.register(body, client)
                    elif self.path == '/api/auth/login':
                        self.new_cookie = auth.cookie(auth.login(body, client))
                    else:
                        key, session = auth.session(self.headers.get('Cookie'))
                        if not session:
                            self.json_response({'error': '请先登录'}, 401)
                            return
                        if not self.local_request():
                            self.json_response({'error': '会话校验失败'}, 403)
                            return
                        if self.path == '/api/auth/password':
                            auth.change(session['user_id'], body)
                        else:
                            with auth.lock:
                                auth.sessions.pop(key, None)
                        self.new_cookie = auth.cookie(clear=True)
                    self.json_response({'ok': True})
                except (ValueError, TypeError) as exc:
                    self.json_response({'error': str(exc)}, 400)
                return
            _, session = auth.session(self.headers.get('Cookie'))
            if not session:
                self.json_response({'error': '请先登录'}, 401)
                return
            if self.path == '/api/shutdown':
                self.json_response({'error': '平台服务由服务器管理员管理，请使用退出登录'}, 403)
                return
            if not self.local_request():
                self.json_response({'error': '会话校验失败'}, 403)
                return
            self.auth_session = session
            if 'X-Monitor-Token' in self.headers:
                del self.headers['X-Monitor-Token']
            self.headers['X-Monitor-Token'] = control_token
            self.local_request = lambda: True
            super().do_POST()
    return Protected
