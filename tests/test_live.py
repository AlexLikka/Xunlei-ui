import unittest
from live_tracker import LiveTracker
from thunder_api import DELETE


def row(status=5, speed=0, active=True, received=10, name='a'):
    return dict(id=1,status=status,speed=speed,active=active,received=received,name=name,save_path='D:/a',size=100)


class LiveTests(unittest.TestCase):
    def test_queue_pause_unknown_never_eligible(self):
        for status in (0,1,2,3,4,6,7,8,9,10,999):
            t=LiveTracker(10,30)
            t.update([row(status=status)],0)
            self.assertFalse(t.update([row(status=status)],20)[0]['delete_eligible'])
        for r in (row(active=False),row(speed=None),row(speed=1)):
            t=LiveTracker(10,30);t.update([r],0)
            self.assertFalse(t.update([r],20)[0]['delete_eligible'])

    def test_continuous_zero_and_all_resets(self):
        for changed in (row(status=3),row(speed=1),row(active=False),row(received=11),row(name='b')):
            t=LiveTracker(20,30)
            t.update([row()],0)
            self.assertTrue(t.update([row()],20)[0]['delete_eligible'])
            self.assertFalse(t.update([changed],25)[0]['delete_eligible'])
            self.assertFalse(t.update([row()],30)[0]['delete_eligible'])

    def test_gap_failure_disappearance(self):
        t=LiveTracker(20,30);t.update([row()],0)
        self.assertFalse(t.update([row()],100)[0]['delete_eligible'])
        t.clear();self.assertFalse(t.update([row()],110)[0]['delete_eligible'])
        t.update([],115);self.assertFalse(t.update([row()],120)[0]['delete_eligible'])

    def test_delete_protocol_always_preserves_files(self):
        self.assertIn("'TaskManagerTaskDeleteTask' @($id,$false)",DELETE)
        self.assertIn('$t.taskStatus -eq 5',DELETE)
        self.assertIn('$id -in $ids',DELETE)
        self.assertIn('$t.downloadSize -eq $expectedBytes',DELETE)

if __name__=='__main__':unittest.main()

class MonitorSafetyTests(unittest.TestCase):
    def run_mode(self, mode, remover):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import monitor
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor,'ROOT',Path(directory)):
            config=dict(mode=mode,poll_seconds=5,idle_seconds=10,hosts=[{'id':'pc','address':'example'}])
            m=monitor.Monitor(config)
            r={**row(), 'completed':0}
            with patch.object(monitor,'read_live_tasks',return_value=[r]), patch.object(monitor,'remove_keep_files',remover):
                with patch.object(monitor.time,'monotonic',return_value=0):m.poll_host(config['hosts'][0])
                with patch.object(monitor.time,'monotonic',return_value=10):result=m.poll_host(config['hosts'][0])
            return result,m,monitor.Monitor(config)

    def test_observe_does_not_remove(self):
        from unittest.mock import Mock
        remove=Mock();result,m,_=self.run_mode('observe',remove)
        remove.assert_not_called()
        self.assertTrue(result['tasks'][0]['delete_eligible'])

    def test_auto_keeps_files_and_journals_success(self):
        from unittest.mock import Mock
        remove=Mock(return_value={'deleted':True,'filesDeleted':False})
        result,m,reloaded=self.run_mode('auto',remove)
        remove.assert_called_once()
        self.assertEqual(result['tasks'],[])
        self.assertEqual(m.events[-1]['event'],'removed_keep_files')
        self.assertIn(('pc',1),reloaded.uncertain)

    def test_timeout_is_not_retried_after_restart(self):
        from unittest.mock import Mock
        remove=Mock(side_effect=TimeoutError())
        _,m,reloaded=self.run_mode('auto',remove)
        self.assertEqual(m.events[-1]['event'],'uncertain_no_retry')
        self.assertIn(('pc',1),reloaded.uncertain)

    def test_recheck_change_skips_and_resets(self):
        from unittest.mock import Mock
        remove=Mock(return_value={'deleted':False,'reason':'changed'})
        _,m,reloaded=self.run_mode('auto',remove)
        self.assertNotIn(1,m.trackers['pc'].previous)
        self.assertNotIn(('pc',1),reloaded.uncertain)
