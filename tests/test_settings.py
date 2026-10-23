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
        with tempfile.TemporaryDirectory() as d,patch('monitor.ROOT',Path(d)):
            m=self.setup_monitor(d);m.trackers['one'].previous[1]={'x':1}
            m.settings.automation(dict(host='one',enabled=True,idle_seconds=120))
            snap=m.snapshot()['hosts'];self.assertTrue(snap[0]['auto_enabled']);self.assertFalse(snap[1]['auto_enabled'])
            self.assertFalse(m.trackers['one'].previous)
            persisted=load_config(Path(d)/'config.json');self.assertTrue(persisted['hosts'][0]['auto_enabled'])
            m.settings.automation(dict(host='one',enabled=False,idle_seconds=60))
            self.assertFalse(m.snapshot()['hosts'][0]['auto_enabled'])
            with self.assertRaises(ValueError):m.settings.automation(dict(host='one',enabled=True,idle_seconds=1))

    def test_add_defaults_observe_secrets_and_dynamic_state(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.ROOT',Path(d)):
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
        with tempfile.TemporaryDirectory() as d,patch('monitor.ROOT',Path(d)):
            m=self.setup_monitor(d);key=paramiko.RSAKey.generate(1024)
            m.settings.pending['t']=dict(id='three',address='10.0.0.3',port=2222,alias='[10.0.0.3]:2222',key=key,expires=time.monotonic()+120)
            with patch.object(m,'poll_host',return_value={'state':'在线'}):m.settings.add(dict(probe='t',trust=True,credentials_from='one'))
            self.assertEqual(m.config['hosts'][-1]['credentials_file'],'password.txt')
            self.assertNotIn('credentials_file',m.snapshot()['hosts'][-1])

    def test_probe_changed_key_and_confirmation(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as d,patch('monitor.ROOT',Path(d)):
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
        with tempfile.TemporaryDirectory() as d,patch('monitor.ROOT',Path(d)):
            m=self.setup_monitor(d)
            for b in [dict(id='one',address='10.0.0.8',port=22),dict(id='x',address='10.0.0.1',port=22),dict(id='../../x',address='10.0.0.8',port=22),dict(id='x',address='invalid',port=22),dict(id='x',address='10.0.0.8',port=0)]:
                with self.assertRaises(ValueError):m.settings.probe(b)
