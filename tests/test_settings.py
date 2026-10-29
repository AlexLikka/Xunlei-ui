import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import paramiko
from monitor import Monitor, load_config

class SettingsTests(unittest.TestCase):
    def setup_monitor(self,d):
        root=Path(d);(root/'.runtime').mkdir();(root/'.runtime/known_hosts').touch()
        conf=dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='one',address='10.0.0.1',credentials_file='password.txt'),dict(id='two',address='10.0.0.2',credentials_file='password.txt')],mode='observe')
        return Monitor(conf,root/'config.json')

    def test_independent_auto_persistence_and_reset(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);m.trackers['one'].previous[1]={'x':1}
            m.settings.automation(dict(host='one',enabled=True,idle_seconds=120))
            snap=m.snapshot()['hosts'];self.assertTrue(snap[0]['auto_enabled']);self.assertFalse(snap[1]['auto_enabled'])
            self.assertFalse(m.trackers['one'].previous)
            persisted=load_config(Path(d)/'config.json');self.assertTrue(persisted['hosts'][0]['auto_enabled'])
            m.settings.automation(dict(host='one',enabled=False,idle_seconds=60))
            self.assertFalse(m.snapshot()['hosts'][0]['auto_enabled'])
            with self.assertRaises(ValueError):m.settings.automation(dict(host='one',enabled=True,idle_seconds=1))

    def test_add_defaults_observe_secrets_and_dynamic_state(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);key=paramiko.RSAKey.generate(1024)
            m.settings.pending['token']=dict(id='three',address='10.0.0.3',port=22,alias='10.0.0.3',key=key,expires=time.monotonic()+120)
            with patch.object(m,'poll_host',return_value={'state':'异常','error':'不兼容'}):
                result=m.settings.add(dict(probe='token',trust=True,username='user',password='private-value'))
            self.assertTrue(result['saved']);self.assertEqual(result['state'],'异常')
            self.assertFalse(m.snapshot()['hosts'][-1]['auto_enabled'])
            self.assertNotIn('private-value',json.dumps(m.snapshot()))
            conf=json.loads((Path(d)/'config.json').read_text());p=Path(d)/conf['hosts'][-1]['credentials_file']
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertIn('three',m.trackers);self.assertIn('three',m.operation_locks)
            with self.assertRaises(ValueError):m.settings.add(dict(probe='token',trust=True))

    def test_reuse_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);key=paramiko.RSAKey.generate(1024)
            m.settings.pending['t']=dict(id='three',address='10.0.0.3',port=2222,alias='[10.0.0.3]:2222',key=key,expires=time.monotonic()+120)
            with patch.object(m,'poll_host',return_value={'state':'在线'}):m.settings.add(dict(probe='t',trust=True,credentials_from='one'))
            self.assertEqual(m.config['hosts'][-1]['credentials_file'],'password.txt')
            self.assertNotIn('credentials_file',m.snapshot()['hosts'][-1])

    def test_probe_changed_key_and_confirmation(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);key=paramiko.RSAKey.generate(1024)
            transport=MagicMock();transport.get_remote_server_key.return_value=key
            with patch('machine_settings.socket.create_connection'),patch('machine_settings.paramiko.Transport',return_value=transport):
                p=m.settings.probe(dict(id='new',address='10.0.0.4',port=22))
                self.assertTrue(p['fingerprint'].startswith('SHA256:'))
                self.assertFalse(p['known'])
                with self.assertRaises(ValueError):m.settings.add(dict(probe=p['probe'],trust=False))
                m.settings.pending[p['probe']]['expires']=0
                with self.assertRaises(ValueError):m.settings.add(dict(probe=p['probe'],trust=True))
                other=paramiko.RSAKey.generate(1024);keys=paramiko.HostKeys();keys.add('10.0.0.4',other.get_name(),other);keys.save(str(Path(d)/'.runtime/known_hosts'))
                with self.assertRaises(ValueError):m.settings.probe(dict(id='new',address='10.0.0.4',port=22))

    def test_probe_duplicate_and_input_validation(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d)
            for b in [dict(id='one',address='10.0.0.8',port=22),dict(id='x',address='10.0.0.1',port=22),dict(id='../../x',address='10.0.0.8',port=22),dict(id='x',address='invalid',port=22),dict(id='x',address='10.0.0.8',port=0)]:
                with self.assertRaises(ValueError):m.settings.probe(b)

    def test_credential_update_does_not_change_reused_source(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d)
            m.settings.credentials(dict(host='one',username='new-user',password='new-secret'))
            first,second=m.config['hosts']
            self.assertEqual(second['credentials_file'],'password.txt')
            self.assertNotEqual(first['credentials_file'],second['credentials_file'])
            p=Path(d)/first['credentials_file']
            self.assertEqual(p.read_text(),'new-user\nnew-secret\n')
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertNotIn('new-secret',json.dumps(m.snapshot()))
            self.assertEqual(load_config(Path(d)/'config.json')['hosts'][0]['credentials_file'],first['credentials_file'])

    def test_rename_preserves_identity_records_and_credentials(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d)
            old=m.config['hosts'][0]
            m.events=[dict(host='one',task_id=1,event='manual_resumed')]
            m.fleet.history['one']=[dict(time=1,speed=10)]
            m.archives['one']={'recycle':dict(tasks=[],state='ready')}
            m.settings.update(dict(host='one',name='客厅电脑',address='10.0.0.1',port=22))
            h=m.config['hosts'][0]
            self.assertEqual(h['id'],'one');self.assertEqual(h['name'],'客厅电脑')
            self.assertEqual(h['credentials_file'],old['credentials_file'])
            self.assertEqual(m.snapshot()['hosts'][0]['name'],'客厅电脑')
            self.assertEqual(m.snapshot()['fleet']['history']['one'],[dict(time=1,speed=10)])
            self.assertIn('one',m.archives)
            self.assertEqual(load_config(Path(d)/'config.json')['hosts'][0]['name'],'客厅电脑')
            self.assertEqual(m.events[0]['host'],'one')

    def test_address_change_requires_bound_trusted_probe_and_resets_samples(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);old=m.config['hosts'][0]
            body=dict(host='one',name='新名字',address='10.0.0.8',port=2222)
            with self.assertRaises(ValueError):m.settings.update(body)
            key=paramiko.RSAKey.generate(1024);transport=MagicMock();transport.get_remote_server_key.return_value=key
            with patch('machine_settings.socket.create_connection'),patch('machine_settings.paramiko.Transport',return_value=transport):
                p=m.settings.probe(body)
            with self.assertRaises(ValueError):m.settings.update(dict(body,probe=p['probe'],trust=False))
            with self.assertRaises(ValueError):m.settings.update(dict(body,host='two',probe=p['probe'],trust=True))
            with self.assertRaises(ValueError):m.settings.add(dict(probe=p['probe'],trust=True,credentials_from='one'))
            m.archives['one']={'recycle':dict(tasks=[{'id':1}],state='ready')}
            m.trackers['one'].previous[1]={'old':'sample'}
            m.settings.update(dict(body,probe=p['probe'],trust=True))
            self.assertEqual(m.snapshot()['hosts'][0]['state'],'等待连接')
            self.assertEqual(m.snapshot()['hosts'][0]['port'],2222)
            self.assertFalse(m.trackers['one'].previous);self.assertNotIn('one',m.archives)
            keys=paramiko.HostKeys();keys.load(str(Path(d)/'.runtime/known_hosts'))
            self.assertTrue(keys.check('[10.0.0.8]:2222',key))
            with patch('monitor.read_live_tasks') as remote:
                m.poll_host(old)
            remote.assert_not_called()

    def test_delete_requires_name_keeps_shared_credentials_and_durable_history(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);path=Path(d)/'password.txt';path.write_text('user\nsecret\n')
            old=m.config['hosts'][0]
            m.fleet.history['one']=[dict(time=1,speed=10)]
            m.events=[dict(host='one',task_id=1,event='manual_resumed')]
            with self.assertRaises(ValueError):m.settings.delete(dict(host='one',confirmation='wrong'))
            m.settings.delete(dict(host='one',confirmation='one'))
            self.assertEqual([h['id'] for h in m.snapshot()['hosts']],['two'])
            self.assertNotIn('one',m.trackers);self.assertNotIn('one',m.snapshot()['fleet']['history'])
            self.assertTrue(path.exists());self.assertEqual(m.events[0]['host'],'one')
            self.assertEqual(len(load_config(Path(d)/'config.json')['hosts']),1)
            with patch('monitor.read_live_tasks') as remote:m.poll_host(old)
            remote.assert_not_called()

    def test_busy_preview_and_operation_lock_reject_edits_quickly(self):
        import threading
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);body=dict(host='one',name='new',address='10.0.0.1',port=22)
            for group in (m.actions.busy,m.cloud.busy):
                group.add('one')
                with self.assertRaises(ValueError):m.settings.update(body)
                group.clear()
            m.previews.requests['r']=dict(host='one',state='running')
            with self.assertRaises(ValueError):m.settings.update(body)
            m.previews.requests.clear()
            ready=threading.Event();release=threading.Event()
            def hold():
                with m.operation_locks['one']:ready.set();release.wait(3)
            t=threading.Thread(target=hold);t.start();ready.wait(3)
            try:
                start=time.monotonic()
                with self.assertRaises(ValueError):m.settings.update(body)
                self.assertLess(time.monotonic()-start,.5)
            finally:release.set();t.join()

    def test_old_plans_invalidated_and_duplicate_edits_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);body=dict(host='one',name='new',address='10.0.0.1',port=22)
            with patch('manual_actions.read_live_tasks',return_value=[]):p=m.actions.prepare('one','start_all')
            old_plan=m.actions.plans[p['plan']]
            m.cloud.plans['c']=dict(host='one',expires=time.monotonic()+900)
            m.previews.requests['r']=dict(host='one',state='done',result={'plan':p['plan']})
            for change in (dict(name='two'),dict(address='10.0.0.2'),dict(name=' '),dict(port=0)):
                with self.assertRaises(ValueError):m.settings.update(dict(body,**change))
            m.settings.update(body)
            self.assertFalse(m.actions.plans);self.assertFalse(m.cloud.plans)
            self.assertEqual(m.previews.status('r')['state'],'error')
            # Even a direct preview finishing after the edit cannot execute.
            m.actions.plans[p['plan']]=old_plan
            with self.assertRaisesRegex(ValueError,'配置已变化'):m.actions.execute(p['plan'],'')
            m.cloud.plans['late']=dict(host='one',host_revision=None,expires=time.monotonic()+900)
            with self.assertRaisesRegex(ValueError,'配置已变化'):m.cloud.execute('late')

    def test_stale_archive_and_cloud_read_cannot_republish_removed_machine(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=self.setup_monitor(d);old=m.config['hosts'][0]
            def read(*args):
                m.settings.delete(dict(host='one',confirmation='one'))
                return []
            with patch('monitor.read_task_archive',side_effect=read):m.refresh_archives(old,force=True)
            self.assertNotIn('one',m.archives)
            m.cloud.refresh('one',force=True)
            self.assertNotIn('one',m.cloud.host_states)
