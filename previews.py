"""Bounded asynchronous previews: acknowledge HTTP before slow remote reads."""
import json
import secrets
import threading
import time

from resource_limits import PREVIEW_WORKERS

_capacity = threading.BoundedSemaphore(8)


class Previews:
    def __init__(self, monitor):
        self.m = monitor
        self.lock = threading.Lock()
        self.requests = {}
        self.futures = []

    def start(self, body):
        kind = body.get('kind')
        if kind not in ('manual', 'cloud'):
            raise ValueError('预览类型无效')
        host = self.m.actions.host(body.get('host'))
        if not host or self.m.stop.is_set():
            raise ValueError('请选择机器，或服务正在关闭')
        signature = json.dumps(body, sort_keys=True)
        with self.lock:
            now = time.time()
            self.requests = {k: v for k, v in self.requests.items()
                             if v['state'] in ('queued', 'running') or now - v['created_at'] < 900}
            for key, value in self.requests.items():
                if value['signature'] == signature and value['state'] in ('queued', 'running'):
                    return {'preview': key, 'existing': True}
            if len(self.requests) >= 16 or not _capacity.acquire(blocking=False):
                raise ValueError('预览队列已满，请等待当前预览完成')
            key = secrets.token_hex(16)
            self.requests[key] = dict(state='queued', host=host['id'], created_at=now, signature=signature)
            try:
                self.futures = [f for f in self.futures if not f.done()]
                self.futures.append(PREVIEW_WORKERS.submit(self._run, key, dict(body)))
            except Exception:
                self.requests.pop(key)
                _capacity.release()
                raise
        return {'preview': key, 'existing': False}

    def _run(self, key, body):
        try:
            with self.lock:
                self.requests[key]['state'] = 'running'
            if self.m.stop.is_set():
                raise ValueError('服务正在关闭')
            if body.pop('kind') == 'cloud':
                result = self.m.cloud.prepare(body)
            else:
                result = self.m.actions.prepare(body.get('host'), body.get('command'), body.get('task_id'), body.get('progress_limit'))
            with self.lock:
                self.requests[key].update(state='done', result=result)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else ('远端查询超时，请检查目标机及网络' if isinstance(exc, TimeoutError) else '远端预览失败，请检查 SSH 连接及迅雷状态')
            with self.lock:
                self.requests[key].update(state='error', error=message)
        finally:
            _capacity.release()

    def status(self, key):
        with self.lock:
            if key not in self.requests:
                raise ValueError('预览请求不存在，请重新预览')
            return {k: v for k, v in self.requests[key].items() if k != 'signature'}

    def join(self):
        for future in self.futures:
            future.result()
