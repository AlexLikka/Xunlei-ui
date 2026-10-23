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
            outcome={'outcome':'paused'} if mode=='pause' else {'outcome':'deleted','filesRequested':files}
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
