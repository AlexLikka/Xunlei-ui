import threading
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
        m=Mock();m.stop=threading.Event();m.lock=threading.Lock();m.config={'hosts':[{'id':'a'},{'id':'b'}],'poll_seconds':15};m.actions.lock=threading.Lock();m.actions.plans={};return m

    def test_fleet_isolation_recheck_and_keep_only(self):
        m=self.monitor();f=Fleet(m)
        def prepare(host,cmd):
            self.assertEqual(cmd,'del_zero')
            if host=='b':raise RuntimeError('offline')
            return {'plan':'p','count':2}
        m.actions.prepare.side_effect=prepare;m.actions.execute.return_value='j'
        m.actions.status.return_value={'state':'done','results':[{'outcome':'deleted'},{'outcome':'skipped'}]}
        f.start();f.join()
        r=f.snapshot([])['acceleration'];self.assertEqual(r['state'],'finished');self.assertEqual(r['hosts']['a']['deleted'],1);self.assertEqual(r['hosts']['a']['skipped'],1);self.assertEqual(r['hosts']['b']['state'],'error')
        m.actions.execute.assert_called_once_with('p','')

    def test_duplicate_start_and_zero_candidates(self):
        m=self.monitor();gate=threading.Event();f=Fleet(m)
        m.actions.prepare.side_effect=lambda *a:(gate.wait(2) or None) and {'plan':'p','count':0}
        a=f.start();b=f.start();self.assertEqual(a['run'],b['run']);self.assertTrue(b['existing']);gate.set();f.join();m.actions.execute.assert_not_called()

    def test_history_bounded_and_gap(self):
        m=self.monitor();f=Fleet(m)
        for i in range(250):f.sample({'id':'a','state':'异常','tasks':[]})
        self.assertEqual(len(f.history['a']),240);self.assertIsNone(f.history['a'][-1]['speed'])
