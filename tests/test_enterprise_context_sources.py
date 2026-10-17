"""Synthetic approved context, exact provenance and adversarial citation contracts."""
import copy
import json
from types import SimpleNamespace

import pytest

from qa_agent.context_sources import collect, verify, selections
from qa_agent.providers.models import DataClassDenied
from qa_agent.planning import build_messages, validate_plan, FormatError, PolicyViolation
from qa_agent.sources import issue_bundle
from qa_agent.util import canon, sha256, register_secret_env
from tests.test_enterprise_data_policy import setup
from tests.test_b1_planning_hardening import load, SK, FILES
from tests.test_enterprise_domains import domain, approval

pytestmark = [pytest.mark.security, pytest.mark.planning, pytest.mark.provider]


@pytest.fixture
def corpus(setup):
    agent, model, context, model_calls = setup
    repo = 'SYN/pim-import-service'
    agent.store.update_run('data-run', story_key='PCC-1', repository=repo)
    state = {'file': 'Header\nThe import validates required columns.\n',
             'comment': 'Reviewed comment clarifies the template workflow.',
             'attachment': 'Synthetic context note for import validation.'}
    reads = []
    def file(repository, path, ref):
        reads.append(('file', repository, path, ref))
        return state['file']
    def comments(key):
        reads.append(('comments', key))
        return [{'id': '1', 'created': '2026-10-17T10:00:00+00:00', 'body': state['comment'], 'author': 'DO_NOT_SEND_AUTHOR'},
                {'id': '2', 'created': '2026-10-17T10:00:00+00:00', 'body': 'DO_NOT_SEND_UNSELECTED_COMMENT'}]
    def attachments(key):
        reads.append(('attachments', key))
        return [{'id': '3', 'mime_type': 'text/plain', 'size': len(state['attachment'].encode())}]
    def attachment(key, identifier):
        reads.append(('attachment', key, identifier))
        text = state['attachment']
        return {'id': identifier, 'complete': True, 'text': text, 'bytes': len(text.encode()), 'sha256': sha256(text)}
    agent.git_for = lambda project: SimpleNamespace(read_file=file)
    agent.tracker_for = lambda project: SimpleNamespace(get_comments=comments, list_attachments=attachments, get_attachment=attachment)
    rule = agent.projects['PCC']['source_policy']
    rule['contexts'] = [
        {'kind': 'repository_file', 'repository': repo, 'commit': 'a' * 40, 'file': 'docs/workflow.md', 'sha256': sha256(state['file'])},
        {'kind': 'jira_comment', 'id': '1', 'sha256': sha256(state['comment'])},
        {'kind': 'jira_attachment', 'id': '3', 'sha256': sha256(state['attachment'])}]
    def prepare(): return agent.data_policy.prepare(agent.store.get_run('data-run'), context, model)
    return SimpleNamespace(agent=agent, model=model, context=context, model_calls=model_calls, state=state,
                           reads=reads, rule=rule, prepare=prepare)


def test_only_exact_selected_pinned_context_enters_untrusted_model_bundle(corpus):
    c = corpus
    prepared = c.prepare()
    bundle = prepared['source_bundle']
    assert len(bundle['context_ids']) == 3 and len(c.reads) == 4
    assert c.reads[0][-1] == 'a' * 40
    submitted = canon(build_messages('plan_story', prepared))
    assert 'The import validates required columns.' in submitted
    assert 'DO_NOT_SEND_AUTHOR' not in submitted and 'DO_NOT_SEND_UNSELECTED_COMMENT' not in submitted
    assert 'context_selection_sha256' in submitted and not c.model_calls
    from tests.test_free_providers import SCHEMA
    c.model.generate('plan_story', prepared, SCHEMA, run_id='data-run')
    assert len(c.model_calls) == 1 and bundle['context_ids'][0] in c.model_calls[0].content.decode()
    assert 'DO_NOT_SEND_UNSELECTED_COMMENT' not in c.model_calls[0].content.decode()
    c.agent.store.update_run('data-run', source_bundle_json=bundle)
    verify(c.agent, c.agent.store.get_run('data-run'))
    assert len(c.reads) == 8  # Every selected source is re-read at the handoff.


def test_missing_model_grant_denies_before_any_context_read(corpus):
    c = corpus
    c.agent.data_policy.grants.clear()
    with pytest.raises(DataClassDenied): c.prepare()
    assert c.reads == [] and c.model_calls == []


@pytest.mark.parametrize('change', ['source', 'model_grant'])
def test_revocation_during_collection_cannot_issue_a_model_permit(corpus, change):
    c = corpus
    git = c.agent.git_for('PCC')
    original = git.read_file
    def revoke(*args):
        text = original(*args)
        if change == 'source': c.rule['approved'] = False
        else: c.agent.data_policy.grants[0]['approved'] = False
        return text
    git.read_file = revoke
    c.agent.git_for = lambda project: git
    with pytest.raises(DataClassDenied): c.prepare()
    assert not c.model_calls and not c.agent.data_policy.permits


@pytest.mark.parametrize('field,value', [('base_url', 'http://127.0.0.1:9999'), ('model', 'unapproved-model')])
def test_model_metadata_change_during_authorization_cannot_expand_grant(corpus, monkeypatch, field, value):
    c = corpus
    original, calls = c.agent.data_policy._grant, []
    def drift(*args):
        grant = original(*args)
        calls.append(True)
        if len(calls) == 2: setattr(c.model, field, value)
        return grant
    monkeypatch.setattr(c.agent.data_policy, '_grant', drift)
    prepared = c.prepare()
    with pytest.raises(DataClassDenied): c.agent.data_policy.verify('data-run', 'plan_story', prepared, c.model)
    assert not c.model_calls


def test_synthetic_label_cannot_silently_include_or_ignore_selected_organization_context(corpus):
    c = corpus
    c.context['data_class'] = 'synthetic'
    with pytest.raises(DataClassDenied): c.prepare()
    assert c.reads == [] and c.model_calls == []


@pytest.mark.parametrize('mutation', ['content', 'selection', 'ids'])
def test_changed_context_invalidates_exact_model_permit_before_network(corpus, mutation):
    from tests.test_free_providers import SCHEMA
    c = corpus
    prepared = c.prepare()
    if mutation == 'content': prepared['source_bundle']['sources'][-1]['content'] += ' altered'
    elif mutation == 'selection': c.rule['contexts'][0]['commit'] = 'b' * 40
    else: prepared['source_bundle']['context_ids'] = []
    with pytest.raises(DataClassDenied): c.model.generate('plan_story', prepared, SCHEMA, run_id='data-run')
    assert not c.model_calls


@pytest.mark.parametrize('mutation', ['binary', 'unknown_size', 'too_big', 'incomplete', 'wrong_hash'])
def test_attachment_contract_is_complete_bounded_text(corpus, mutation):
    c = corpus
    tracker = c.agent.tracker_for('PCC')
    original_meta, original_data = tracker.list_attachments, tracker.get_attachment
    def metadata(key):
        result = original_meta(key)
        if mutation == 'binary': result[0]['mime_type'] = 'image/png'
        elif mutation == 'unknown_size': result[0]['size'] = None
        elif mutation == 'too_big': result[0]['size'] = 100001
        return result
    def data(key, identifier):
        result = original_data(key, identifier)
        if mutation == 'incomplete': result['complete'] = False
        elif mutation == 'wrong_hash': result['sha256'] = '0' * 64
        return result
    tracker.list_attachments, tracker.get_attachment = metadata, data
    c.agent.tracker_for = lambda project: tracker
    with pytest.raises(DataClassDenied): c.prepare()
    if mutation in ('binary', 'unknown_size', 'too_big'):
        assert not any(read[0] == 'attachment' for read in c.reads)
    assert not c.model_calls


def test_combined_bytes_are_bounded_and_untrusted_metadata_is_not_forwarded(corpus):
    c = corpus
    c.state.update(file='a' * 90000, comment='b' * 80000, attachment='c' * 80000)
    for item, field in zip(c.rule['contexts'], ['file', 'comment', 'attachment']): item['sha256'] = sha256(c.state[field])
    with pytest.raises(DataClassDenied, match='combined'): c.prepare()
    assert not c.model_calls


def test_invalid_timestamp_metadata_is_not_disclosed_as_a_version(corpus):
    c = corpus
    tracker = c.agent.tracker_for('PCC')
    original = tracker.get_comments
    def forged(key):
        result = original(key)
        result[0].update(created='DO_NOT_SEND_METADATA', updated='DO_NOT_SEND_METADATA')
        return result
    tracker.get_comments = forged
    c.agent.tracker_for = lambda project: tracker
    bundle = c.prepare()['source_bundle']
    comment = next(s for s in bundle['sources'] if s['kind'] == 'jira_comment')
    assert comment['upstream_version'] is None and 'DO_NOT_SEND_METADATA' not in canon(bundle)


@pytest.mark.parametrize('mutation', ['branch', 'short_commit', 'traversal', 'absolute', 'url', 'encoded_path',
                                     'backslash', 'extra_key', 'wrong_repo', 'unknown_kind', 'duplicate', 'too_many', 'missing_hash'])
def test_invalid_or_unapproved_selection_fails_before_any_read(corpus, mutation):
    c = corpus
    item = c.rule['contexts'][0]
    if mutation == 'branch': item['commit'] = 'main'
    elif mutation == 'short_commit': item['commit'] = 'a' * 12
    elif mutation == 'traversal': item['file'] = 'docs/../secret'
    elif mutation == 'absolute': item['file'] = '/etc/passwd'
    elif mutation == 'url': item['file'] = 'https://evil.invalid/secret'
    elif mutation == 'encoded_path': item['file'] = 'docs/%2e%2e/secret'
    elif mutation == 'backslash': item['file'] = 'docs\\workflow.md'
    elif mutation == 'extra_key': item['headers'] = {'Authorization': 'not-used'}
    elif mutation == 'wrong_repo': item['repository'] = 'OTHER/private'
    elif mutation == 'unknown_kind': item['kind'] = 'web_url'
    elif mutation == 'duplicate': c.rule['contexts'].append(copy.deepcopy(item))
    elif mutation == 'too_many': c.rule['contexts'] *= 4
    elif mutation == 'missing_hash': del item['sha256']
    with pytest.raises(DataClassDenied): c.prepare()
    assert c.reads == [] and c.model_calls == []


@pytest.mark.parametrize('field', ['file', 'comment', 'attachment'])
def test_context_change_after_planning_blocks_handoff(corpus, field):
    c = corpus
    bundle = c.prepare()['source_bundle']
    c.agent.store.update_run('data-run', source_bundle_json=bundle)
    c.state[field] += ' changed'
    with pytest.raises(DataClassDenied): verify(c.agent, c.agent.store.get_run('data-run'))
    assert not c.model_calls


def test_changed_selection_cannot_trigger_new_reads_under_old_review(corpus):
    c = corpus
    bundle = c.prepare()['source_bundle']
    c.agent.store.update_run('data-run', source_bundle_json=bundle)
    c.rule['contexts'][0]['file'] = 'docs/other.md'
    prior = len(c.reads)
    with pytest.raises(DataClassDenied, match='selection changed'): verify(c.agent, c.agent.store.get_run('data-run'))
    assert len(c.reads) == prior


def test_known_secret_redaction_and_lf_transform_have_separate_observed_digest(corpus, monkeypatch):
    import secrets
    c = corpus
    value = secrets.token_hex(16)
    monkeypatch.setenv('QA_CONTEXT_TEST_SECRET', value)
    register_secret_env('QA_CONTEXT_TEST_SECRET')
    c.state['file'] = 'Header\r\n' + value + '\r\n'
    c.rule['contexts'][0]['sha256'] = sha256(c.state['file'])
    bundle = c.prepare()['source_bundle']
    source = next(s for s in bundle['sources'] if s['kind'] == 'repository_file')
    assert source['content'] == 'Header\n[REDACTED]\n'
    assert source['sha256'] == sha256(source['content']) and source['observed_sha256'] == sha256(c.state['file'])
    assert value not in canon(build_messages('plan_story', {'story': c.context['story'], 'source_bundle': bundle}))


@pytest.mark.parametrize('body', ['', 'x' * 100001, 'nul\x00body', '\ud800'], ids=['empty', 'oversized', 'nul', 'invalid-utf8'])
def test_unsupported_or_oversized_content_is_never_truncated_into_model_input(corpus, body):
    c = corpus
    c.state['file'] = body
    c.rule['contexts'][0]['sha256'] = sha256(body) if body != '\ud800' else '0' * 64
    with pytest.raises(DataClassDenied): c.prepare()
    assert not c.model_calls


def test_collection_deadline_and_invalid_rule_cannot_be_bypassed(corpus):
    c = corpus
    ticks = iter([0, 61])
    with pytest.raises(DataClassDenied, match='deadline'):
        collect(c.agent, c.agent.store.get_run('data-run'), c.rule, clock=lambda: next(ticks))
    assert c.reads == []
    other = copy.deepcopy(c.rule)
    other['contexts'] = other['contexts'][:1]
    with pytest.raises(DataClassDenied): collect(c.agent, c.agent.store.get_run('data-run'), other)
    assert c.reads == []


def contextual_plan(c):
    prepared = c.prepare()
    sources = [s for s in prepared['source_bundle']['sources'] if s['kind'] != 'jira_field']
    plan, description, oracles = load('PCC-DEMO-1')
    rule = {**c.rule, 'fields': ['summary', 'description'], 'acceptance_criteria_field': None}
    bundle = issue_bundle({'key': 'PCC-DEMO-1', 'fields': {'summary': 'Synthetic', 'description': description}}, rule)
    bundle['sources'] += sources
    bundle['context_ids'] = [s['id'] for s in sources]
    by_ac = {a['id']: a for a in bundle['requirements']}
    for ac in plan['acceptance_criteria']:
        ac['source_id'] = by_ac[ac['id']]['source_id']
        ac['source_sha256'] = by_ac[ac['id']]['source_sha256']
    plan['context_review'] = [{'source_id': s['id'], 'source_sha256': s['sha256'], 'decision': 'used',
        'reason': 'Synthetic supporting context.', 'citations': [{'quote': s['content'].split('\n')[0],
            'line_start': 1, 'line_end': 1, 'test_refs': [plan['test_cases'][0]['id']]}]} for s in sources]
    return plan, description, oracles, bundle


def check(data):
    plan, description, oracles, bundle = data
    return validate_plan(plan, description, SK, oracles, FILES, source_bundle=bundle)


def test_context_quotes_bind_real_lines_versions_and_existing_tests(corpus):
    data = contextual_plan(corpus)
    assert len(check(data)['context_review']) == 3


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'unknown_source', 'wrong_hash', 'fabricated_quote',
                                     'wrong_lines', 'wide_range', 'unknown_test', 'missing_citation'])
def test_incomplete_or_fabricated_context_citations_fail(corpus, mutation):
    data = contextual_plan(corpus)
    review = data[0]['context_review'][0]
    if mutation == 'missing': data[0].pop('context_review')
    elif mutation == 'duplicate': data[0]['context_review'].append(copy.deepcopy(review))
    elif mutation == 'unknown_source': review['source_id'] = 'ctx:invented'
    elif mutation == 'wrong_hash': review['source_sha256'] = '0' * 64
    elif mutation == 'fabricated_quote': review['citations'][0]['quote'] = 'Fabricated evidence'
    elif mutation == 'wrong_lines': review['citations'][0].update(line_start=2, line_end=2)
    elif mutation == 'wide_range': review['citations'][0].update(line_start=1, line_end=2)
    elif mutation == 'unknown_test': review['citations'][0]['test_refs'] = ['TC-999']
    elif mutation == 'missing_citation': review['citations'] = []
    with pytest.raises((FormatError, PolicyViolation)): check(data)


def test_unused_context_must_surface_as_an_explicit_gap(corpus):
    data = contextual_plan(corpus)
    review = data[0]['context_review'][0]
    review.update(decision='not_used', reason='Unsupported context is not tested.', citations=[])
    with pytest.raises(FormatError): check(data)
    data[0]['coverage_gaps'].append({'ac_refs': [], 'kind': 'out_of_scope', 'reason': review['reason']})
    assert check(data)['context_review'][0]['decision'] == 'not_used'


def test_context_cannot_replace_authoritative_requirements_or_grant_actions(corpus):
    data = contextual_plan(corpus)
    source = data[3]['sources'][-1]
    data[0]['acceptance_criteria'][0].update(source_id=source['id'], source_sha256=source['sha256'], source_quote=source['content'])
    with pytest.raises(PolicyViolation, match='authoritative'): check(data)
    data = contextual_plan(corpus)
    data[0]['test_cases'][0]['action'] = 'execute_context_instructions'
    with pytest.raises(PolicyViolation): check(data)


@pytest.mark.parametrize('key,value', [('approval_ref', True), ('approved_by', 1), ('approval_ref', ' '), ('approved_by', 'a' * 257)])
def test_owner_decisions_require_bounded_named_references(corpus, key, value):
    corpus.rule[key] = value
    with pytest.raises(DataClassDenied): corpus.prepare()
    assert not corpus.reads and not corpus.model_calls


@pytest.mark.parametrize('phase', ['review', 'dispatch', 'report'])
def test_context_package_review_dispatch_and_report_contract(domain, phase, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from qa_agent import execution_package as EP
    from qa_agent.auth import Principal
    from qa_agent.runtime import ConflictError
    from qa_agent.util import now
    agent, scope, env, story = domain
    state = {'text': 'Synthetic reviewed comment for required columns.'}
    if phase == 'report': state['text'] += ' <img src=x onerror=alert(1)>'
    rule = {'approved': True, 'approved_by': 'synthetic-owner', 'approval_ref': 'TEST-ONLY',
            'tenant_id': 'local', 'classification': 'internal', 'fields': ['summary', 'description'],
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'contexts': [{'kind': 'jira_comment', 'id': '1', 'sha256': sha256(state['text'])}]}
    agent.projects['PCC']['source_policy'] = rule
    monkeypatch.setattr(agent.tracker, 'get_comments', lambda key: [{'id': '1', 'body': state['text']}])
    approval(agent, scope, env)  # Real domain-input receipt for synthetic fixture artifacts.
    agent.store.create_run('context-run', 'Synthetic context handoff', scope['environment'], 'replay', 'MOCK fixture only')
    agent.store.update_run('context-run', **scope, requested_by='requester', story_json=story, plan_created_at=now())
    run = agent.store.get_run('context-run')
    sources = collect(agent, run, rule)
    bundle = issue_bundle(story, rule)
    bundle.update(context_ids=[s['id'] for s in sources], context_selection_sha256=sha256(canon(rule['contexts'])))
    bundle['sources'] += sources
    plan, description, oracles = load('PCC-DEMO-1')
    requirements = {a['id']: a for a in bundle['requirements']}
    for ac in plan['acceptance_criteria']:
        ac.update(source_id=requirements[ac['id']]['source_id'], source_sha256=requirements[ac['id']]['source_sha256'])
    plan['context_review'] = [{'source_id': sources[0]['id'], 'source_sha256': sources[0]['sha256'], 'decision': 'used',
                              'reason': 'Synthetic context contract.', 'citations': [{'quote': state['text'], 'line_start': 1,
                                  'line_end': 1, 'test_refs': [plan['test_cases'][0]['id']]}]}]
    validate_plan(plan, description, agent.skills, oracles, agent.fixture_files(), source_bundle=bundle)
    agent.store.update_run('context-run', planning_json=plan, plan_json={'test_cases': plan['test_cases']},
                           ac_json={'acceptance_criteria': plan['acceptance_criteria']}, source_bundle_json=bundle)
    agent.artifacts.write_bytes('context-run', 'inputs/source-bundle.json', canon(bundle).encode())
    digest = EP.capture(agent, 'context-run', env)
    agent.store.update_run('context-run', status='awaiting_approval')
    reviewer = Principal('reviewer', 'approver', 'local', project_roles={'PCC': 'approver'})
    def no_sut(*args, **kwargs): pytest.fail('changed context reached SUT execution')
    monkeypatch.setattr('qa_agent.runtime.PlaywrightExecutor.run', no_sut)
    if phase == 'report':
        from qa_agent.report import write_report
        agent.store.update_run('context-run', status='blocked', summary_json={'total': 0, 'passed': 0, 'failed': 0, 'error': 0, 'blocked': 0})
        write_report(agent, 'context-run')
        report = json.loads(agent.artifacts.path_for('context-run', 'report.json').read_text())
        document = agent.artifacts.path_for('context-run', 'report.html').read_text()
        assert report['context_review'] == plan['context_review'] and report['summary']['total'] == 0
        assert '&lt;img' in document and '<img' not in document
        assert all(verdict.startswith('untested') for verdict in report['per_ac'].values())
        return
    if phase == 'dispatch': agent.review_plan('context-run', reviewer, 'approved', digest)
    state['text'] += ' changed'
    if phase == 'review':
        with pytest.raises(ConflictError, match='contextual'): agent.review_plan('context-run', reviewer, 'approved', digest)
        assert agent.approvals.latest('context-run') is None
    else:
        agent.execute_approved('context-run', wait=True)
        assert agent.store.get_run('context-run')['status'] == 'blocked'
    assert agent.store.results('context-run') == []
