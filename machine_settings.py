"""UI configuration and explicit first-use SSH host-key trust."""
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import threading
from contextlib import contextmanager
from pathlib import Path
import paramiko
from live_tracker import LiveTracker


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)


class MachineSettings:
    def __init__(self, monitor, root, config_path):
        self.m=monitor;self.root=root;self.path=config_path;self.pending={}

    def save(self, config):
        atomic_json(self.path,config)

    def validate_machine(self, name, address, port, excluding=None):
        if not isinstance(name,str) or not name.strip() or not re.fullmatch(r'[\w\- .]{1,64}',name):
            raise ValueError('机器名称限 1–64 个字母、数字、中文、空格、点或横线')
        try:address=str(ipaddress.ip_address(address))
        except (ValueError,TypeError):raise ValueError('请输入机器的 IP 地址')
        if type(port) is not int or not 1<=port<=65535:raise ValueError('SSH 端口无效')
        with self.m.lock:
            for h in self.m.config['hosts']:
                if h['id']==excluding:continue
                if name in (h['id'],h.get('name',h['id'])) or (h['address']==address and h.get('port',22)==port):
                    raise ValueError('机器名称或地址已存在')
        return address

    @contextmanager
    def changing(self, host_id):
        host=self.m.actions.host(host_id)
        if not host:raise ValueError('请选择本工作空间的机器')
        operation=self.m.operation_locks[host_id]
        if not operation.acquire(blocking=False):raise ValueError('机器正在读取状态或执行操作，请稍后重试')
        try:
            # Same order as command admission; never wait for a long remote job.
            with self.m.actions.lock,self.m.cloud.lock,self.m.previews.lock,self.m.lock:
                if self.m.stop.is_set():raise ValueError('服务正在关闭')
                if self.m.actions.host(host_id) is not host:raise ValueError('机器配置已变化，请刷新后重试')
                if host_id in self.m.actions.busy or host_id in self.m.cloud.busy:
                    raise ValueError('机器正在执行操作，请完成后重试')
                if any(r['host']==host_id and r['state'] in ('queued','running') for r in self.m.previews.requests.values()):
                    raise ValueError('机器正在生成预览，请完成后重试')
                yield host
        finally:operation.release()

    def invalidate(self, host_id):
        self.m.actions.plans={k:p for k,p in self.m.actions.plans.items() if p['host']['id']!=host_id}
        self.m.cloud.plans={k:p for k,p in self.m.cloud.plans.items() if p['host']!=host_id}
        for r in self.m.previews.requests.values():
            if r['host']==host_id:r.update(state='error',error='机器配置已变化，请重新预览');r.pop('result',None)
        self.pending={k:p for k,p in self.pending.items() if p.get('host')!=host_id}

    def update(self, body):
        with self.changing(body.get('host')) as host:
            name=body.get('name');port=body.get('port',22)
            address=self.validate_machine(name,body.get('address'),port,host['id'])
            changed=(address,port)!=(host['address'],host.get('port',22))
            if changed:
                p=self.pending.get(body.get('probe'))
                if not p or p['expires']<=time.monotonic() or p.get('host')!=host['id'] or (p['address'],p['port'])!=(address,port) or p.get('previous')!=(host['address'],host.get('port',22)):
                    raise ValueError('地址已变化，请重新检查 SSH 主机密钥')
                if body.get('trust') is not True:raise ValueError('请确认 SSH 主机密钥')
                keys=paramiko.HostKeys();keypath=self.root/'.runtime/known_hosts';keys.load(str(keypath))
                if p['alias'] in keys and not keys.check(p['alias'],p['key']):raise ValueError('主机密钥已变化')
                keys.add(p['alias'],p['key'].get_name(),p['key']);keys.save(str(keypath));os.chmod(keypath,0o600)
            updated={**host,'name':name,'address':address,'port':port,'revision':secrets.token_hex(12)}
            config={**self.m.config,'hosts':[updated if h is host else h for h in self.m.config['hosts']]}
            self.save(config)
            self.m.config['hosts']=config['hosts']
            self.invalidate(host['id'])
            if changed:
                self.m.trackers[host['id']].clear()
                self.m.hosts[host['id']]=dict(id=host['id'],address=address,state='等待连接',tasks=[])
                self.m.archives.pop(host['id'],None)
                self.m.cloud.host_states.pop(host['id'],None)
            return dict(saved=True,id=host['id'],name=name,address=address,port=port)

    def delete(self, body):
        with self.changing(body.get('host')) as host:
            if body.get('confirmation')!=host.get('name',host['id']):raise ValueError('请输入机器名称确认删除')
            config={**self.m.config,'hosts':[h for h in self.m.config['hosts'] if h is not host]}
            self.save(config)
            self.m.config['hosts']=config['hosts']
            self.invalidate(host['id'])
            for mapping in (self.m.hosts,self.m.trackers,self.m.archives,self.m.cloud.host_states):mapping.pop(host['id'],None)
            # Credentials may be shared by other machines. Keep credentials,
            # fingerprints and operation lock for in-flight read-only callers.
            return dict(deleted=True,id=host['id'])

    def automation(self, body):
        host=self.m.actions.host(body.get('host'))
        enabled=body.get('enabled');seconds=body.get('idle_seconds')
        if host is None or type(enabled) is not bool or type(seconds) is not int or not self.m.config['poll_seconds']*2<=seconds<=86400:
            raise ValueError('请选择机器，零速时间须为采样间隔两倍以上、最多 24 小时')
        with self.m.operation_locks[host['id']],self.m.lock:
            if self.m.stop.is_set():raise ValueError('服务正在关闭')
            if self.m.actions.host(host['id']) is not host:raise ValueError('机器配置已变化，请刷新后重试')
            config=json.loads(json.dumps(self.m.config))
            entry=next(h for h in config['hosts'] if h['id']==host['id'])
            entry.update(auto_enabled=enabled,idle_seconds=seconds)
            self.save(config)
            host.update(auto_enabled=enabled,idle_seconds=seconds)
            tracker=self.m.trackers[host['id']];tracker.threshold=seconds;tracker.clear()
            # Published counters must also reflect the reset.
            for task in self.m.hosts[host['id']]['tasks']:
                task.update(unchanged_seconds=0,delete_eligible=False,needs_review=False)
            return {'saved':True,'enabled':enabled,'idle_seconds':seconds}

    def probe(self, body):
        if self.m.stop.is_set():raise ValueError('服务正在关闭')
        editing=body.get('host');host=self.m.actions.host(editing) if editing else None
        if editing and not host:raise ValueError('请选择本工作空间的机器')
        name=body.get('name',body.get('id',''));port=body.get('port',22)
        address=self.validate_machine(name,body.get('address',''),port,editing)
        with socket.create_connection((address,port),timeout=8) as sock:
            transport=paramiko.Transport(sock)
            try:transport.start_client(timeout=8);key=transport.get_remote_server_key()
            finally:transport.close()
        alias=address if port==22 else f'[{address}]:{port}'
        known=paramiko.HostKeys();known.load(str(self.root/'.runtime/known_hosts'))
        if alias in known and not known.check(alias,key):raise ValueError('已有机器密钥发生变化，已阻止添加，请核对目标机器')
        fp='SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        token=secrets.token_urlsafe(32)
        with self.m.lock:
            self.pending={k:v for k,v in self.pending.items() if v['expires']>time.monotonic()}
            if len(self.pending)>=20:raise ValueError('待添加机器过多，请稍后重试')
            self.pending[token]=dict(id=name,address=address,port=port,key=key,alias=alias,expires=time.monotonic()+900,
                                     host=editing,previous=(host['address'],host.get('port',22)) if host else None)
        return {'probe':token,'fingerprint':fp,'known':alias in known,'address':address,'port':port}

    def add(self, body):
        with self.m.lock:
            if self.m.stop.is_set():raise ValueError('服务正在关闭')
            p=self.pending.get(body.get('probe'))
            if not p or p['expires']<=time.monotonic():raise ValueError('密钥检查已过期，请重新检查')
            if p.get('host'):raise ValueError('此密钥检查仅用于编辑机器')
            if body.get('trust') is not True:raise ValueError('请确认 SSH 主机密钥')
            self.validate_machine(p['id'],p['address'],p['port'])
            source=body.get('credentials_from')
            if source:
                existing=self.m.actions.host(source)
                if not existing:raise ValueError('凭据来源机器不存在')
                credential=existing['credentials_file']
            else:
                user=body.get('username');password=body.get('password')
                if not isinstance(user,str) or not user.strip() or not isinstance(password,str) or not password or any(c in user+password for c in '\r\n\0') or len(user)>256 or len(password)>4096:raise ValueError('请输入有效 SSH 账号和密码（不能包含换行）')
                folder=self.root/'.runtime/credentials';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
                f=folder/(secrets.token_hex(16)+'.txt');fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as out:out.write(user.strip()+'\n'+password+'\n')
                credential=str(f.relative_to(self.root))
            h=dict(id=p['id'],address=p['address'],port=p['port'],credentials_file=credential,auto_enabled=False,idle_seconds=self.m.config['idle_seconds'])
            keys=paramiko.HostKeys();keypath=self.root/'.runtime/known_hosts';keys.load(str(keypath))
            if p['alias'] in keys and not keys.check(p['alias'],p['key']):raise ValueError('主机密钥已变化')
            keys.add(p['alias'],p['key'].get_name(),p['key']);keys.save(str(keypath));os.chmod(keypath,0o600)
            config={**self.m.config,'hosts':self.m.config['hosts']+[h]};self.save(config)
            self.m.operation_locks[h['id']]=threading.RLock()
            self.m.trackers[h['id']]=LiveTracker(h['idle_seconds'],config['poll_seconds']*2)
            self.m.hosts[h['id']]={'id':h['id'],'address':h['address'],'state':'等待连接','tasks':[]}
            self.m.config['hosts']=config['hosts'];self.pending.pop(body['probe'])
        # Saved even on authentication/version failure, so diagnostics remain visible.
        result=self.m.poll_host(h)
        return {'saved':True,'id':h['id'],'state':result['state'],'error':result.get('error')}

    def credentials(self, body):
        host=self.m.actions.host(body.get('host'))
        user=body.get('username');password=body.get('password')
        if not host:raise ValueError('请选择机器')
        if not isinstance(user,str) or not user.strip() or not isinstance(password,str) or not password or len(user)>256 or len(password)>4096 or any(c in user+password for c in '\r\n\0'):
            raise ValueError('SSH 用户名或密码无效')
        with self.m.operation_locks[host['id']], self.m.lock:
            if self.m.stop.is_set():raise ValueError('服务正在关闭')
            if self.m.actions.host(host['id']) is not host:raise ValueError('机器配置已变化，请刷新后重试')
            folder=self.root/'.runtime/credentials';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            path=folder/(secrets.token_hex(16)+'.txt')
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:f.write(user.strip()+'\n'+password+'\n')
            config=json.loads(json.dumps(self.m.config))
            entry=next(h for h in config['hosts'] if h['id']==host['id'])
            entry['credentials_file']=str(path.relative_to(self.root))
            try:self.save(config)
            except Exception:path.unlink(missing_ok=True);raise
            host['credentials_file']=entry['credentials_file']
            self.m.trackers[host['id']].clear()
        return {'saved':True}
