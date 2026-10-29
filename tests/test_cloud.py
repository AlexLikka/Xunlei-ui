import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cloud_transfer import CloudRemote, CloudTransfers, options, public_status, transferable
from monitor import Monitor
from live_tracker import LiveTracker


def row(id=1, **updates):
    return dict(dict(id=id, name='magnet', save_path='D:\\test', size=100, received=19,
                     speed=0, status=5, active=True, view='incomplete', recycle_time=0,
                     task_type=2, is_pan_task=False, url='magnet:?xt=urn:btih:'+'a'*40), **updates)


class CloudTests(unittest.TestCase):
    def monitor(self, path):
        return Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='pc',address='example')]),data_dir=Path(path))

    def test_preview_uses_bounded_queries_without_waiting_for_poll_lock(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock()
            def read(*args,**kwargs):
                self.assertTrue(kwargs['active_only']);return [row()]
            def status(*args):
                return {'state':{}}
            m.cloud.remote.status.side_effect=status
            finished=threading.Event();results=[]
            def prepare():
                try:results.append(m.cloud.prepare({'host':'pc','command':'transfer'}))
                except Exception as exc:results.append(exc)
                finally:finished.set()
            with patch('cloud_transfer.read_live_tasks',side_effect=read):
                with m.operation_locks['pc']:
                    worker=threading.Thread(target=prepare);worker.start()
                    completed=finished.wait(3)
                worker.join(3)
            self.assertTrue(completed,'read-only preview waited for periodic polling')
            self.assertIsInstance(results[0],dict)
            self.assertEqual(results[0]['count'],1)

    def test_submit_acknowledges_before_remote_machine_is_available(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock()
            m.cloud.remote.status.return_value={'state':{},'workers':{}}
            m.cloud.remote.launch.return_value={'launched':True}
            with patch('cloud_transfer.read_live_tasks',return_value=[row()]):
                p=m.cloud.prepare({'host':'pc','command':'transfer'})
            with m.operation_locks['pc']:
                result=m.cloud.execute(p['plan'])
                self.assertEqual(m.cloud.jobs[result['job']]['phase'],'waiting_for_machine')
                self.assertTrue(m.cloud.journal.exists())
                m.cloud.remote.launch.assert_not_called()
            m.cloud.join();m.cloud.remote.launch.assert_called_once()

    def test_options_and_exact_threshold(self):
        for value in [True, 0, -1, 101, float('nan'), '20']:
            with self.assertRaises(ValueError): options({'progress_limit':value})
        for value in [True, -1, 1.2, 1441]:
            with self.assertRaises(ValueError): options({'wait_minutes':value})
        for value in ['relative', 'D:\\..\\private', 'D:\\CON.txt', 'D:\\folder\\.', 'D:\\bad\nname']:
            with self.assertRaises(ValueError): options({'output':value})
        self.assertEqual(options({'output':'D:\\电影 下载'})['output'],'D:\\电影 下载')
        self.assertTrue(transferable(row()))
        for t in [row(received=20),row(active=False),row(status=3),row(is_pan_task=True),row(origin='cloud_bridge_v1'),row(speed=None),row(view='recycle',recycle_time=12)]:
            self.assertFalse(transferable(t))
        self.assertTrue(transferable(row(view='recycle',recycle_time=12,status=7,read_only=True),recycle=True))

    def test_namespace_is_per_workspace_machine_and_address(self):
        a=CloudRemote('/data/users/one');b=CloudRemote('/data/users/two');host={'id':'pc','address':'10.0.0.1'}
        self.assertNotEqual(a.namespace(host),b.namespace(host))
        self.assertNotEqual(a.namespace(host),a.namespace(dict(host,id='other')))
        self.assertEqual(a.namespace(host),CloudRemote('/data/users/one').namespace(host))

    def test_preview_filters_archives_native_downloads_and_tracked_tasks(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock();m.cloud.remote.status.return_value={'state':{'jobs':{'2':{}}}}
            with patch('cloud_transfer.read_live_tasks',return_value=[row(),row(2),row(3,received=20),row(4,is_pan_task=True)]):
                preview=m.cloud.prepare({'host':'pc','command':'transfer'})
            self.assertEqual([t['id'] for t in preview['tasks']],[1])
            m.cloud.remote.launch.assert_not_called()
            self.assertFalse(m.cloud.protected('pc',1))
            m.cloud.remote.status.assert_called_once_with(m.config['hosts'][0])
            with self.assertRaises(ValueError):m.cloud.prepare({'host':'other','command':'transfer'})
            with self.assertRaises(ValueError):m.cloud.prepare({'host':'pc','command':'recycle'})

    def test_execute_persists_before_dispatch_single_use_and_keeps_identity(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock();m.cloud.remote.status.return_value={'state':{},'workers':{}}
            with patch('cloud_transfer.read_live_tasks',return_value=[row()]):p=m.cloud.prepare({'host':'pc','command':'transfer'})
            def launch(host,job):
                saved=json.loads(m.cloud.journal.read_text());self.assertEqual(saved[job['id']]['state'],'dispatching')
                self.assertEqual(job['tasks'][0]['url'],row()['url'])
                self.assertTrue(m.cloud.protected('pc',1))
                return {'launched':True}
            m.cloud.remote.launch.side_effect=launch
            result=m.cloud.execute(p['plan']);m.cloud.join()
            with self.assertRaises(ValueError):m.cloud.execute(p['plan'])
            restarted=CloudTransfers(m)
            self.assertEqual(restarted.jobs[result['job']]['state'],'recovering')
            self.assertTrue(restarted.protected('pc',1))
            self.assertTrue(restarted.is_busy('pc'))
            public=restarted.snapshot();self.assertNotIn('magnet:?',json.dumps(public))

    def test_timeout_never_replays_remote_mutation(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock();m.cloud.remote.status.return_value={'state':{},'workers':{}}
            with patch('cloud_transfer.read_live_tasks',return_value=[row()]):p=m.cloud.prepare({'host':'pc','command':'transfer'})
            m.cloud.remote.launch.side_effect=TimeoutError()
            result=m.cloud.execute(p['plan']);m.cloud.join();m.cloud.refresh('pc',True)
            self.assertEqual(m.cloud.jobs[result['job']]['state'],'uncertain')
            m.cloud.remote.launch.assert_called_once()
            self.assertTrue(m.cloud.protected('pc',1))

    def test_remote_completion_and_cache_request_state_are_distinct(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock()
            m.cloud.jobs['a']={'id':'a','host':'pc','state':'running','command':'transfer','tasks':[row()], 'created_at':time.time()}
            m.cloud.busy.add('pc')
            m.cloud.remote.status.return_value={'workers':{'a':{'id':'a','state':'done','result':{'request':{'id':'req'}}}},'state':{},'requests':[{'id':'req','stage':'waiting','jobIds':['1'],'notBefore':9999}]}
            m.cloud.refresh('pc',True)
            snap=m.cloud.snapshot();self.assertEqual(snap['jobs'][0]['state'],'done')
            self.assertEqual(snap['hosts']['pc']['requests'][0]['stage'],'waiting')
            self.assertFalse(m.cloud.is_busy('pc'))
            m.cloud.remote.status.side_effect=TimeoutError();m.cloud.refresh('pc',True)
            self.assertEqual(m.cloud.snapshot()['hosts']['pc']['requests'][0]['stage'],'waiting')
            self.assertEqual(m.cloud.snapshot()['hosts']['pc']['state'],'error')

    def test_public_status_never_exposes_sdk_errors_or_source_credentials(self):
        raw={'state':{'ownerId':'secret-user','jobs':{'1':dict(id='1',name='file',error='Authorization: secret-token',url='private',downloads={'x':dict(fileId='x',error='password: secret')})}},'requests':[dict(id='r',errors=[{'message':'secret'}],results=[dict(id='1',ready=False,reason='secret')])]}
        out=public_status(raw)
        self.assertNotIn('secret',json.dumps(out));self.assertNotIn('Authorization',json.dumps(out))
        self.assertTrue(out['jobs'][0]['needs_review'])

    def test_cloud_downloads_never_eligible_for_legacy_automatic_cleanup(self):
        tracker=LiveTracker(10,30);tracker.update([row(is_pan_task=True)],0)
        self.assertFalse(tracker.update([row(is_pan_task=True)],20)[0]['delete_eligible'])

    def test_download_preview_binds_resource_units_and_requires_windows_destination(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock()
            with self.assertRaises(ValueError):m.cloud.prepare({'host':'pc','command':'download'})
            m.cloud.remote.list_cloud.return_value={'resources':[dict(id='folder',name='ready',ready=True,remaining=2,fileIds=['one','two']),dict(id='pending',ready=False,remaining=0)]}
            result=m.cloud.prepare({'host':'pc','command':'download','output':'D:\\downloads'})
            self.assertEqual(result['count'],1)
            self.assertEqual(m.cloud.plans[result['plan']]['units'][0]['fileIds'],['one','two'])

    def test_recycle_preview_binds_cleanup_policy_and_persists_at_confirmation(self):
        with tempfile.TemporaryDirectory() as path:
            m=self.monitor(path);m.cloud.remote=Mock()
            m.cloud.remote.status.return_value={'state':{},'workers':{}}
            m.cloud.remote.launch.return_value={'launched':True}
            task=row(view='recycle',recycle_time=123,status=7,read_only=True)
            with patch('cloud_transfer.read_task_archive',return_value=[task]):
                legacy=m.cloud.prepare(dict(host='pc',command='recycle',all=True,cache_only=True))
                self.assertFalse(legacy['cleanup_recycle'])
                p=m.cloud.prepare(dict(host='pc',command='recycle',all=True,cache_only=True,cleanup_recycle=True))
            self.assertTrue(p['cleanup_recycle'])
            self.assertTrue(m.cloud.plans[p['plan']]['cleanup_recycle'])
            m.cloud.remote.launch.assert_not_called()
            with patch('cloud_transfer.read_live_tasks',return_value=[row()]):
                ordinary=m.cloud.prepare(dict(host='pc',command='transfer'))
            self.assertFalse(ordinary['cleanup_recycle'])
            result=m.cloud.execute(p['plan']);m.cloud.join()
            self.assertTrue(json.loads(m.cloud.journal.read_text())[result['job']]['cleanup_recycle'])
            self.assertTrue(m.cloud.remote.launch.call_args.args[1]['cleanup_recycle'])

    def test_cleanup_status_is_visible_without_raw_error_or_credentials(self):
        raw=dict(state=dict(jobs={'1':dict(id='1',source='recycle',cleanupRecycle=True,recycleCleanup='uncertain',recycleCleanupError='private token')}),
                 requests=[dict(id='r',cleanupRecycle=True,cleanupStage='waiting',cleanupPending=2,cleanupCleaned=1,cleanupReview=0,cleanupNextCheck=100,cleanupError='private token')])
        out=public_status(raw)
        self.assertEqual(out['jobs'][0]['recycle_cleanup'],'uncertain')
        self.assertTrue(out['jobs'][0]['recycle_cleanup_review'])
        self.assertEqual(out['requests'][0]['cleanupPending'],2)
        self.assertNotIn('private',json.dumps(out))
