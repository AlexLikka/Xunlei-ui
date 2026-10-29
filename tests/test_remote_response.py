"""SSH can report process exit before all stdout packets have arrived."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from thunder_api import remote


class DelayedOutput:
    def __init__(self, payload):
        self.packets = [payload[:31], payload[31:]]
        self.ticks = 0
        self.received = 0
        self.closed = False

    @property
    def eof_received(self):
        return self.received == 2

    def recv_ready(self):
        self.ticks += 1
        return bool(self.packets) and (self.received == 0 or self.ticks >= 5)

    def recv(self, size):
        self.received += 1
        return self.packets.pop(0)

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return 0


class RemoteResponseTests(unittest.TestCase):
    def test_early_exit_status_does_not_truncate_output_and_unicode_is_not_a_record_boundary(self):
        expected = {'tasks': [{'name': '测试\u0085\u2028\u2029任务', 'url': 'magnet:?xt=' + 'a'*300000}]}
        payload = ('THUNDER_CLI_JSON:' + json.dumps(expected, ensure_ascii=False) + '\r\n').encode()
        channel = DelayedOutput(payload)
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'credentials.txt').write_text('user\npassword')
            with patch('thunder_api.paramiko.SSHClient', return_value=client), patch('thunder_api.time.sleep'):
                result = remote(dict(address='example', credentials_file='credentials.txt'), '', root)
        self.assertEqual(result, expected)
        self.assertEqual(channel.received, 2)
        client.close.assert_called_once()
