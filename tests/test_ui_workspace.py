"""Real Chromium UI contracts against loopback synthetic/replay servers; no live inference."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import httpx
import pytest
from playwright.sync_api import sync_playwright, expect
from qa_agent import ROOT
from qa_agent.auth import UserStore
from qa_agent.util import load_json
from tests.b1_helpers import free_port
from tests.conftest import sut

pytestmark = [pytest.mark.integration, pytest.mark.approval, pytest.mark.execution, pytest.mark.security]
SHOTS = Path(os.environ.get('QA_UI_SCREENSHOTS_DIR', ROOT / 'reports/ui-screenshots'))


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    directory = tmp_path_factory.mktemp('ui-workspace')
    users = UserStore(directory / 'users.json')
    tokens = {role: users.add('ui_' + role, role) for role in ('requester', 'approver', 'viewer')}
    cfg = load_json(ROOT / 'config/agent.json')
    cfg['model']['provider'] = 'replay'
    cfg['auth'].update(mode='local', forbid_self_approval=True)
    (directory / 'agent.json').write_text(json.dumps(cfg))
    port = free_port()
    unavailable_port = free_port()
    while unavailable_port == port:
        unavailable_port = free_port()
    cfg['providers']['ollama'].update(base_url=f'http://127.0.0.1:{unavailable_port}', timeout_s=2, max_retries=0)
    (directory / 'agent.json').write_text(json.dumps(cfg))
    base = f'http://127.0.0.1:{port}'
    with sut('all') as sut_url:
        env = dict(os.environ, QA_MODEL_PROVIDER='replay', QA_AUTH_MODE='local', QA_AGENT_PORT=str(port),
                   QA_AGENT_CONFIG=str(directory / 'agent.json'), QA_USERS_FILE=str(users.path),
                   QA_DATA_DIR=str(directory / 'data'), SYNTHETIC_SUT_URL=sut_url)
        proc = subprocess.Popen([sys.executable, '-m', 'qa_agent.server'], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            for _ in range(120):
                try:
                    if httpx.get(base + '/api/me').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError('workspace did not start')
            yield base, tokens
        finally:
            proc.terminate(); proc.wait(15)


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser, workspace):
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='light',
                                  permissions=['clipboard-read', 'clipboard-write'])
    # Browser requests are also restricted to local synthetic infrastructure.
    def network(route):
        from urllib.parse import urlparse
        if urlparse(route.request.url).hostname not in ('127.0.0.1', 'localhost'):
            route.abort()
        else:
            route.continue_()
    context.route('**/*', network)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(workspace[0])
    yield page
    context.close()
    assert not errors, errors


def login(page, workspace, role='requester'):
    expect(page.locator('#login-dialog')).to_be_visible()
    page.locator('#tok').fill(workspace[1][role])
    page.locator('#login').click()
    expect(page.locator('#login-dialog')).not_to_be_visible()
    expect(page.locator('#composer-model')).to_contain_text('Replay')
    assert 'qa_session' not in page.evaluate('document.cookie')
    assert not page.evaluate('Object.keys(localStorage).some(k=>/token|secret/i.test(k))')


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / (name + '.png')), animations='disabled')


def geometry(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'page horizontal overflow'
    box = page.locator('#composer').bounding_box()
    assert box and box['x'] >= 0 and box['x'] + box['width'] <= page.viewport_size['width']
    assert box['y'] + box['height'] <= page.viewport_size['height']
    if page.viewport_size['width'] >= 1440:
        center = page.locator('#main').bounding_box()
        right = page.locator('#context-panel').bounding_box()
        assert center['x'] + center['width'] <= right['x'] + 1


def request_run(workspace, message='Generate tests for PCC-DEMO-1'):
    response = httpx.post(workspace[0] + '/api/chat', headers={'Authorization': 'Bearer ' + workspace[1]['requester']},
                          json={'message': message, 'story_key': 'PCC-DEMO-1', 'environment': 'synthetic-local',
                                'model_provider': 'replay', 'model_id': 'replay'})
    response.raise_for_status()
    rid = response.json()['run_id']
    for _ in range(150):
        r = httpx.get(workspace[0] + '/api/runs/' + rid, headers={'Authorization': 'Bearer ' + workspace[1]['requester']}).json()
        if r['status'] == 'awaiting_approval':
            return rid
        time.sleep(.1)
    raise AssertionError(r)


def open_run(page, rid):
    page.get_by_role('button', name='Sessions', exact=True).click()
    page.locator('#page-view [data-action="open-run"][data-run="' + rid + '"]').click()
    expect(page.locator('#status')).to_contain_text('Awaiting approval', timeout=30000)


def contrast(page):
    colors = page.evaluate('Object.fromEntries(["text","bg","muted","accent","accent-soft","success","success-bg","danger","danger-bg","warning","warning-bg"].map(k=>[k,getComputedStyle(document.documentElement).getPropertyValue("--"+k).trim()]))')
    def lum(h):
        rgb = [int(h[i:i+2], 16)/255 for i in (1, 3, 5)]
        rgb = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
        return sum(v*w for v,w in zip(rgb, (.2126,.7152,.0722)))
    for a,b in [('text','bg'),('muted','bg'),('accent','accent-soft'),('success','success-bg'),('danger','danger-bg'),('warning','warning-bg')]:
        hi,lo=sorted([lum(colors[a]),lum(colors[b])],reverse=True)
        assert (hi+.05)/(lo+.05) >= 4.5, (a,b)


def test_layout_themes_keyboard_and_first_run(page, workspace):
    shot(page, 'login')
    page.locator('#tok').fill('invalid-token')
    page.locator('#login').click()
    expect(page.locator('#login-error')).to_contain_text('invalid token')
    login(page, workspace)
    expect(page.get_by_role('heading', name='From story to evidence.')).to_be_visible()
    expect(page.get_by_role('button', name='Run Approved Browser Tests')).to_be_disabled()
    contrast(page)
    expect(page.locator('#story option:checked')).to_have_text('Identify from message')
    expect(page.locator('#repo option:checked')).to_have_text('No repository selected')
    page.get_by_role('button',name='Close context panel').click()
    expect(page.locator('#context-toggle')).to_be_focused()
    page.locator('#context-toggle').click()
    for w,h in [(1920,1080),(1440,900),(1366,768),(1024,768),(390,844)]:
        page.set_viewport_size({'width':w,'height':h})
        geometry(page)
        shot(page, f'initial-light-{w}x{h}')
    page.locator('#context-toggle').click()
    expect(page.locator('#context-panel')).to_have_attribute('aria-modal','true')
    expect(page.get_by_role('button',name='Close context panel')).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(page.locator('#model-choice')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#context-toggle')).to_be_focused()
    expect(page.locator('#context-panel')).not_to_be_visible()
    page.get_by_role('button', name='Open navigation').click()
    expect(page.locator('#sidebar')).to_have_attribute('aria-modal','true')
    page.keyboard.press('Escape')
    expect(page.get_by_role('button', name='Open navigation')).to_be_focused()
    page.set_viewport_size({'width':1440,'height':900})
    page.get_by_role('button',name='Switch theme',exact=True).click()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    contrast(page); shot(page,'initial-dark-1440x900')
    page.reload();expect(page.locator('html')).to_have_attribute('data-theme','dark')
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('#context-toggle').evaluate('e=>getComputedStyle(e).transitionDuration') == '0s'
    page.get_by_role('button',name='Switch theme',exact=True).click()
    page.locator('#tab-context').focus();page.keyboard.press('ArrowRight')
    expect(page.locator('#tab-plan')).to_be_focused()
    expect(page.locator('#tab-plan')).to_have_attribute('aria-selected','true')
    page.keyboard.press('Home');expect(page.locator('#tab-context')).to_be_focused()


def test_authenticated_full_workflow_and_artifacts(page, workspace):
    login(page,workspace)
    page.locator('#proj').select_option('PCC')
    page.locator('#story').select_option('PCC-DEMO-1')
    page.locator('#repo').select_option('SYN/pim-import-service')
    page.locator('#env').select_option('synthetic-local')
    page.get_by_role('button',name='Models',exact=True).click()
    expect(page.locator('#model-settings')).to_be_visible();shot(page,'models')
    nim=page.locator('.model-card').filter(has=page.get_by_role('heading',name='NVIDIA NIM',exact=True))
    expect(nim).to_contain_text('Unsupported / deferred')
    expect(nim.get_by_role('button',name='Use for new run')).to_be_disabled()
    replay=page.locator('.model-card').filter(has=page.get_by_role('heading',name='Replay',exact=True))
    with page.expect_response(lambda r:'/api/models/replay/probe' in r.url):
        replay.get_by_role('button',name='Check connection').click()
    ollama=page.locator('.model-card').filter(has=page.get_by_role('heading',name='Ollama',exact=True))
    ollama.get_by_role('button',name='Use for new run').click()
    ollama.get_by_role('button',name='Check connection').click()
    expect(page.locator('#context-model')).to_contain_text('Unavailable',timeout=30000)
    expect(page.locator('#model-choice option:checked')).to_contain_text('Ollama')
    expect(page.locator('#send')).to_be_disabled()
    replay.get_by_role('button',name='Use for new run').click()
    page.get_by_role('button',name='New QA Session',exact=True).click()
    prompt='Read the synthetic supplier import story, generate test cases, and wait for approval.'
    page.locator('#msg').fill(prompt)
    with page.expect_response(lambda r:r.url.endswith('/api/chat') and r.request.method=='POST') as response:
        page.locator('#send').click()
    rid=response.value.json()['run_id']
    expect(page.locator('#status')).to_contain_text('Awaiting approval',timeout=30000)
    shot(page,'conversation-awaiting-approval')
    page.locator('#chat [data-action="review"]').click()
    expect(page.locator('#review-hash')).to_have_text(__import__('re').compile(r'^[a-f0-9]{64}$'))
    expect(page.locator('#approve')).to_be_disabled()
    expect(page.locator('#review-error')).to_contain_text('requester cannot approve')
    page.keyboard.press('Escape')
    page.get_by_role('button',name='Sign out',exact=True).click()
    expect(page.locator('#chat')).not_to_contain_text(prompt)
    login(page,workspace,'approver')
    open_run(page,rid)
    page.locator('#tab-plan').click();shot(page,'test-plan')
    page.locator('#chat [data-action="review"]').click()
    expect(page.get_by_role('heading',name='Acceptance criteria & source quotations')).to_be_visible()
    plan_hash=page.locator('#review-hash').inner_text()
    shot(page,'approval-review')
    page.set_viewport_size({'width':390,'height':844})
    expect(page.locator('#approve')).to_be_visible()
    expect(page.locator('#review-hash')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    shot(page,'approval-mobile')
    page.set_viewport_size({'width':1440,'height':900})
    page.locator('#review-comment').fill('Synthetic UI regression review')
    with page.expect_response(lambda r:r.url.endswith('/approval') and r.request.method=='POST') as approval:
        page.locator('#approve').click()
    assert approval.value.request.post_data_json['plan_hash']==plan_hash
    assert approval.value.request.post_data_json['comment']=='Synthetic UI regression review'
    expect(page.locator('#status')).to_contain_text('Executing',timeout=30000)
    shot(page,'execution-running')
    page.locator('#chat .tool-activity summary').focus()
    expect(page.locator('#status')).to_contain_text('Completed',timeout=240000)
    expect(page.locator('#chat .tool-activity summary')).to_be_focused()
    expect(page.locator('#chat')).to_contain_text('3 passed · 5 failed')
    page.locator('#chat [data-action="review"]').click()
    expect(page.locator('#review-dialog .approval-record')).to_contain_text('ui_approver')
    expect(page.locator('#review-dialog .approval-record')).to_contain_text('Synthetic UI regression review')
    expect(page.locator('#approve')).to_be_disabled()
    page.keyboard.press('Escape')
    page.locator('#chat [data-action="evidence"]').click()
    expect(page.locator('#results .test-result')).to_have_count(8)
    expect(page.locator('#links')).to_be_visible();shot(page,'completed-evidence')
    page.locator('[data-action="screenshot"]').first.click()
    expect(page.locator('#evidence-dialog')).to_be_visible()
    page.wait_for_function('document.querySelector("#evidence-image").naturalWidth > 0')
    shot(page,'screenshot-viewer');page.keyboard.press('Escape')
    with page.expect_download() as download:
        page.get_by_role('link',name='Download trace').first.click()
    assert Path(download.value.path()).read_bytes()[:2]==b'PK'
    with page.expect_download() as download:
        page.get_by_role('link',name='JSON report').click()
    assert json.loads(Path(download.value.path()).read_text())['summary']['total']==8
    with page.expect_popup() as report:
        page.get_by_role('link',name='HTML report').click()
    report.value.wait_for_load_state();expect(report.value.locator('body')).to_contain_text('REPLAYED');report.value.close()
    page.get_by_role('button',name='Show steps').first.click()
    expect(page.locator('[id^="steps-"] pre').first).not_to_be_empty()
    page.get_by_role('button',name='Reports',exact=True).click()
    expect(page.get_by_role('button',name='Open evidence')).to_be_visible()
    page.get_by_role('button',name='Settings',exact=True).click()
    expect(page.get_by_role('heading',name='Budget & registered skills')).to_be_visible()


def test_rejection_cancellation_permissions_and_error_recovery(page,workspace):
    rid=request_run(workspace)
    login(page,workspace,'approver');open_run(page,rid)
    page.locator('#chat [data-action="review"]').click()
    page.locator('#review-comment').fill('Reject this synthetic candidate')
    page.locator('#reject').click();expect(page.locator('#status')).to_contain_text('Rejected')
    page.get_by_role('button',name='Sign out',exact=True).click();login(page,workspace)
    page.get_by_role('button',name='New QA Session',exact=True).click()
    page.locator('#msg').fill('Generate tests for PCC-DEMO-1')
    page.locator('#send').click();expect(page.locator('#status')).to_contain_text('Awaiting approval',timeout=30000)
    page.locator('#chat [data-action="cancel"]').click();expect(page.locator('#status')).to_contain_text('Cancelled')
    page.get_by_role('button',name='New QA Session',exact=True).click()
    page.locator('#msg').fill('Generate tests for PCC-DEMO-999')
    page.locator('#send').click();expect(page.locator('#status')).to_contain_text('Failed',timeout=30000)
    expect(page.locator('#chat .callout')).to_be_visible();shot(page,'failed-run')
    page.locator('#chat [data-action="retry"]').click()
    expect(page.locator('#msg')).to_have_value('Generate tests for PCC-DEMO-999')
    page.get_by_role('button',name='Sign out',exact=True).click();login(page,workspace,'viewer')
    expect(page.locator('#send')).to_be_disabled();expect(page.locator('#msg')).to_be_disabled()
    # Viewer cannot probe or start work; listing preserves backend ownership filtering.
    page.get_by_role('button',name='Models',exact=True).click()
    assert all(button.is_disabled() for button in page.get_by_role('button',name='Check connection').all())
    page.get_by_role('button',name='Sign out',exact=True).click();login(page,workspace)
    # Simulated HTTP failure tests recovery UI, not inference or screenshot evidence.
    page.route('**/api/models',lambda route:route.fulfill(status=503,json={'detail':'Synthetic registry unavailable'}))
    page.get_by_role('button',name='Models',exact=True).click()
    page.get_by_role('button',name='Refresh status',exact=True).click()
    expect(page.locator('#notice')).to_contain_text('Synthetic registry unavailable')
    page.unroute('**/api/models')
    page.get_by_role('button',name='Refresh workspace').click()
    expect(page.locator('#notice')).not_to_be_visible()


def test_safe_markdown_and_copy(page,workspace):
    login(page,workspace)
    page.locator('#msg').fill('PCC-DEMO-1 **Review first.**\n\n```text\nsynthetic only\n```\n<img src=x onerror=alert(1)>')
    page.locator('#send').click()
    expect(page.locator('#status')).to_contain_text('Awaiting approval',timeout=30000)
    expect(page.locator('.user-message img')).to_have_count(0)
    expect(page.locator('.user-message strong')).to_have_text('Review first.')
    expect(page.locator('.user-message code')).to_contain_text('synthetic only')
    page.locator('.user-message [data-action="copy-code"]').click()
    expect(page.locator('#live-status')).to_contain_text('Copied to clipboard')
