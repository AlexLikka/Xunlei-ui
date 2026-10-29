import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from manual_actions import candidate,perform,COMMANDS
from monitor import Monitor

def task(id=1,status=5,speed=0,active=True,received=19,size=100):
    return dict(id=id,status=status,speed=speed,active=active,received=received,size=size,name='fixture',save_path='D:/test',completed=0)

class ManualTests(unittest.TestCase):
    def test_filters_boundary(self):
        self.assertTrue(candidate(task(),True))
        self.assertFalse(candidate(task(received=20),True))
        self.assertFalse(candidate(task(size=0),True))
        for t in (task(status=3),task(status=7),task(speed=1),task(speed=None),task(active=False)):
            self.assertFalse(candidate(t))

    def test_preview_and_single_use(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,mode='observe',hosts=[{'id':'pc','address':'example'}]))
            rows=[task(),task(2,status=3),task(3,received=20),task(4,speed=1)]
            with patch('manual_actions.read_live_tasks',return_value=rows):
                p=m.actions.prepare('pc','del_zero20')
                self.assertEqual([t['id'] for t in p['tasks']],[1])
                p2=m.actions.prepare('pc','delf',2)
                with self.assertRaises(ValueError):m.actions.execute(p2['plan'],'wrong')
                self.assertEqual(p2['count'],1)
                with patch('manual_actions.perform',return_value={'outcome':'skipped'}),patch.object(m,'_poll_host'):
                    job=m.actions.execute(p['plan'],'')
                    m.actions.join()
                self.assertEqual(m.actions.status(job)['results'][0]['outcome'],'skipped')
                with self.assertRaises(ValueError):m.actions.execute(p['plan'],'')

    def test_start_all_and_custom_progress_limit(self):
        with tempfile.TemporaryDirectory() as d:
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[{'id':'pc','address':'example'}]),data_dir=Path(d))
            rows=[task(1,received=37),task(2,received=38),task(3,status=7),task(4,status=9),
                  dict(task(5,status=7),view='recycle',recycle_time=12,read_only=True)]
            with patch('manual_actions.read_live_tasks',return_value=rows):
                p=m.actions.prepare('pc','pause_zero',progress_limit=38)
                self.assertEqual([t['id'] for t in p['tasks']],[1])
                p=m.actions.prepare('pc','start_all')
                self.assertEqual([t['id'] for t in p['tasks']],[3,4])

    def test_batch_freezes_preview_and_stops_on_uncertainty(self):
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[{'id':'pc','address':'example'}]))
            with patch('manual_actions.read_live_tasks',return_value=[task(),task(2)]):p=m.actions.prepare('pc','pause_zero')
            with patch('manual_actions.perform',side_effect=TimeoutError()) as call,patch.object(m,'_poll_host'):
                j=m.actions.execute(p['plan'],'');m.actions.join()
                self.assertEqual(call.call_count,1)
            self.assertEqual(m.actions.status(j)['state'],'stopped')
            self.assertIn(('pc',1),m.uncertain)
            reloaded=Monitor(m.config);self.assertIn(('pc',1),reloaded.uncertain)

    def test_every_command_payload(self):
        import re,json,base64
        for cmd,(_,mode,files,under20,bulk) in COMMANDS.items():
            outcome={'outcome':'started'} if mode=='start' else {'outcome':'paused'} if mode=='pause' else {'outcome':'deleted','filesRequested':files}
            with patch('manual_actions.remote',return_value=outcome) as call:
                perform({},cmd,task())
                script=call.call_args.args[1]
                encoded=re.search("FromBase64String\\('([^']+)'\\)",script).group(1)
                p=json.loads(base64.b64decode(encoded))
                self.assertEqual((p['files'],p['under20'],p['bulk'],p['mode']),(files,under20,bulk,mode))

class ManualHTTPTests(unittest.TestCase):
    def test_http_preview_confirmation_execute_and_auth(self):
        import json,urllib.request,urllib.error
        from http.server import ThreadingHTTPServer
        from monitor import handler_for
        with tempfile.TemporaryDirectory() as d,patch('monitor.DATA',Path(d)):
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[{'id':'pc','address':'example'}]))
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(m,'test-token'))
            thread=threading.Thread(target=server.serve_forever);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path,body,token='test-token',origin=None):
                headers={'X-Monitor-Token':token,'Content-Type':'application/json'}
                if origin:headers['Origin']=origin
                req=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers=headers)
                try:
                    with opener.open(req) as r:return r.status,json.load(r)
                except urllib.error.HTTPError as e:
                    status=e.code;e.close();return status,None
            try:
                self.assertEqual(post('/api/actions/preview',{},token='bad')[0],403)
                self.assertEqual(post('/api/actions/preview',{},origin='http://evil.example')[0],403)
                with patch('manual_actions.read_live_tasks',return_value=[task()]),patch('manual_actions.perform',return_value={'outcome':'deleted','filesRequested':True}),patch.object(m,'_poll_host'):
                    status,p=post('/api/actions/preview',{'host':'pc','command':'delf','task_id':1})
                    self.assertEqual(status,200)
                    self.assertEqual(post('/api/actions/execute',{'plan':p['plan'],'confirmation':''})[0],400)
                    status,j=post('/api/actions/execute',{'plan':p['plan'],'confirmation':'删除文件'})
                    self.assertEqual(status,200);m.actions.join()
                    status,result=post('/api/actions/status',{'job':j['job']})
                    self.assertEqual(result['results'][0]['outcome'],'deleted')
                    self.assertEqual(post('/api/actions/execute',{'plan':p['plan'],'confirmation':'删除文件'})[0],400)
            finally:
                server.shutdown();server.server_close();thread.join()

class BatchedManualTests(unittest.TestCase):
    def test_cached_preview_and_expiry_only_apply_before_acceptance(self):
        import time
        with tempfile.TemporaryDirectory() as path:
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='pc',address='example')]),data_dir=Path(path))
            m.hosts['pc']=dict(id='pc',state='在线',checked_at=time.time(),tasks=[task(status=7)])
            with patch('manual_actions.read_live_tasks') as read:
                p=m.actions.prepare('pc','start_all')
                self.assertEqual(p['expires_seconds'],900);read.assert_not_called()
            m.actions.plans[p['plan']]['expires']=time.monotonic()+1
            with patch('manual_actions.perform_many',return_value=[dict(id=1,outcome='started')]):
                with m.operation_locks['pc']:
                    key=m.actions.execute(p['plan'],'')
                    self.assertEqual(m.actions.status(key)['phase'],'waiting')
                    m.actions.jobs[key]['created_at']-=1800
                m.actions.join()
            self.assertEqual(m.actions.status(key)['state'],'done')

    def test_start_all_uses_two_small_concurrent_chunks(self):
        from manual_actions import perform_many
        with tempfile.TemporaryDirectory() as path:
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='pc',address='example')]),data_dir=Path(path))
            with patch('manual_actions.read_live_tasks',return_value=[task(i,status=7) for i in range(1,17)]):p=m.actions.prepare('pc','start_all')
            together=threading.Barrier(2,timeout=3)
            def run(host,command,rows,*args):
                self.assertLessEqual(len(rows),8)
                for row in rows:self.assertIn(('pc',row['id']),m.uncertain)
                together.wait();return [dict(id=t['id'],outcome='started') for t in rows]
            with patch('manual_actions.perform_many',side_effect=run) as batched,patch('manual_actions.perform') as single:
                key=m.actions.execute(p['plan'],'');m.actions.join()
                self.assertEqual(batched.call_count,2);single.assert_not_called()
            state=m.actions.status(key);self.assertEqual(state['processed'],16);self.assertEqual(state['state'],'done')

    def test_delete_chunk_timeout_stops_remaining_chunks(self):
        with tempfile.TemporaryDirectory() as path:
            m=Monitor(dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='pc',address='example')]),data_dir=Path(path))
            with patch('manual_actions.read_live_tasks',return_value=[task(i) for i in range(1,21)]):p=m.actions.prepare('pc','del_zero')
            with patch('manual_actions.perform_many',side_effect=TimeoutError()) as run:
                key=m.actions.execute(p['plan'],'');m.actions.join();self.assertEqual(run.call_count,1)
            result=m.actions.status(key);self.assertEqual(result['state'],'stopped');self.assertEqual(result['processed'],8)
            self.assertTrue(all(('pc',i) in m.uncertain for i in range(1,9)))

    def test_batch_results_are_bound_to_exact_task_ids(self):
        from manual_actions import perform_many
        with patch('manual_actions.remote',return_value={'results':[dict(id=999,outcome='started')]}):
            with self.assertRaises(RuntimeError):perform_many({},'start_all',[task()])
