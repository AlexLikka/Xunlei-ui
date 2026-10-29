"""Browser regression: automatic directories and asynchronous confirmation.

Requires Playwright Chromium. Set THUNDER_TEST_USERNAME and
THUNDER_TEST_PASSWORD for an existing account with configured hosts.
All nonempty execution requests are intercepted; no real task is converted.
"""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
BASE=os.environ.get('THUNDER_TEST_URL','http://127.0.0.1:8765')
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 ctx=browser.new_context(viewport={'width':1440,'height':1000})
 assert ctx.request.post(BASE+'/api/auth/login',data={'username':os.environ['THUNDER_TEST_USERNAME'],'password':os.environ['THUNDER_TEST_PASSWORD']}).ok
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 if os.environ.get('THUNDER_TEST_SOURCE'):
  source=Path(os.environ['THUNDER_TEST_SOURCE'])
  page.route('**/app.js',lambda r:r.fulfill(response=r.fetch(),body=(source/'web/app.js').read_text()))
  page.route(BASE+'/',lambda r:r.fulfill(response=r.fetch(),body=(source/'web/index.html').read_text()))
 page.goto(BASE+'/');expect(page.locator('#summary')).to_contain_text('台主机')
 print('PASS: actual Chromium loads dashboard; machines:',page.locator('#sidebar-count').inner_text(),flush=True)
 page.click('#accelerate')
 assert not page.locator('#cloud-advanced').evaluate('(e)=>e.open')
 assert not page.locator('#cloud-folder-label').is_visible()
 assert page.locator('#cloud-output').input_value()==''
 print('PASS: automatic paths default; no cloud folder ID requested',flush=True)
 # Deterministic preview for submission feedback testing. Never migrate a user task.
 tickets={}
 def preview(r):
  body=r.request.post_data_json;h=body['host'];key='browser-only-'+h
  result={'host':h,'plan':key,'label':'启动全部未完成','files':False,'expires_seconds':900,'count':1,'tasks':[{'id':9000000000000,'name':'browser fixture','save_path':'C:\\Temp','path':'C:\\Temp'}],'resources':[]}
  tickets[key]=result;r.fulfill(status=200,content_type='application/json',body=json.dumps({'preview':key}))
 def preview_status(r):
  key=r.request.post_data_json['preview'];r.fulfill(status=200,content_type='application/json',body=json.dumps({'state':'done','result':tickets[key]}))
 page.route('**/api/previews/start',preview)
 page.route('**/api/previews/status',preview_status)
 page.click('#cloud-preview');expect(page.locator('#cloud-submit')).to_be_enabled()
 captured=[]
 def submit(r):
  captured.append(r.request.post_data_json)
  r.fulfill(status=200,content_type='application/json',body=json.dumps({'run':'browser-test-only'}))
 page.route('**/api/fleet/accelerate',submit)
 # Hold the following dashboard refresh indefinitely: it must not block unlocking.
 held=[]
 page.route('**/api/status',lambda r:held.append(r))
 page.click('#cloud-submit')
 expect(page.locator('#cloud-error')).to_contain_text('服务已受理',timeout=3000);expect(page.locator('#cloud-preview')).to_be_enabled()
 assert captured[0]['output']=='' and captured[0]['folder']==''
 assert not page.locator('#cloud-submit').is_enabled()
 print('PASS: confirmation acknowledges and unlocks without waiting for dashboard refresh',flush=True)
 page.click('#cloud-preview');expect(page.locator('#cloud-submit')).to_be_enabled()
 page.unroute('**/api/fleet/accelerate')
 page.route('**/api/fleet/accelerate',lambda r:r.fulfill(status=400,content_type='application/json',body=json.dumps({'error':'预览已过期，请重新预览'},ensure_ascii=False)))
 page.click('#cloud-submit');expect(page.locator('#cloud-error')).to_contain_text('预览已过期');expect(page.locator('#cloud-preview')).to_be_enabled()
 print('PASS: server errors shown and controls unlocked',flush=True)
 page.route('**/api/test-hang',lambda r:held.append(r))
 result=page.evaluate("async()=>{try{await post('/api/test-hang',{}, {timeoutMs:150});return 'missing timeout'}catch(e){return e.message}}")
 assert '请求超时' in result,result
 print('PASS: stalled request reports timeout',flush=True)
 # Manual start uses the same asynchronous preview and immediate acknowledgment.
 page.click('#cloud-close');page.click('#tab-tasks');page.select_option('#host',index=1)
 page.click('[data-command="start_all"]')
 expect(page.locator('#preview')).to_be_visible()
 expect(page.locator('#preview-note')).to_contain_text('15 分钟')
 expect(page.locator('#preview-note')).to_contain_text('已受理的任务不受此时效限制')
 page.route('**/api/actions/execute',lambda r:r.fulfill(status=200,content_type='application/json',body=json.dumps({'job':'browser-manual-only'})))
 manual_result=dict(host='pc-101',command='start_all',state='running',phase='executing',processed=8,total=346,created_at=0,results=[])
 page.route('**/api/actions/status',lambda r:r.fulfill(status=200,content_type='application/json',body=json.dumps(manual_result)))
 page.click('#execute');expect(page.locator('#preview')).not_to_be_visible()
 expect(page.locator('#job')).to_contain_text('8/346')
 print('PASS: manual start accepts without waiting for 346 tasks and displays batch progress',flush=True)
 assert not errors,errors
 print('PASS: no browser JavaScript errors; all acceleration execution requests intercepted',flush=True)
 for route in held:
  try:route.abort()
  except Exception:pass  # The browser may already have aborted the timeout probe.
 page.wait_for_timeout(100)
 page.unroute_all(behavior='ignoreErrors')
 session=ctx.request.get(BASE+'/api/session').json()
 ctx.request.post(BASE+'/api/auth/logout',data={},headers={'X-Monitor-Token':session['token']})
 ctx.close()
 browser.close()
