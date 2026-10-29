"""Offline Chromium check for recycle-transfer cleanup disclosures and results."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2];BASE='http://127.0.0.1:8765'
expect.set_options(timeout=15000)
seen=[]
host=dict(id='fixture',name='测试机器',address='10.0.0.1',port=22,state='在线',checked_at=0,tasks=[],archives={},auto_enabled=False,idle_seconds=600)
cloud=dict(hosts=dict(fixture=dict(state='ready',requests=[dict(id='request-fixture',stage='cacheOnly',jobIds=['1','2'],cacheOnly=True,failed=0,cleanupRecycle=True,cleanupStage='waiting',cleanupCleaned=1,cleanupPending=1,cleanupReview=0)],jobs=[dict(id='1',name='成功项',stage='caching',recycle_cleanup='cleaned',downloads=[]),dict(id='2',name='等待项',stage='caching',recycle_cleanup='waiting',downloads=[])])),jobs=[])
state=dict(hosts=[host],mode='observe',poll_seconds=15,idle_seconds=600,events=[],manual_jobs=[],cloud=cloud,
           fleet=dict(machines=[dict(id='fixture',fresh=True,speed=0,transmitting=0,downloading=0,queued=0,zero=0,paused=0,tasks=0)],history={},acceleration=None,known_speeds=1,total=1,online=1,speed=0,downloading=0,queued=0,zero=0,transmitting=0,tasks=0))
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def api(route):
  path=route.request.url.removeprefix(BASE);body=route.request.post_data_json if route.request.method=='POST' else {}
  if path=='/api/session':result=dict(token='fixture-token',username='fixture')
  elif path=='/api/status':result=state
  elif path=='/api/previews/start':
   seen.append(body);result=dict(preview='ticket')
  elif path=='/api/previews/status':result=dict(state='done',result=dict(plan='plan-fixture',host='fixture',command='recycle',cleanup_recycle=True,count=1,tasks=[dict(id=1,name='种子',save_path='C:\fixture')],resources=[],expires_seconds=900))
  elif path=='/api/cloud/execute':seen.append(body);result=dict(job='job-fixture')
  else:raise AssertionError('Unexpected API: '+path)
  route.fulfill(content_type='application/json',body=json.dumps(result,ensure_ascii=False))
 page.route(BASE+'/api/**',api)
 page.route(BASE+'/',lambda r:r.fulfill(content_type='text/html',body=(ROOT/'web/index.html').read_text()))
 page.route(BASE+'/app.js',lambda r:r.fulfill(content_type='application/javascript',body=(ROOT/'web/app.js').read_text()))
 page.route(BASE+'/style.css',lambda r:r.fulfill(content_type='text/css',body=(ROOT/'web/style.css').read_text()))
 page.goto(BASE+'/');expect(page.locator('#sidebar-count')).to_have_text('1')
 page.click('#tab-tasks');page.select_option('#host','fixture');page.click('#cloud-recycle')
 expect(page.locator('#cloud-description')).to_contain_text('逐项确认云盘缓存完成后清理对应回收站记录')
 expect(page.locator('#cloud-description')).to_contain_text('保留本地文件')
 expect(page.locator('#cloud-description')).to_contain_text('失败或结果不明的条目保留')
 page.check('#cloud-cache-only');page.click('#cloud-preview')
 expect(page.locator('#cloud-submit')).to_be_enabled()
 assert seen[0]['command']=='recycle' and seen[0]['cache_only'] is True and seen[0]['cleanup_recycle'] is True
 page.click('#cloud-submit');expect(page.locator('#cloud-error')).to_contain_text('服务已受理')
 assert seen[-1]==dict(plan='plan-fixture')
 expect(page.locator('#cloud-status-view')).to_contain_text('已清理回收站记录')
 expect(page.locator('#cloud-status-view')).to_contain_text('等待缓存完成')
 expect(page.locator('#cloud-status-view')).to_contain_text('已清理 1 · 等待 1')
 assert not errors,errors
 print('PASS: recycle confirmation explains cleanup and retained failures; cache-only preview and single-use confirmation; per-item cleanup status; no real execution or JavaScript errors',flush=True)
 browser.close()
