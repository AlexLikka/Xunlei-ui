import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from web_auth import Auth, protected_handler
from monitor import handler_for

PASSWORD = 'secure-password-123'


class Request:
    def __init__(self, method, path, body, headers):
        data = json.dumps(body).encode()
        self.input = io.BytesIO((f'{method} {path} HTTP/1.1\r\nHost: localhost:8765\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers.items()) + '\r\n').encode() + data)
        self.output = io.BytesIO()
    def makefile(self, *args, **kwargs):
        return self.input
    def sendall(self, data):
        self.output.write(data)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = Auth(self.root / '.runtime')
        self.handler = protected_handler(handler_for(None, 'control'), self.auth, 'control')

    def request(self, method, path, body=None, client='10.0.0.8', **headers):
        request = Request(method, path, body or {}, headers)
        self.handler(request, (client, 1234), SimpleNamespace(server_port=8765))
        head, body = request.output.getvalue().split(b'\r\n\r\n', 1)
        return head.decode(), body

    def register(self, username='admin'):
        return self.auth.register(dict(username=username, password=PASSWORD))

    def test_unauthenticated_and_control_secret_not_public(self):
        for method, path in [('GET', '/api/status'), ('GET', '/api/session'), ('POST', '/api/fleet/accelerate'), ('POST', '/api/shutdown')]:
            head, _ = self.request(method, path, **{'X-Monitor-Token': 'control'})
            self.assertIn('401', head)

    def test_public_registration_needs_no_setup_and_creates_empty_space(self):
        for username in ('first', 'second'):
            head, _ = self.request('POST', '/api/auth/register', dict(username=username, password=PASSWORD))
            self.assertIn('200', head)
            account = self.auth.find(username)
            config = json.loads((self.root / 'users' / account['id'] / 'config.json').read_text())
            self.assertEqual(config['hosts'], [])
            self.assertEqual(config['mode'], 'observe')
        self.assertFalse((self.root / '.runtime/setup-token').exists())
        _, raw = self.request('GET', '/api/auth/status')
        self.assertTrue(json.loads(raw)['registration_open'])

    def test_registration_login_csrf_logout(self):
        self.register()
        head, _ = self.request('POST', '/api/auth/login', dict(username='admin', password=PASSWORD))
        self.assertIn('200', head)
        self.assertIn('HttpOnly', head)
        cookie = next(x.split(': ', 1)[1].split(';')[0] for x in head.split('\r\n') if x.startswith('Set-Cookie:'))
        _, body = self.request('GET', '/api/session', Cookie=cookie)
        token = json.loads(body)['token']
        self.assertNotEqual(token, 'control')
        head, _ = self.request('POST', '/api/auth/logout', Cookie=cookie)
        self.assertIn('403', head)
        head, _ = self.request('POST', '/api/auth/logout', Cookie=cookie, **{'X-Monitor-Token': token})
        self.assertIn('200', head)
        head, _ = self.request('GET', '/api/session', Cookie=cookie)
        self.assertIn('401', head)

    def test_password_change_preserves_identity_and_other_users_sessions(self):
        owner = self.register()
        other = self.register('other')
        sid = self.auth.login(dict(username='admin', password=PASSWORD))
        other_sid = self.auth.login(dict(username='other', password=PASSWORD))
        self.auth.change(owner, dict(old_password=PASSWORD, password='another-password-123', username='owner'))
        self.assertIsNone(self.auth.session('thunder_session=' + sid)[1])
        self.assertEqual(self.auth.session('thunder_session=' + other_sid)[1]['user_id'], other)
        restarted = Auth(self.root / '.runtime', secure=True)
        restarted.login(dict(username='owner', password='another-password-123'))
        self.assertEqual(restarted.find('owner')['id'], owner)
        self.assertIn('Secure', restarted.cookie('x'))
        self.assertNotIn(b'another-password-123', restarted.path.read_bytes())

    def test_duplicate_normalized_username_and_collision_on_change(self):
        first = self.register()
        for name in ('ADMIN', ' admin ', 'ａｄｍｉｎ'):
            with self.assertRaisesRegex(ValueError, '已被使用'):
                self.register(name)
        self.register('second')
        with self.assertRaisesRegex(ValueError, '已被使用'):
            self.auth.change(first, dict(old_password=PASSWORD, password=PASSWORD, username='second'))
        self.assertEqual(self.auth.find('admin')['id'], first)

    def test_cross_origin_login_and_registration_rejected(self):
        for action in ('login', 'register'):
            head, _ = self.request('POST', '/api/auth/' + action, dict(username='someone', password=PASSWORD), Origin='https://evil.example')
            self.assertIn('403', head)
        self.assertEqual(self.auth.user_ids(), [])

    def test_rate_limits_are_scoped_to_client(self):
        self.register()
        for _ in range(10):
            with self.assertRaises(ValueError):
                self.auth.login({}, client='bad-client')
        with self.assertRaisesRegex(ValueError, '过多'):
            self.auth.login({}, client='bad-client')
        self.auth.login(dict(username='admin', password=PASSWORD), client='other-client')

    def test_protected_action_requires_session_csrf(self):
        from unittest.mock import Mock
        self.register()
        monitor = SimpleNamespace(fleet=SimpleNamespace(start=Mock(return_value={'job': 'test'})))
        self.handler = protected_handler(handler_for(monitor, 'control'), self.auth, 'control')
        sid = self.auth.login(dict(username='admin', password=PASSWORD))
        cookie = 'thunder_session=' + sid
        head, _ = self.request('POST', '/api/fleet/accelerate', Cookie=cookie)
        self.assertIn('403', head)
        monitor.fleet.start.assert_not_called()
        token = self.auth.session(cookie)[1]['token']
        head, _ = self.request('POST', '/api/fleet/accelerate', Cookie=cookie, **{'X-Monitor-Token': token})
        self.assertIn('200', head)
        monitor.fleet.start.assert_called_once()

    def test_concurrent_duplicate_registration_creates_one_account(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt(index):
            try:
                return self.auth.register(dict(username='same', password=PASSWORD), client=str(index))
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.auth.user_ids()), 1)

    def test_registration_validation(self):
        for username, password in [(None, PASSWORD), ('', PASSWORD), ('line\nbreak', PASSWORD), ('x' * 65, PASSWORD), ('valid', 'short'), ('valid', None)]:
            with self.assertRaises(ValueError):
                self.auth.register(dict(username=username, password=password))
        self.assertEqual(self.auth.user_ids(), [])
