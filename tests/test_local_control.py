import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from local_control import Instance, stop_instance
from monitor import Monitor, handler_for


class LifecycleTests(unittest.TestCase):
    def test_lock_and_stale_record(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Instance(directory), Instance(directory)
            self.assertTrue(a.acquire())
            a.publish(12345)
            self.assertFalse(b.acquire())
            self.assertEqual((Path(directory) / 'control.json').stat().st_mode & 0o777, 0o600)
            a.close()
            self.assertTrue(b.acquire())
            b.close()
            (Path(directory) / 'control.json').write_text('{"port":12345,"token":"stale"}')
            self.assertEqual(stop_instance(directory), '监控未运行。')
            self.assertFalse((Path(directory) / 'control.json').exists())

    def test_only_authenticated_shutdown_stops_server(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = Instance(directory)
            self.assertTrue(instance.acquire())
            monitor = Monitor({'idle_seconds': 600, 'poll_seconds': 15,
                               'hosts': [{'id': 'fake', 'address': 'unused'}]})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(monitor, instance.token))
            instance.publish(server.server_port)

            def serve():
                try:
                    server.serve_forever(poll_interval=0.01)
                finally:
                    server.server_close()
                    instance.close()

            thread = threading.Thread(target=serve)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(base + '/api/status') as response:
                    data = json.load(response)
                self.assertTrue(data['delete_supported'])
                self.assertEqual(data['mode'], 'observe')
                self.assertNotIn(instance.token, json.dumps(data))
                for path, token, expected in [('/api/shutdown', '', 403),
                                              ('/api/shutdown', 'wrong', 403),
                                              ('/api/delete', instance.token, 404)]:
                    request = urllib.request.Request(base + path, data=b'',
                        headers={'X-Monitor-Token': token}, method='POST')
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(request)
                    self.assertEqual(error.exception.code, expected)
                    error.exception.close()
                    self.assertFalse(monitor.stop.is_set())
                self.assertIn('已关闭', stop_instance(directory, timeout=3))
                self.assertTrue(monitor.stop.is_set())
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
