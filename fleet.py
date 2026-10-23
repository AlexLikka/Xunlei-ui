"""Fleet summaries and one-shot, task-only acceleration."""
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import threading
import time


def summarize(host, now, stale_after):
    fresh=host.get('state')=='在线' and now-host.get('checked_at',0)<=stale_after
    rows=host.get('tasks',[]) if fresh else []
    active=[t for t in rows if t.get('active') is True and t.get('status')==5]
    known=all(type(t.get('speed')) is int and t['speed']>=0 for t in active)
    return dict(id=host['id'],fresh=fresh,speed=sum(t['speed'] for t in active) if fresh and known else None,
                transmitting=sum(type(t.get('speed')) is int and t['speed']>0 for t in active),downloading=len(active),queued=sum(t.get('status')==3 for t in rows),
                zero=sum(t.get('speed')==0 for t in active),paused=sum(t.get('status')==7 for t in rows),other=sum(t.get('status')!=3 and t.get('status')!=7 and not (t.get('status')==5 and t.get('active') is True) for t in rows),tasks=len(rows))


class Fleet:
    def __init__(self, monitor):
        self.m=monitor;self.lock=threading.Lock();self.current=None;self.thread=None;self.history={}

    def sample(self, result):
        point=summarize(result,time.time(),self.m.config['poll_seconds']*3)
        with self.lock:
            series=self.history.setdefault(result['id'],[])
            series.append(dict(time=time.time(),speed=point['speed']))
            self.history[result['id']]=series[-240:]

    def snapshot(self, hosts):
        now=time.time();rows=[summarize(h,now,max(60,self.m.config['poll_seconds']*3)) for h in hosts]
        with self.lock:
            return dict(machines=rows,history=json.loads(json.dumps(self.history)),
                        acceleration=json.loads(json.dumps(self.current)),
                        speed=sum(r['speed'] or 0 for r in rows),known_speeds=sum(r['speed'] is not None for r in rows),
                        online=sum(r['fresh'] for r in rows),total=len(rows),paused=sum(r['paused'] for r in rows),other=sum(r['other'] for r in rows),tasks=sum(r['tasks'] for r in rows),
                        transmitting=sum(r['transmitting'] for r in rows),downloading=sum(r['downloading'] for r in rows),queued=sum(r['queued'] for r in rows),zero=sum(r['zero'] for r in rows))

    def start(self):
        with self.lock:
            if self.m.stop.is_set():raise ValueError('服务正在关闭')
            if self.current and self.current['state']=='running':return {'run':self.current['id'],'existing':True}
            with self.m.lock:hosts=list(self.m.config['hosts'])
            self.current=dict(id=secrets.token_hex(12),state='running',started=time.time(),
                              hosts={h['id']:dict(state='waiting',matched=0,deleted=0,skipped=0,uncertain=0) for h in hosts})
            self.thread=threading.Thread(target=self._run,args=(hosts,),daemon=True);self.thread.start()
            return {'run':self.current['id'],'existing':False}

    def update(self, host, **values):
        with self.lock:self.current['hosts'][host].update(values)

    def _host(self, host):
        name=host['id'];token=None
        try:
            if self.m.stop.is_set():self.update(name,state='stopped');return
            self.update(name,state='checking')
            plan=self.m.actions.prepare(name,'del_zero');token=plan['plan']
            self.update(name,matched=plan['count'])
            if plan['count']==0:self.update(name,state='done');return
            job=self.m.actions.execute(token,'');token=None
            self.update(name,state='running')
            while True:
                result=self.m.actions.status(job)
                outcomes=[t['outcome'] for t in result['results']]
                self.update(name,deleted=outcomes.count('deleted'),skipped=outcomes.count('skipped')+outcomes.count('blocked'),uncertain=outcomes.count('uncertain'))
                if result['state']!='running':
                    self.update(name,state=result['state']);return
                time.sleep(.2)
        except Exception:
            self.update(name,state='error',error='连接、接口或任务操作失败；请查看该机器状态，未自动重试')
        finally:
            if token:
                with self.m.actions.lock:self.m.actions.plans.pop(token,None)

    def _run(self,hosts):
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(self._host,hosts))
        finally:
            with self.lock:self.current['state']='finished';self.current['finished']=time.time()

    def join(self):
        if self.thread:self.thread.join()
