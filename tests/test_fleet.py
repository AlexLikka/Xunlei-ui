import threading
import time
import unittest
from unittest.mock import Mock
from fleet import Fleet,summarize

class FleetTests(unittest.TestCase):
    def test_summary_unknown_offline_queue(self):
        h=dict(id='a',state='在线',checked_at=100,tasks=[dict(status=5,active=True,speed=100),dict(status=5,active=True,speed=0),dict(status=3,active=False,speed=0)])
        r=summarize(h,101,60)
        self.assertEqual((r['speed'],r['zero'],r['queued'],r['transmitting']),(100,1,1,1))
        self.assertIsNone(summarize(h,200,60)['speed'])
        h['tasks'].append(dict(status=5,active=True,speed=None));self.assertIsNone(summarize(h,101,60)['speed'])

    def test_distribution_includes_paused_and_other(self):
        rows=[dict(status=5,active=True,speed=10),dict(status=3,active=False,speed=0),dict(status=7,active=False,speed=0),dict(status=9,active=False,speed=0)]
        r=summarize(dict(id='x',state='在线',checked_at=100,tasks=rows),101,60)
        self.assertEqual((r['tasks'],r['downloading'],r['queued'],r['paused'],r['other']),(4,1,1,1,1))

    def monitor(self):
        m=Mock();m.stop=threading.Event();m.lock=threading.Lock();m.config={'hosts':[{'id':'a'},{'id':'b'}],'poll_seconds':15};m.actions.lock=threading.Lock();m.actions.plans={};m.cloud.lock=threading.Lock();m.cloud.plans={};return m

    def test_fleet_isolation_recheck_and_keep_only(self):
        m=self.monitor();f=Fleet(m)
        def prepare(cmd):
            host=cmd['host']
            self.assertEqual(cmd['command'],'transfer');self.assertEqual(cmd['progress_limit'],20)
            if host=='b':raise RuntimeError('offline')
            return {'plan':'p','count':2}
        m.cloud.prepare.side_effect=prepare;m.cloud.execute.return_value={'job':'j'}

        f.start();f.join()
        r=f.snapshot([])['acceleration'];self.assertEqual(r['state'],'finished');self.assertEqual(r['hosts']['a']['submitted'],2);self.assertEqual(r['hosts']['b']['state'],'error')
        m.cloud.execute.assert_called_once_with('p');m.actions.execute.assert_not_called()

    def test_duplicate_start_and_zero_candidates(self):
        m=self.monitor();gate=threading.Event();f=Fleet(m)
        m.cloud.prepare.side_effect=lambda *a:(gate.wait(2) or None) and {'plan':'p','count':0}
        a=f.start();b=f.start();self.assertEqual(a['run'],b['run']);self.assertTrue(b['existing']);gate.set();f.join();m.cloud.execute.assert_not_called()

    def test_history_bounded_and_gap(self):
        m=self.monitor();f=Fleet(m)
        for i in range(250):f.sample({'id':'a','state':'异常','tasks':[]})
        self.assertEqual(len(f.history['a']),240);self.assertIsNone(f.history['a'][-1]['speed'])

    def test_confirmed_plans_submit_in_parallel_without_rescanning(self):
        m=self.monitor();f=Fleet(m);together=threading.Barrier(2,timeout=2)
        m.cloud.plans={key:dict(host=host,expires=time.monotonic()+120,command='transfer',tasks=[{}])
                       for key,host in [('pa','a'),('pb','b')]}
        def execute(token):
            together.wait();return {'job':token}
        m.cloud.execute.side_effect=execute
        f.start({'plans':[{'plan':'pa'},{'plan':'pb'}]});f.join()
        result=f.snapshot([])['acceleration']
        self.assertEqual([r['state'] for r in result['hosts'].values()],['submitted','submitted'])
        m.cloud.prepare.assert_not_called()
