"""Local lifecycle control; never signal a PID read from a stale file."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.request


class Instance:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.info = self.directory / 'control.json'
        self.token = secrets.token_urlsafe(32)
        self.lock = None

    def acquire(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.directory / 'instance.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            return False
        return True

    def publish(self, port):
        temporary = self.directory / 'control.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'port': port, 'token': self.token}, stream)
        temporary.replace(self.info)

    def close(self):
        if self.lock is not None:
            self.info.unlink(missing_ok=True)
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None


def stop_instance(directory, timeout=40):
    instance = Instance(directory)
    if instance.acquire():
        instance.close()
        return '监控未运行。'
    try:
        info = json.loads(instance.info.read_text())
        port = int(info['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid local port')
        request = urllib.request.Request(
            f'http://127.0.0.1:{port}/api/shutdown', data=b'', method='POST',
            headers={'X-Monitor-Token': info['token']})
        # Never send the local control token through environment HTTP proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            if response.status != 202:
                raise ValueError('Unexpected shutdown response')
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        raise RuntimeError('无法关闭监控：请稍后重试，或在启动窗口按 Control+C。') from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.acquire():
            instance.close()
            return '监控已关闭，网页服务和远程采样均已停止。'
        time.sleep(0.2)
    raise RuntimeError('已发送关闭请求，仍在等待当前查询结束。请稍后再次运行关闭监控。')
