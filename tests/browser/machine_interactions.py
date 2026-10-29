"""Machine editor/deletion UI with isolated API fixtures; no real machines changed."""
import copy
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

expect.set_options(timeout=15000)
ROOT=Path(__file__).resolve().parents[2]
BASE='http://127.0.0.1:8765'
hosts=[dict(id='one',name='一号机器',address='10.0.0.1',port=22,state='在线',checked_at=0,tasks=[],archives={},auto_enabled=False,idle_seconds=600),
       dict(id='two',name='two',address='10.0.0.2',port=22,state='在线',checked_at=0,tasks=[],archives={},auto_enabled=False,idle_seconds=600)]
changes=[]
def snapshot():
    rows=[dict(id=h['id'],fresh=True,speed=0,transmitting=0,downloading=0,queued=0,zero=0,paused=0,tasks=0) for h in hosts]
    return dict(hosts=hosts,mode='observe',poll_seconds=15,idle_seconds=600,events=[],manual_jobs=[],cloud=dict(hosts={},jobs=[]),
                fleet=dict(machines=rows,history={},acceleration=None,known_speeds=len(hosts),total=len(hosts),online=len(hosts),speed=0,downloading=0,queued=0,zero=0,transmitting=0,tasks=0))

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=r.request.url.removeprefix(BASE)
        body=r.request.post_data_json if r.request.method=='POST' else {}
        status=200
        if path=='/api/session':result=dict(token='fixture',username='test')
        elif path=='/api/status':result=snapshot()
        elif path=='/api/hosts/probe':
            changes.append((path,body));result=dict(probe='fixture-probe',address=body['address'],port=body['port'],fingerprint='SHA256:fixture',known=False)
        elif path=='/api/hosts/update':
            changes.append((path,body));h=next(h for h in hosts if h['id']==body['host']);h.update({k:body[k] for k in ('name','address','port')});result=dict(h,saved=True)
        elif path=='/api/hosts/delete':
            changes.append((path,body));h=next(h for h in hosts if h['id']==body['host'])
            if body['confirmation']!=h['name']:status=400;result=dict(error='请输入机器名称确认删除')
            else:hosts.remove(h);result=dict(deleted=True,id=h['id'])
        else:raise AssertionError('Unexpected API: '+path)
        r.fulfill(status=status,content_type='application/json',body=json.dumps(copy.deepcopy(result),ensure_ascii=False))
    page.route(BASE+'/api/**',route)
    page.route(BASE+'/',lambda r:r.fulfill(content_type='text/html',body=(ROOT/'web/index.html').read_text()))
    def static_file(name,typ):
        def serve(r):r.fulfill(content_type=typ,body=(ROOT/'web'/name).read_text())
        return serve
    for name,typ in [('app.js','application/javascript'),('style.css','text/css')]:
        page.route(BASE+'/'+name,static_file(name,typ))
    page.goto(BASE+'/');expect(page.locator('#sidebar-count')).to_have_text('2')
    tile=page.locator('.machine-tile').first
    tile.get_by_role('button',name='编辑机器',exact=True).click()
    expect(page.locator('#machine-id')).to_have_value('一号机器')
    expect(page.locator('#machine-address')).to_have_value('10.0.0.1')
    expect(page.locator('#new-credentials')).not_to_be_visible()
    page.fill('#machine-id','家庭下载机');expect(page.locator('#machine-save')).to_be_enabled();page.click('#machine-save')
    expect(page.locator('#machine-dialog')).not_to_be_visible()
    expect(page.locator('#sidebar-machines')).to_contain_text('家庭下载机')
    assert changes[-1][1]['host']=='one' and not any(p=='/api/hosts/probe' for p,_ in changes)
    print('PASS: rename without remote probe; stable ID and refreshed labels',flush=True)
    page.locator('.machine-tile').first.get_by_role('button',name='编辑机器',exact=True).click()
    page.fill('#machine-address','10.0.0.9');page.fill('#machine-port','2222')
    expect(page.locator('#machine-save')).to_be_disabled()
    page.click('#machine-probe');expect(page.locator('#machine-fingerprint')).to_contain_text('SHA256:fixture')
    expect(page.locator('#machine-save')).to_be_disabled()
    page.check('#machine-trust');expect(page.locator('#machine-save')).to_be_enabled()
    page.fill('#machine-address','10.0.0.8');expect(page.locator('#machine-save')).to_be_disabled()
    page.click('#machine-probe');page.check('#machine-trust');page.click('#machine-save')
    expect(page.locator('#machine-dialog')).not_to_be_visible()
    assert changes[-1][1]['host']=='one' and changes[-1][1]['address']=='10.0.0.8' and changes[-1][1]['port']==2222
    print('PASS: address/port change requires fresh fingerprint confirmation',flush=True)
    page.locator('.machine-tile').first.get_by_role('button',name='删除机器',exact=True).click()
    page.fill('#machine-delete-confirm','wrong');page.click('#machine-delete-submit')
    expect(page.locator('#machine-delete-error')).to_contain_text('请输入机器名称')
    page.fill('#machine-delete-confirm','家庭下载机');page.click('#machine-delete-submit')
    expect(page.locator('#machine-delete-dialog')).not_to_be_visible();expect(page.locator('#sidebar-count')).to_have_text('1')
    assert hosts[0]['id']=='two'
    print('PASS: name confirmation, error recovery, and deletion refresh',flush=True)
    page.click('#view-table');page.locator('#machine-table').get_by_role('button',name='编辑机器',exact=True).click()
    expect(page.locator('#machine-id')).to_have_value('two');page.click('#machine-cancel')
    page.click('#tab-tasks');page.select_option('#host','two');page.click('#edit-machine')
    expect(page.locator('#machine-id')).to_have_value('two');page.click('#machine-cancel')
    page.click('#add-machine');expect(page.locator('#machine-title')).to_have_text('添加机器')
    expect(page.locator('#machine-address')).to_have_value('');expect(page.locator('#machine-port')).to_have_value('22');expect(page.locator('#machine-save')).to_be_disabled();page.click('#machine-cancel')
    page.click('#delete-machine');page.fill('#machine-delete-confirm','two');page.click('#machine-delete-submit')
    expect(page.locator('#sidebar-count')).to_have_text('0');expect(page.locator('#edit-machine')).to_be_disabled();expect(page.locator('#delete-machine')).to_be_disabled()
    page.click('#tab-dashboard');expect(page.locator('#empty-workspace')).to_be_visible()
    assert not errors,errors
    print('PASS: card/table/task controls, add form reset, empty workspace; no JavaScript errors',flush=True)
    browser.close()
