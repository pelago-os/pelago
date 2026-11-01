import copy
import json
import pytest
from app.meetings.adapters.receipts import parse_claude, parse_hermes


def claude():
    return [{'type': 'system', 'subtype': 'init', 'tools': [], 'mcp_servers': [], 'session_id': 'session-1', 'model': 'requested-only'},
            {'type': 'result', 'is_error': False, 'result': 'answer', 'modelUsage': {'served-1': {}}, 'session_id': 'session-1'}]


def run_claude(events, code=0):
    return parse_claude('\n'.join(json.dumps(e) for e in events), code, 'request-1', {'request-1': ('served-1',)}, 'session-1')


def test_claude_served_comes_from_usage_not_init():
    r = run_claude(claude())
    assert r['status'] == 'accepted' and r['model'] == 'served-1'
    assert r['support_tier'] == 'experimental'


@pytest.mark.parametrize('case', ['tools', 'mcp', 'tool_use', 'duplicate_init', 'duplicate_result', 'session', 'model', 'missing_usage', 'bad_result', 'provider_error', 'exit'])
def test_claude_fails_closed(case):
    e = claude()
    if case == 'tools': e[0]['tools'] = ['Bash']
    elif case == 'mcp': e[0]['mcp_servers'] = ['external']
    elif case == 'tool_use': e.insert(1, {'type': 'assistant', 'message': {'content': [{'type': 'tool_use'}]}})
    elif case == 'duplicate_init': e.insert(0, copy.deepcopy(e[0]))
    elif case == 'duplicate_result': e.append(copy.deepcopy(e[-1]))
    elif case == 'session': e[-1]['session_id'] = 'different'
    elif case == 'model': e[-1]['modelUsage'] = {'unexpected': {}}
    elif case == 'missing_usage': del e[-1]['modelUsage']
    elif case == 'bad_result': e[-1]['result'] = {'secret': 'never-return'}
    elif case == 'provider_error': e[-1]['is_error'] = True
    assert run_claude(e, 1 if case == 'exit' else 0)['status'] == 'failed'


def hermes():
    return dict(schema='pelago.hermes.v1', credential_mode='profile-access-only-no-copy',
                requested_model='request-1', session_id='session-1', runtime_sha256='a'*64,
                speaker_sha256='b'*64, runtime_guard=True, title_generation=False, tools=[], tool_attempts=0,
                text='answer', calls=[dict(requested='request-1', provider='test-provider', served='served-1',
                                         source='sdk.response.model', tools=[], session_id='session-1')])


def run_hermes(data):
    return parse_hermes(json.dumps(data), 0, 'request-1', {('test-provider', 'served-1')}, 'session-1', 'a'*64, 'b'*64)


def test_hermes_exact_bindings():
    assert run_hermes(hermes())['status'] == 'accepted'


@pytest.mark.parametrize('field,value', [('runtime_sha256', 'c'*64), ('speaker_sha256', 'c'*64),
    ('credential_mode', 'copied'), ('session_id', 'other'), ('tool_attempts', False), ('title_generation', True),
    ('runtime_guard', 1), ('tools', ['shell']), ('calls', []), ('calls', [None]), ('text', None)])
def test_hermes_rejects_invalid_evidence(field, value):
    data = hermes(); data[field] = value
    assert run_hermes(data)['status'] == 'failed'


@pytest.mark.parametrize('field,value', [('served', 'other'), ('provider', 'other'), ('source', 'init.model'),
                                      ('requested', 'fallback'), ('tools', ['shell']), ('session_id', 'other')])
def test_hermes_rejects_each_bad_call(field, value):
    data = hermes(); data['calls'][0][field] = value
    assert run_hermes(data)['status'] == 'failed'


@pytest.mark.parametrize('raw', ['null', '[]', 'not-json', '{"secret":"must-not-echo"}'])
def test_parser_errors_do_not_echo_provider_output(raw):
    a = parse_claude(raw, 1, 'request-1', {'request-1': ('served-1',)}, 'session-1')
    b = parse_hermes(raw, 1, 'request-1', {('test-provider', 'served-1')}, 'session-1', 'a'*64, 'b'*64)
    assert a['status'] == b['status'] == 'failed'
    assert 'must-not-echo' not in json.dumps([a, b])


def test_hermes_requires_nonempty_typed_request_and_session():
    data = hermes()
    data['requested_model'] = data['calls'][0]['requested'] = None
    result = parse_hermes(json.dumps(data), 0, None, {('test-provider', 'served-1')}, 'session-1', 'a'*64, 'b'*64)
    assert result['status'] == 'failed'


def test_claude_other_configured_route_is_not_an_allowed_fallback():
    events=claude()
    routes={'request-1': ('another-served',), 'request-2': ('served-1',)}
    result=parse_claude('\n'.join(json.dumps(e) for e in events),0,'request-1',routes,'session-1')
    assert result['error_code']=='model_mismatch'
