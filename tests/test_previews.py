import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import Monitor


class PreviewTests(unittest.TestCase):
    def test_slow_preview_acknowledges_deduplicates_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as path:
            m=Monitor(dict(hosts=[dict(id='pc',address='example')],poll_seconds=15,idle_seconds=600),data_dir=Path(path))
            gate=threading.Event();entered=threading.Event()
            def prepare(*args):
                entered.set();gate.wait(3);return dict(plan='confirmed',count=2)
            with patch.object(m.actions,'prepare',side_effect=prepare) as read,patch.object(m.actions,'execute') as execute:
                body=dict(kind='manual',host='pc',command='start_all')
                first=m.previews.start(body);self.assertTrue(entered.wait(1))
                second=m.previews.start(body)
                self.assertTrue(second['existing']);self.assertEqual(first['preview'],second['preview'])
                self.assertEqual(m.previews.status(first['preview'])['state'],'running')
                execute.assert_not_called();gate.set();m.previews.join()
                self.assertEqual(m.previews.status(first['preview'])['result']['plan'],'confirmed')
                self.assertEqual(read.call_count,1)

    def test_global_preview_workers_are_bounded_and_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as path:
            hosts=[dict(id=str(i),address='example') for i in range(4)]
            m=Monitor(dict(hosts=hosts,poll_seconds=15,idle_seconds=600),data_dir=Path(path))
            lock=threading.Lock();gate=threading.Event();running=0;peak=0;two=threading.Event()
            def prepare(*args):
                nonlocal running,peak
                with lock:
                    running+=1;peak=max(peak,running)
                    if running==2:two.set()
                gate.wait(3)
                with lock:running-=1
                raise RuntimeError('secret credentials')
            with patch.object(m.actions,'prepare',side_effect=prepare):
                keys=[m.previews.start(dict(kind='manual',host=str(i),command='start_all'))['preview'] for i in range(4)]
                self.assertTrue(two.wait(1));self.assertEqual(peak,2)
                self.assertEqual(m.previews.status(keys[-1])['state'],'queued')
                gate.set();m.previews.join()
            self.assertEqual(peak,2)
            for key in keys:
                status=m.previews.status(key)
                self.assertEqual(status['state'],'error');self.assertNotIn('secret',status['error'])
