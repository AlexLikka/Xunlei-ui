import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from machine_settings import atomic_json
from migrate_users import import_legacy
from monitor import handler_for
from paths import initialize
from thunder_api import remote
from web_auth import Auth, protected_handler
from workspaces import Workspaces
from test_auth import PASSWORD, Request


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = Auth(self.root / '.runtime')
        self.owner = self.auth.register(dict(username='owner', password=PASSWORD))
        self.guest = self.auth.register(dict(username='guest', password=PASSWORD))
        path = self.root / 'users' / self.owner / 'config.json'
        config = json.loads(path.read_text())
        config['hosts'] = [dict(id='pc-' + str(i), address='10.0.0.' + str(i), credentials_file='credentials.txt') for i in range(101, 105)]
        atomic_json(path, config)
        (path.parent / 'credentials.txt').write_text('owner-ssh\nowner-secret\n')
        self.spaces = Workspaces(self.root, self.auth)
        self.addCleanup(self.spaces.close)
        self.handler = protected_handler(handler_for(None, 'control'), self.auth, 'control', self.spaces)
        self.cookies = {}
        for username in ('owner', 'guest'):
            self.cookies[username] = 'thunder_session=' + self.auth.login(dict(username=username, password=PASSWORD))

    def request(self, username, method, path, body=None, token=None):
        cookie = self.cookies[username]
        if token is None:
            token = self.auth.session(cookie)[1]['token']
        req = Request(method, path, body or {}, {'Cookie': cookie, 'X-Monitor-Token': token})
        self.handler(req, ('10.0.0.8', 1234), SimpleNamespace(server_port=8765))
        head, raw = req.output.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split()[1]), json.loads(raw)

    def test_owner_has_four_hosts_new_user_has_empty_private_space(self):
        monitor = self.spaces.for_user(self.owner)
        monitor.record('pc-103', 99, 'manual_started')
        monitor.archives['pc-103'] = {'recycle': dict(state='ready', checked_at=1,
                                                    tasks=[dict(id=99, name='private archived task', read_only=True)])}
        _, owner = self.request('owner', 'GET', '/api/status')
        _, guest = self.request('guest', 'GET', '/api/status')
        self.assertEqual(len(owner['hosts']), 4)
        self.assertEqual(len(owner['events']), 1)
        archived = next(h for h in owner['hosts'] if h['id'] == 'pc-103')['archives']['recycle']['tasks']
        self.assertEqual(archived[0]['name'], 'private archived task')
        self.assertEqual(guest['hosts'], [])
        self.assertEqual(guest['events'], [])
        self.assertEqual(guest['fleet']['history'], {})
        self.assertEqual(guest['fleet']['total'], 0)
        self.assertNotIn('owner-secret', json.dumps(owner))
        self.assertNotIn('pc-103', json.dumps(guest))
        self.assertNotIn('private archived task', json.dumps(guest))

    def test_owner_can_edit_and_remove_machine_through_authenticated_routes(self):
        monitor=self.spaces.for_user(self.owner)
        host=next(h for h in monitor.config['hosts'] if h['id']=='pc-103')
        code,result=self.request('owner','POST','/api/hosts/update',dict(host='pc-103',name='工作机',address=host['address'],port=22))
        self.assertEqual(code,200);self.assertEqual(result['id'],'pc-103')
        _,status=self.request('owner','GET','/api/status')
        self.assertEqual(next(h for h in status['hosts'] if h['id']=='pc-103')['name'],'工作机')
        code,_=self.request('owner','POST','/api/hosts/delete',dict(host='pc-103',confirmation='工作机'))
        self.assertEqual(code,200)
        _,status=self.request('owner','GET','/api/status')
        self.assertEqual(len(status['hosts']),3)
        _,guest=self.request('guest','GET','/api/status')
        self.assertEqual(guest['hosts'],[])

    def test_user_id_in_request_cannot_select_another_workspace(self):
        for path, body in [('/api/refresh', {'host': 'pc-103'}),
                           ('/api/hosts/update', {'host':'pc-103','name':'renamed','address':'10.0.0.9','port':22}),
                           ('/api/hosts/delete', {'host':'pc-103','confirmation':'pc-103'}),
                           ('/api/hosts/probe', {'host':'pc-103','name':'renamed','address':'10.0.0.9','port':22}),
                           ('/api/settings/credentials', {'host': 'pc-103', 'username': 'x', 'password': 'x'}),
                           ('/api/settings/automation', {'host': 'pc-103', 'enabled': True, 'idle_seconds': 60}),
                           ('/api/actions/preview', {'host': 'pc-103', 'command': 'del_zero'}),
                           ('/api/cloud/preview', {'host': 'pc-103', 'command': 'transfer'}),
                           ('/api/previews/start', {'kind':'manual','host':'pc-103','command':'start_all'}),
                           ('/api/previews/status', {'preview':'another-user-ticket'}),
                           ('/api/cloud/inspect', {'host': 'pc-103', 'action': 'status'})]:
            code, _ = self.request('guest', 'POST', path, dict(body, user_id=self.owner, username='owner'))
            self.assertEqual(code, 400)
        self.assertEqual(self.spaces.for_user(self.guest).config['hosts'], [])

    def test_other_session_csrf_is_rejected(self):
        token = self.auth.session(self.cookies['owner'])[1]['token']
        code, _ = self.request('guest', 'POST', '/api/fleet/accelerate', {}, token=token)
        self.assertEqual(code, 403)

    def test_cloud_previews_and_records_cannot_cross_accounts(self):
        monitor = self.spaces.for_user(self.owner)
        task = dict(id=99, name='private-cloud-source', save_path='D:\\test', size=100, received=1,
                    speed=0, status=5, active=True, task_type=2, url='magnet:?xt=test')
        monitor.cloud.remote = Mock()
        monitor.cloud.remote.status.return_value = {'state': {}, 'workers': {}}
        with patch('cloud_transfer.read_live_tasks', return_value=[task]):
            code, preview = self.request('owner', 'POST', '/api/cloud/preview', dict(host='pc-103', command='transfer'))
        self.assertEqual(code, 200)
        code, _ = self.request('guest', 'POST', '/api/cloud/execute', dict(plan=preview['plan'], user_id=self.owner))
        self.assertEqual(code, 400)
        monitor.cloud.host_states['pc-103'] = dict(state='ready', jobs=[dict(name='private-cloud-result')])
        _, result = self.request('guest', 'GET', '/api/status')
        self.assertNotIn('private-cloud-result', json.dumps(result))
        self.assertEqual(result['cloud'], dict(hosts={}, jobs=[]))

    def test_plans_jobs_and_probe_tokens_are_private(self):
        monitor = self.spaces.for_user(self.owner)
        task = dict(id=1, status=5, speed=0, active=True, received=1, size=100, name='private-file', save_path='D:/private', completed=0)
        with patch('manual_actions.read_live_tasks', return_value=[task]) as read:
            code, preview = self.request('owner', 'POST', '/api/actions/preview', dict(host='pc-103', command='del_zero'))
            self.assertEqual(code, 200)
            self.assertEqual(read.call_args.args[1], monitor.data_dir)
        code, _ = self.request('guest', 'POST', '/api/actions/execute', dict(plan=preview['plan'], confirmation=''))
        self.assertEqual(code, 400)
        with patch('manual_actions.perform', return_value={'outcome': 'skipped'}), patch.object(monitor, '_poll_host'):
            code, result = self.request('owner', 'POST', '/api/actions/execute', dict(plan=preview['plan'], confirmation=''))
            self.assertEqual(code, 200)
            monitor.actions.join()
        code, _ = self.request('guest', 'POST', '/api/actions/status', dict(job=result['job']))
        self.assertEqual(code, 400)
        monitor.settings.pending['owner-probe'] = {'expires': float('inf')}
        code, _ = self.request('guest', 'POST', '/api/hosts/add', dict(probe='owner-probe', trust=True, credentials_from='pc-103'))
        self.assertEqual(code, 400)

    def test_same_host_ids_use_independent_credentials_settings_and_journals(self):
        owner = self.spaces.for_user(self.owner)
        guest = self.spaces.for_user(self.guest)
        host = dict(owner.config['hosts'][0])
        guest.config['hosts'] = [host]
        guest.operation_locks[host['id']] = threading.RLock()
        from live_tracker import LiveTracker
        guest.trackers[host['id']] = LiveTracker(600, 30)
        guest.hosts[host['id']] = dict(id=host['id'], address=host['address'], state='等待连接', tasks=[])
        guest.settings.credentials(dict(host=host['id'], username='guest-ssh', password='guest-secret'))
        owner.settings.automation(dict(host=host['id'], enabled=True, idle_seconds=120))
        self.assertFalse(guest.snapshot()['hosts'][0]['auto_enabled'])
        owner.record(host['id'], 123, 'manual_started')
        self.assertNotIn((host['id'], 123), guest.uncertain)
        self.assertEqual(guest.events, [])
        with patch('thunder_api.paramiko.SSHClient') as factory:
            client = factory.return_value
            client.exec_command.side_effect = RuntimeError('stop before remote execution')
            for monitor, ssh_user in ((owner, 'owner-ssh'), (guest, 'guest-ssh')):
                selected = monitor.config['hosts'][0]
                with self.assertRaises(RuntimeError):
                    remote(selected, '', monitor.data_dir)
                self.assertEqual(client.connect.call_args.kwargs['username'], ssh_user)
                client.load_host_keys.assert_called_with(str(monitor.data_dir / '.runtime/known_hosts'))
        with self.assertRaises(ValueError):
            remote(dict(host, credentials_file='../../outside.txt'), '', guest.data_dir)

    def test_web_users_cannot_stop_the_platform(self):
        for username in ('owner', 'guest'):
            code, _ = self.request(username, 'POST', '/api/shutdown')
            self.assertEqual(code, 403)
        self.assertFalse(self.spaces.stop.is_set())
        self.assertFalse(self.spaces.for_user(self.owner).stop.is_set())

    def test_renamed_account_keeps_workspace_and_survives_restart(self):
        self.auth.change(self.owner, dict(old_password=PASSWORD, password=PASSWORD, username='renamed'))
        restarted_auth = Auth(self.root / '.runtime')
        spaces = Workspaces(self.root, restarted_auth)
        self.addCleanup(spaces.close)
        owner = restarted_auth.find('renamed')['id']
        self.assertEqual(owner, self.owner)
        self.assertEqual(len(spaces.for_user(owner).config['hosts']), 4)
        self.assertEqual(spaces.for_user(self.guest).config['hosts'], [])

    def test_shared_scheduler_polls_background_without_login(self):
        event = threading.Event()
        monitor = self.spaces.for_user(self.owner)
        monitor.poll_host = Mock(side_effect=lambda host: event.set())
        self.spaces.start()
        self.assertTrue(event.wait(3))
        self.spaces.close()
        self.assertGreaterEqual(monitor.poll_host.call_count, 1)


class MigrationTests(unittest.TestCase):
    def test_interrupted_import_resumes_without_publishing_partial_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            config = json.loads((root / 'config.json').read_text())
            config['hosts'] = [dict(id='old', address='10.0.0.1', credentials_file='password.txt')]
            atomic_json(root / 'config.json', config)
            (root / 'password.txt').write_text('legacy-user\nlegacy-password')
            auth = Auth(root / '.runtime')
            with patch('migrate_users.shutil.copyfile', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    import_legacy(auth, 'owner', PASSWORD)
            owner = auth.find('owner')['id']
            target = root / 'users' / owner
            self.assertEqual(json.loads((target / 'config.json').read_text())['hosts'], [])
            self.assertEqual(import_legacy(auth, 'owner', PASSWORD), owner)
            self.assertEqual(len(json.loads((target / 'config.json').read_text())['hosts']), 1)
            self.assertEqual(json.loads((root / '.runtime/legacy-owner.json').read_text())['state'], 'ready')

    def test_legacy_migration_is_explicit_idempotent_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            config = json.loads((root / 'config.json').read_text())
            config['hosts'] = [dict(id='pc-' + str(i), address='10.0.0.' + str(i), credentials_file='credentials.txt') for i in range(101, 105)]
            atomic_json(root / 'config.json', config)
            (root / 'credentials.txt').write_text('legacy-ssh\nlegacy-secret')
            event = json.dumps(dict(host='pc-103', task_id=99, event='manual_started'))
            (root / '.runtime/events.jsonl').write_text(event + '\n')
            (root / '.runtime/setup-token').write_text('obsolete')
            auth = Auth(root / '.runtime')
            unrelated = auth.register(dict(username='early-signup', password=PASSWORD))
            owner = import_legacy(auth, 'owner', PASSWORD)
            self.assertEqual(import_legacy(auth, 'owner', PASSWORD), owner)
            self.assertFalse((root / '.runtime/setup-token').exists())
            guest = auth.register(dict(username='new-signup', password=PASSWORD))
            spaces = Workspaces(root, auth)
            try:
                self.assertEqual(len(spaces.for_user(owner).config['hosts']), 4)
                self.assertIn(('pc-103', 99), spaces.for_user(owner).uncertain)
                self.assertEqual((spaces.for_user(owner).data_dir / 'credentials.txt').read_text(), 'legacy-ssh\nlegacy-secret')
                for user_id in (unrelated, guest):
                    self.assertEqual(spaces.for_user(user_id).config['hosts'], [])
                    self.assertEqual(spaces.for_user(user_id).events, [])
            finally:
                spaces.close()
            with self.assertRaisesRegex(ValueError, '另一个账号'):
                import_legacy(auth, 'new-signup', PASSWORD)
