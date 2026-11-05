"""HTTP input boundaries, rollback and replay: no real adapters or production data."""
import pytest
from test_meetings import env, create, get, tok, rid  # noqa: F401


@pytest.mark.parametrize('field,value', [
    ('participants', [{}]), ('participants', 'agent_alpha'), ('materials', [{}]),
    ('models', {'agent_alpha': {}}), ('models', []), ('moderator', {}),
    ('request_id', 123), ('request_id', ['abc']), ('rounds', True),
    ('rounds', 1.5), ('title', {}),
])
def test_create_bad_shape_is_client_error_without_writes(env, field, value):
    body = {'request_id': rid(), 'title': 'boundary', 'participants': ['agent_alpha', 'agent_beta'], field: value}
    before = env.client.get('/api/meetings', headers=tok('moderator')).json()
    response = env.client.post('/api/meetings', headers=tok('moderator'), json=body)
    assert response.status_code == 400 and response.json()["error"] == "invalid_request", response.text
    assert env.client.get('/api/meetings', headers=tok('moderator')).json() == before


@pytest.mark.parametrize('field,value', [('max_chars', {}), ('max_chars', 'oops'),
    ('max_chars', True), ('add', [{}]), ('remove', [{}]), ('models', {'agent_alpha': []}),
    ('expected_version', True), ('expected_version', []), ('request_id', 123)])
def test_patch_bad_shape_leaves_meeting_unchanged(env, field, value):
    m = create(env)
    before = get(env, m['id']).json()
    response = env.client.patch('/api/meetings/' + m['id'], headers=tok('moderator'),
        json={'request_id': rid(), 'expected_version': m['version'], field: value})
    assert response.status_code == 400 and response.json()["error"] == "invalid_request", response.text
    assert get(env, m['id']).json() == before


def test_invalid_input_is_checked_after_authorization(env):
    m = create(env)
    r = env.client.patch('/api/meetings/' + m['id'], headers=tok('agent_gamma'),
        json={'request_id': 123, 'expected_version': m['version'], 'max_chars': {}})
    assert r.status_code == 404  # private meeting is not disclosed to a nonparticipant


def test_patch_retry_replays_once_and_payload_reuse_conflicts(env):
    m = create(env); url = '/api/meetings/' + m['id']
    body = {'request_id': rid(), 'expected_version': m['version'], 'max_chars': 600}
    first = env.client.patch(url, headers=tok('moderator'), json=body)
    assert first.status_code == 200
    retry = env.client.patch(url, headers=tok('moderator'), json=body)
    assert retry.json() == first.json()
    reused = env.client.patch(url, headers=tok('moderator'), json={**body, 'max_chars': 700})
    assert reused.status_code == 409 and reused.json()['error'] == 'request_id_reused'
    stale = env.client.patch(url, headers=tok('moderator'), json={**body, 'request_id': rid()})
    assert stale.status_code == 409 and stale.json()['error'] == 'version_conflict'
    assert get(env, m['id']).json()['version'] == first.json()['version']


@pytest.mark.parametrize('action,extra', [('materials', {'paths': [{}]}), ('export', {'revision': 'oops'})])
def test_action_bad_shape_has_no_server_error(env, action, extra):
    m = create(env)
    r = env.client.post(f"/api/meetings/{m['id']}/{action}", headers=tok('moderator'),
        json={'request_id': rid(), 'expected_version': m['version'], **extra})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request", r.text


def test_health_dispatches_to_own_adapter_without_provider_calls(env, monkeypatch):
    from dataclasses import replace
    from app.routes import meetings
    from app.meetings.adapters import claude_runtime, hermes_runtime
    members=meetings.load_members()
    members['agent_alpha']=replace(members['agent_alpha'], adapter='hermes')
    members['agent_beta']=replace(members['agent_beta'], adapter='claude_code')
    monkeypatch.setattr(meetings,'load_members',lambda:members)
    seen=[]
    def probe(adapter):
        def health(member):
            assert member.adapter==adapter
            seen.append((adapter,member.username))
            return {'ok':False,'status':'experimental_requires_acceptance','live_verified':False}
        return health
    monkeypatch.setattr(hermes_runtime,'health',probe('hermes'))
    monkeypatch.setattr(claude_runtime,'health',probe('claude_code'))
    for module in (hermes_runtime,claude_runtime):
        monkeypatch.setattr(module,'_approved',lambda *a,**k:pytest.fail('listing must not approve a runtime'))
    response=env.client.get('/api/meetings/adapters',headers=tok('moderator'))
    assert response.status_code==200
    assert seen==[('hermes','agent_alpha'),('claude_code','agent_beta')]
    assert all(m['health']['live_verified'] is False for m in response.json()['members'])
