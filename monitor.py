"""Central Thunder live monitor with guarded task-only cleanup."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
from pathlib import Path
import threading
import time
import secrets
import signal
import webbrowser
import os

import paramiko
from local_control import Instance, stop_instance
from thunder_api import read_live_tasks, read_task_archive, remove_keep_files
from live_tracker import LiveTracker
from manual_actions import ManualActions
from machine_settings import MachineSettings
from fleet import Fleet
from cloud_transfer import CloudTransfers
from paths import DATA, initialize
from previews import Previews
from resource_limits import POLL_WORKERS
from web_auth import Auth, protected_handler

ROOT = Path(__file__).resolve().parent

def load_config(path):
    conf = json.loads(path.read_text())
    for key in ('poll_seconds', 'idle_seconds'):
        value = conf[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} 必须是正数')
    if conf['poll_seconds'] < 5:
        raise ValueError('poll_seconds 至少为 5 秒')
    if conf.get('mode', 'observe') not in ('observe', 'auto'):
        raise ValueError('mode 必须是 observe 或 auto')
    if conf['idle_seconds'] < conf['poll_seconds'] * 2:
        raise ValueError('零速时间至少为两倍采样间隔')
    hosts = conf['hosts']
    if not isinstance(hosts,list) or len({h['id'] for h in hosts}) != len(hosts):
        raise ValueError('主机 id 必须唯一')
    for h in hosts:
        for key in ('id', 'address', 'credentials_file'):
            if not isinstance(h.get(key), str) or not h[key]:
                raise ValueError(f'主机缺少 {key}')
        if 'auto_enabled' in h and type(h['auto_enabled']) is not bool:
            raise ValueError('auto_enabled 必须是布尔值')
        if 'idle_seconds' in h and (type(h['idle_seconds']) is not int or not conf['poll_seconds']*2 <= h['idle_seconds'] <= 86400):
            raise ValueError('机器零速时间无效')
    return conf


class Monitor:
    def __init__(self, config, config_path=None, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir is not None else DATA
        self.config = config
        self.lock = threading.RLock()
        self.settings = MachineSettings(self, self.data_dir, config_path or self.data_dir / "config.json")
        self.stop = threading.Event()
        self.events = []
        self.archives = {}
        self.uncertain = set()
        journal = self.data_dir / '.runtime/events.jsonl'
        if journal.exists():
            with journal.open() as lines:
                for line in lines:
                    entry = json.loads(line)
                    key = (entry['host'], entry['task_id'])
                    if entry['event'] in ('skipped_changed','manual_paused','manual_resumed','manual_skipped_changed'):
                        self.uncertain.discard(key)
                    else:
                        self.uncertain.add(key)
                    self.events = (self.events + [entry])[-100:]
        self.operation_locks = {h['id']: threading.RLock() for h in config['hosts']}
        self.actions = ManualActions(self)
        self.cloud = CloudTransfers(self)
        self.previews = Previews(self)
        self.fleet = Fleet(self)
        self.trackers = {h['id']: LiveTracker(h.get('idle_seconds',config['idle_seconds']), config['poll_seconds'] * 2) for h in config['hosts']}
        self.hosts = {h['id']: {'id': h['id'], 'address': h['address'], 'state': '等待连接', 'tasks': []} for h in config['hosts']}

    def poll_host(self, host):
        with self.operation_locks[host['id']]:
            if self.actions.host(host['id']) is not host:
                return dict(id=host['id'],state='配置已变化',tasks=[])
            result = self._poll_host(host, include_archives=False)
        # Archive reads do not mutate tasks. Release the operation lock first
        # so a submitted action need not wait for two additional full lists.
        if result['state'] == '在线':
            self.refresh_archives(host)
        return result

    def refresh_archives(self, host, force=False):
        for view in ('completed', 'recycle'):
            if self.stop.is_set() or self.actions.host(host['id']) is not host:
                return
            with self.lock:
                previous = self.archives.get(host['id'], {}).get(view, {})
            if not force and time.monotonic() - previous.get('attempted_at', -float('inf')) < 60:
                continue
            attempted_at = time.monotonic()
            try:
                tasks = read_task_archive(host, view, self.data_dir)
                entry = dict(tasks=tasks, state='ready', checked_at=time.time(), attempted_at=attempted_at)
            except Exception:
                entry = dict(previous, state='error', attempted_at=attempted_at,
                             error='归档读取失败，当前显示上次结果；请刷新或检查连接')
                entry.setdefault('tasks', [])
            with self.lock:
                if self.actions.host(host['id']) is host:
                    self.archives.setdefault(host['id'], {})[view] = entry

    def _poll_host(self, host, allow_auto=True, force_archives=False, include_archives=True):
        if self.actions.host(host['id']) is not host:
            raise ValueError('机器配置已变化，请刷新后重试')
        if self.stop.is_set():
            return self.hosts[host['id']]
        tracker = self.trackers[host['id']]
        try:
            rows = [t for t in read_live_tasks(host, self.data_dir)
                    if not t.get('read_only') and t.get('view', 'incomplete') == 'incomplete'
                    and not t.get('recycle_time', 0)]
            result = {'id': host['id'], 'address': host['address'], 'state': '在线',
                      'checked_at': time.time(), 'tasks': tracker.update(rows, time.monotonic())}
            for task in result['tasks']:
                if self.cloud.protected(host['id'], task['id']):
                    task.update(delete_eligible=False, needs_review=False, protection_reason='已有云盘转存记录，禁止重复清理')
                if (host['id'], task['id']) in self.uncertain:
                    task['delete_eligible'] = False
                    task['needs_review'] = False
                    task['protection_reason'] = '已有执行记录，禁止自动重试'
            if allow_auto and host.get('auto_enabled', self.config.get('mode', 'observe') == 'auto') and not self.stop.is_set():
                for task in result['tasks']:
                    key = (host['id'], task['id'])
                    if not task['delete_eligible'] or key in self.uncertain:
                        continue
                    # At most one removal per host per poll; let the queue settle.
                    if self.stop.is_set():
                        break
                    self.uncertain.add(key)
                    # Persist before sending: a crash/timeout must not replay a mutation.
                    self.record(host['id'], task['id'], 'attempt_started')
                    try:
                        outcome = remove_keep_files(host, task, self.data_dir)
                        if outcome.get('deleted') is True and outcome.get('filesDeleted') is False:
                            event = 'removed_keep_files'
                            result['tasks'] = [t for t in result['tasks'] if t['id'] != task['id']]
                        elif outcome.get('deleted') is False and outcome.get('reason') == 'changed':
                            event = 'skipped_changed'
                            self.uncertain.discard(key)
                        else:
                            event = 'uncertain_no_retry'
                    except Exception:
                        event = 'uncertain_no_retry'
                    tracker.forget(task['id'])
                    self.record(host['id'], task['id'], event)
                    break
        except Exception as exc:
            tracker.clear()
            messages = {paramiko.AuthenticationException: 'SSH 认证失败',
                        paramiko.BadHostKeyException: 'SSH 主机密钥发生变化',
                        paramiko.SSHException: 'SSH 连接或主机密钥校验失败',
                        FileNotFoundError: '本地凭据或主机密钥文件不存在',
                        TimeoutError: '连接或查询超时'}
            msg = next((v for t, v in messages.items() if isinstance(exc, t)), '查询失败，请检查网络、SSH 凭据和迅雷接口兼容性')
            result = {'id': host['id'], 'address': host['address'], 'state': '异常',
                      'error': msg, 'checked_at': time.time(), 'tasks': []}
        self.fleet.sample(result)
        with self.lock:
            self.hosts[host['id']] = result
        if result['state'] == '在线' and include_archives:
            self.refresh_archives(host, force=force_archives)
        self.cloud.refresh_async(host['id'])
        return result

    def record(self, host, task_id, event):
        entry = dict(time=time.time(), host=host, task_id=task_id, event=event)
        with self.lock:
            path = self.data_dir / '.runtime/events.jsonl'
            path.parent.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as log:
                log.write(json.dumps(entry) + '\n')
                log.flush()
                os.fsync(log.fileno())
            self.events = (self.events + [entry])[-100:]

    def snapshot(self):
        with self.lock:
            hosts=[{**self.hosts[h['id']], 'name':h.get('name',h['id']), 'port':h.get('port',22), 'auto_enabled':h.get('auto_enabled',self.config.get('mode')=='auto'),
                    'idle_seconds':h.get('idle_seconds',self.config['idle_seconds']),
                    'archives':{view:{k:v for k,v in self.archives.get(h['id'],{}).get(view,dict(tasks=[],state='pending')).items() if k!='attempted_at'}
                                for view in ('completed','recycle')}} for h in self.config['hosts']]
            result={'mode':self.config.get('mode','observe'),'delete_supported':True,'events':list(self.events),
                    'poll_seconds':self.config['poll_seconds'],'idle_seconds':self.config['idle_seconds'],'hosts':hosts}
        result['fleet']=self.fleet.snapshot(hosts)
        result['cloud']=self.cloud.snapshot()
        result['manual_jobs']=self.actions.snapshot()
        return result

    def run(self):
        with ThreadPoolExecutor(max_workers=POLL_WORKERS) as pool:
            while not self.stop.is_set():
                started = time.monotonic()
                with self.lock:
                    hosts = list(self.config['hosts'])
                list(pool.map(self.poll_host, hosts))
                self.stop.wait(max(0, self.config['poll_seconds'] - (time.monotonic() - started)))


def handler_for(monitor, shutdown_token=None):
    class Handler(BaseHTTPRequestHandler):
        @property
        def monitor(self):
            return monitor

        def json_response(self, data, status=200):
            raw = json.dumps(data, ensure_ascii=False).encode()
            raw, encoding = self.compress_response(raw)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            if encoding:
                self.send_header('Content-Encoding', encoding)
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def compress_response(self, raw):
            # Status snapshots can contain thousands of task rows. Compress
            # only when the client advertises support; old API clients and
            # the unit-test request shim continue to receive plain JSON.
            supported = False
            for item in self.headers.get('Accept-Encoding', '').lower().split(','):
                parts = [part.strip() for part in item.split(';')]
                if parts[0] != 'gzip':
                    continue
                quality = next((part[2:] for part in parts[1:] if part.startswith('q=')), '1')
                try:
                    supported = float(quality) > 0
                except ValueError:
                    supported = False
                break
            if not supported or len(raw) < 1024:
                return raw, None
            return gzip.compress(raw, compresslevel=6, mtime=0), 'gzip'

        def local_request(self):
            expected = f'127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') == expected and
                    self.headers.get('Origin') in (None, 'http://' + expected) and
                    self.headers.get('Sec-Fetch-Site') not in ('cross-site',))

        def do_POST(self):
            if self.path not in ('/api/shutdown','/api/actions/preview','/api/actions/execute','/api/actions/status','/api/refresh','/api/settings/automation','/api/settings/credentials','/api/hosts/probe','/api/hosts/add','/api/hosts/update','/api/hosts/delete','/api/fleet/accelerate','/api/cloud/preview','/api/cloud/execute','/api/cloud/inspect','/api/previews/start','/api/previews/status'):
                self.send_error(404)
                return
            supplied = self.headers.get('X-Monitor-Token', '')
            if not self.local_request() or not shutdown_token or not secrets.compare_digest(supplied, shutdown_token):
                self.send_error(403)
                return
            if self.path == '/api/shutdown':
                self.monitor.stop.set()
                self.json_response({'stopping':True},202)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16384:
                    raise ValueError('请求长度无效')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):
                    raise ValueError('请求格式无效')
                if self.path == '/api/previews/start':
                    result=self.monitor.previews.start(body)
                elif self.path == '/api/previews/status':
                    result=self.monitor.previews.status(body.get('preview'))
                elif self.path == '/api/fleet/accelerate':
                    result=self.monitor.fleet.start(body)
                elif self.path == '/api/cloud/preview':
                    result=self.monitor.cloud.prepare(body)
                elif self.path == '/api/cloud/execute':
                    result=self.monitor.cloud.execute(body.get('plan'))
                elif self.path == '/api/cloud/inspect':
                    result=self.monitor.cloud.inspect(body)
                elif self.path == '/api/settings/credentials':
                    result=self.monitor.settings.credentials(body)
                elif self.path == '/api/settings/automation':
                    result=self.monitor.settings.automation(body)
                elif self.path == '/api/hosts/probe':
                    result=self.monitor.settings.probe(body)
                elif self.path == '/api/hosts/add':
                    result=self.monitor.settings.add(body)
                elif self.path == '/api/hosts/update':
                    result=self.monitor.settings.update(body)
                elif self.path == '/api/hosts/delete':
                    result=self.monitor.settings.delete(body)
                elif self.path == '/api/actions/preview':
                    result=self.monitor.actions.prepare(body.get('host'),body.get('command'),body.get('task_id'),body.get('progress_limit'))
                elif self.path == '/api/actions/execute':
                    result={'job':self.monitor.actions.execute(body.get('plan'),body.get('confirmation'))}
                elif self.path == '/api/actions/status':
                    result=self.monitor.actions.status(body.get('job'))
                else:
                    host=self.monitor.actions.host(body.get('host'))
                    if not host:raise ValueError('请选择一台主机')
                    with self.monitor.operation_locks[host['id']]:
                        result=self.monitor._poll_host(host,allow_auto=False,force_archives=True)
                self.json_response(result)
            except (ValueError,TypeError) as exc:
                self.json_response({'error':str(exc)},400)
            except Exception:
                self.json_response({'error':'远程操作失败；请检查连接或迅雷版本，刷新后核对状态'},502)

        def do_GET(self):
            if not self.local_request():
                self.send_error(403)
                return
            if self.path == '/api/session':
                self.json_response({'token': shutdown_token})
                return
            if self.path == '/api/status':
                data = json.dumps(self.monitor.snapshot(), ensure_ascii=False).encode()
                mime = 'application/json; charset=utf-8'
            elif self.path == '/style.css':
                data = (ROOT / 'web/style.css').read_bytes()
                mime = 'text/css; charset=utf-8'
            elif self.path == '/app.js':
                data = (ROOT / 'web/app.js').read_bytes()
                mime = 'text/javascript; charset=utf-8'
            elif self.path == '/':
                data = (ROOT / 'web/index.html').read_bytes()
                mime = 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            data, encoding = self.compress_response(data)
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            if encoding:
                self.send_header('Content-Encoding', encoding)
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description='迅雷多主机实时监控')
    parser.add_argument('--config', type=Path, default=DATA / 'config.json')
    parser.add_argument('--once', action='store_true', help='只读查询一次，输出数量摘要')
    parser.add_argument('--stop', action='store_true', help='关闭本目录启动的监控')
    parser.add_argument('--open-browser', action='store_true', help='启动后打开面板')
    parser.add_argument('--auto', action='store_true', help='开启连续零速自动移除（固定保留文件）')
    args = parser.parse_args()
    if args.stop:
        try:
            print(stop_instance(DATA / '.runtime'))
            return 0
        except RuntimeError as exc:
            print(str(exc))
            return 1
    initialize()
    config = load_config(args.config)
    if args.auto:
        parser.error('平台模式请登录各自账号，在网页中开启自动清理')
    if args.once:
        monitor = Monitor(config, args.config)
        monitor.config = {**config, 'mode':'observe'}
        for h in config['hosts']:h['auto_enabled']=False
        failed = False
        for host in config['hosts']:
            result = monitor.poll_host(host)
            failed |= result['state'] != '在线'
            print(json.dumps({k: v for k, v in result.items() if k != 'tasks'} | {'task_count': len(result['tasks'])}, ensure_ascii=False))
        return int(failed)
    instance = Instance(DATA / '.runtime')
    if not instance.acquire():
        print('监控已经运行，无需重复启动。双击“关闭监控.command”可停止。')
        return 1
    server = None
    workspaces = None
    try:
        from workspaces import Workspaces
        auth = Auth(DATA / '.runtime', secure=os.environ.get('THUNDER_SECURE_COOKIE') == '1')
        workspaces = Workspaces(DATA, auth, Monitor, load_config)
        bind = os.environ.get('THUNDER_BIND', '0.0.0.0')
        server = ThreadingHTTPServer((bind, int(os.environ.get('THUNDER_PORT', config.get('listen_port', 8765)))), protected_handler(handler_for(None, instance.token), auth, instance.token, workspaces))
        server.daemon_threads = True
        instance.publish(server.server_port)
        workspaces.start()
        def request_stop(signum, frame):
            workspaces.request_stop()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, request_stop)
        print(f"Thunder 平台：http://{bind}:{server.server_port}；开放注册，各账号独立空间", flush=True)
        if args.open_browser:
            webbrowser.open(f'http://127.0.0.1:{server.server_port}')
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f'无法启动监控：端口可能已被占用（{exc.__class__.__name__}）。')
        return 1
    finally:
        if workspaces:
            workspaces.request_stop()
        if server:
            server.server_close()
        if workspaces:
            workspaces.close()
        instance.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
