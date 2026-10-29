"""Independent account monitors using a bounded, shared polling pool."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from paths import initialize
from resource_limits import POLL_WORKERS


class Workspaces:
    def __init__(self, root, auth, monitor_factory=None, config_loader=None):
        if monitor_factory is None or config_loader is None:
            from monitor import Monitor, load_config
            monitor_factory = monitor_factory or Monitor
            config_loader = config_loader or load_config
        self.root = Path(root)
        self.auth = auth
        self.monitor_factory = monitor_factory
        self.config_loader = config_loader
        self.lock = threading.RLock()
        self.monitors = {}
        self.stop = threading.Event()
        self.thread = None
        for user_id in auth.user_ids():
            self.for_user(user_id)

    def for_user(self, user_id):
        with self.lock:
            if self.stop.is_set():
                raise ValueError('服务正在关闭')
            if user_id not in self.monitors:
                if not self.auth.user(user_id):
                    raise ValueError('账号不存在')
                directory = self.root / 'users' / user_id
                initialize(directory)
                path = directory / 'config.json'
                self.monitors[user_id] = self.monitor_factory(
                    self.config_loader(path), path, data_dir=directory)
            return self.monitors[user_id]

    def start(self):
        self.thread = threading.Thread(target=self.run, name='workspace-poller', daemon=True)
        self.thread.start()

    def run(self):
        pending, due = {}, {}
        offset = 0
        with ThreadPoolExecutor(max_workers=POLL_WORKERS, thread_name_prefix='host-poll') as pool:
            while not self.stop.is_set():
                pending = {k: v for k, v in pending.items() if not v.done()}
                with self.lock:
                    monitors = list(self.monitors.items())
                if monitors:
                    offset %= len(monitors)
                    monitors = monitors[offset:] + monitors[:offset]
                    offset += 1
                for user_id, monitor in monitors:
                    with monitor.lock:
                        hosts = list(monitor.config['hosts'])
                    for host in hosts:
                        # Native cloud jobs have their own status reader. Keep
                        # progress updating while a full task poll is pending.
                        monitor.cloud.refresh_async(host['id'])
                        key = (user_id, host['id'])
                        now = time.monotonic()
                        if self.stop.is_set() or len(pending) >= POLL_WORKERS:
                            break
                        if key not in pending and now >= due.get(key, 0) and host['id'] not in monitor.actions.busy and not monitor.cloud.is_busy(host['id']):
                            pending[key] = pool.submit(monitor.poll_host, host)
                            due[key] = now + monitor.config['poll_seconds']
                self.stop.wait(1)

    def request_stop(self):
        with self.lock:
            self.stop.set()
            for monitor in self.monitors.values():
                monitor.stop.set()

    def close(self):
        self.request_stop()
        if self.thread:
            self.thread.join()
        for monitor in self.monitors.values():
            monitor.fleet.join()
            monitor.actions.join()
            monitor.cloud.join()
            monitor.previews.join()
