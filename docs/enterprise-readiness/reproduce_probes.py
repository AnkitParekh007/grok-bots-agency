"""Collect narrow synthetic readiness observations; never run against organization services.

Run from any directory with the repository's Python environment. No external API,
real model, installation, or persistent credential is used. P03 captures executor
inputs instead of executing a test. These observations are not security PASS gates.
"""
import copy, json, os, sys, tempfile, threading, subprocess, hashlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from qa_agent import ROOT
from qa_agent.auth import UserStore
from qa_agent.server import create_app
from qa_agent.runtime import QAAgent
from qa_agent.store import Store
from qa_agent.executor import ToolContext
from qa_agent.integrations.http import ReadOnlyHttpClient, IntegrationError
import httpx

# This is an observation script, not a passing security regression suite.
# After remediation, inspect changed observations rather than preserving gaps.
baseline = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
code_files = ['qa_agent/runtime.py', 'qa_agent/server.py', 'qa_agent/executor.py', 'qa_agent/store.py', 'qa_agent/auth.py', 'qa_agent/integrations/http.py']
out = {'schema':'enterprise-assessment-probes/1','baseline':baseline,'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in code_files},'scope':'offline synthetic probes; temporary credentials, stores and loopback HTTP only; no model inference or organization access','observations':[]}
# Ignore the operator's optional SUT override within this probe process.
os.environ.pop('SYNTHETIC_SUT_URL', None)
def record(id, **kw):
    out['observations'].append(dict(id=id, **kw))

with tempfile.TemporaryDirectory(prefix='qa-readiness-') as temp:
    td=Path(temp)
    cfg=json.loads((ROOT/'config/agent.json').read_text())
    cfg['model']['provider']='replay'
    users=UserStore(td/'users.json')
    viewer=users.add('assessment_viewer','viewer')
    app=create_app(td/'app',auth_mode='local',users_file=td/'users.json',agent_cfg=cfg)
    with TestClient(app) as c:
        h={'Authorization':'Bearer '+viewer}
        record('P01', finding='ER-03', projects_status=c.get('/api/projects',headers=h).status_code, story_status=c.get('/api/projects/PCC/stories/PCC-DEMO-1',headers=h).status_code, explanation='A viewer without project memberships can read every configured project; fixture story only.')
        r=c.post('/api/login',json={'token':viewer})
        cookie=r.headers.get('set-cookie','')
        c.post('/api/logout')
        record('P02',finding='ER-02',httponly='httponly' in cookie.lower(),same_site_strict='samesite=strict' in cookie.lower(),secure_attribute='; secure' in cookie.lower(), bearer_after_logout_status=c.get('/api/projects',headers=h).status_code,explanation='Logout clears the browser cookie; long-lived bearer remains valid. Cookie/token values omitted.')
    a=QAAgent(td/'drift',agent_cfg=copy.deepcopy(cfg))
    od=td/'oracles'; od.mkdir()
    original=json.loads((ROOT/'fixtures/oracles/PCC-DEMO-1.expected.json').read_text())
    op=od/'PCC-DEMO-1.expected.json'; op.write_text(json.dumps(original))
    a.cfg['oracles_dir']=str(od)
    rid=a.start_run('Read the synthetic supplier import story',story_key='PCC-DEMO-1',wait=True)
    run=a.store.get_run(rid)
    assert run['status']=='awaiting_approval',run
    oid=run['plan_json']['test_cases'][0]['oracle_ref']
    old=copy.deepcopy(original['oracles'][oid]['expected'])
    a.approvals.record(rid,run['plan_hash'],'synthetic_probe_reviewer','approved',comment='Offline harness fixture, not human authorization for real execution')
    original['oracles'][oid]['expected']={'assessment_changed_after_review':True}
    op.write_text(json.dumps(original))
    captured={}
    def fake_execute(executor,*args,**kw):
        captured['expected']=executor.oracles[oid]['expected']
        return []
    with patch('qa_agent.runtime.PlaywrightExecutor.run',new=fake_execute):
        a.execute(rid,wait=True)
    record('P03',finding='ER-09',test_plan_hash_unchanged=a.store.get_run(rid)['plan_hash']==run['plan_hash'],mutated_oracle_reached_executor=captured.get('expected')=={'assessment_changed_after_review':True}, actual_sut_execution=False,explanation='Executor replaced with a capture stub; demonstrates oracle change reaches execution boundary without invalidating approval, not a real test result.')
    try: a._oracles('ORG-123')
    except Exception as e: record('P04',finding='ER-05',error=str(e))
    s1=Store(td/'workers');s1.create_run('run-live-fixture','x','synthetic-local','replay','replay')
    s2=Store(td/'workers')
    record('P05',finding='ER-10',first_store_still_open=True, status_after_second_store_initialization=s1.get_run('run-live-fixture')['status'],explanation='Second Store treats another still-open instance planning row as interrupted. No OS process kill/restart performed.')
    s2.db.close();s1.db.close()
    hits=[]
    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'benign probe')
        def log_message(self,*a): pass
    sink=ThreadingHTTPServer(('127.0.0.1',0),Sink); target=f'http://127.0.0.1:{sink.server_port}'
    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/redirect':
                self.send_response(302);self.send_header('Location',target+'/redirect-target');self.end_headers()
            else:
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(f'<html><img src="{target}/subresource"><p>synthetic egress probe</p></html>'.encode())
        def log_message(self,*a): pass
    source=ThreadingHTTPServer(('127.0.0.1',0),Source)
    for server in (sink,source): threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        env={'name':'synthetic-local','base_url':f'http://127.0.0.1:{source.server_port}','status':'verified-synthetic','allowed_hosts':['127.0.0.1'],'production':False}
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                t=ToolContext(rid,'probe',page,env,run['plan_hash'],a.gateway,a.artifacts,ROOT/'fixtures/data')
                t.goto('/');page.wait_for_load_state('networkidle')
                t.goto('/redirect');page.wait_for_load_state('load')
                record('P06',finding='ER-08',target_url_allowed_by_registry=a.envs.is_url_allowed(env,target+'/subresource'),subresource_observed='/subresource' in hits,redirect_observed='/redirect-target' in hits,explanation='Actual Chromium ToolContext navigation with fixture approval; both servers are owned temporary loopback servers. Different target port is denied by registry yet received requests.')
            finally:browser.close()
    finally:
        for server in (source,sink):server.shutdown();server.server_close()
    seen=[]
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(3):seen.append(1024);yield b'x'*1024
    icfg={'enabled':True,'approved':True,'approval_ref':'offline-probe','base_url':'https://jira-probe.invalid','allowed_hosts':['jira-probe.invalid'],'projects':['PCC'],'auth':{'type':'bearer','token_env':'QA_ASSESSMENT_FAKE_TOKEN'},'max_response_bytes':1024,'max_retries':0}
    with patch.dict(os.environ,{'QA_ASSESSMENT_FAKE_TOKEN':'synthetic-not-a-real-credential'}):
        http=ReadOnlyHttpClient('probe',icfg,transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Chunks())))
        try:http.get_json('fixture')
        except IntegrationError as e:
            record('P07',finding='ER-12',configured_max_bytes=1024,bytes_consumed_before_rejection=sum(seen),response_rejected=True,proxy_environment_inheritance=http._client._trust_env,explanation='MockTransport only. Limit checked after body is buffered; integration HTTP inherits HTTPX environment proxy settings by default.')
        finally:http._client.close()
    for agent in (a,app.state.agent):agent.store.db.close()
path=ROOT/'reports/enterprise-readiness/probe-results.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
print(json.dumps(out,indent=2))
