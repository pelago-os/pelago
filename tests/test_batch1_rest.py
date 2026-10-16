import json
import sqlite3
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import config
from app.database import get_connection
from app.routes import forum
from app.services import audit, login_limits
from test_forum_api import auth, client






@pytest.mark.parametrize('peer,xff,expected', [
    ('192.0.2.2', '198.51.100.1', '192.0.2.2'),
    ('127.0.0.1', '198.51.100.1', '198.51.100.1'),
    ('127.0.0.1', 'forged,198.51.100.1', '127.0.0.1'),
    ('127.0.0.1', '192.0.2.9,198.51.100.1', '198.51.100.1'),
    ('127.0.0.1', '198.51.100.1,127.0.0.1', '198.51.100.1'),
])
def test_trusted_proxy_chain(monkeypatch, peer, xff, expected):
    monkeypatch.setenv('PELAGO_TRUSTED_PROXY_IPS', '127.0.0.1')
    request = Request({'type': 'http', 'client': (peer, 123),
                       'headers': [(b'x-forwarded-for', xff.encode())]})
    assert login_limits.login_source(request) == expected


def test_many_sources_cannot_lock_vv_and_success_clears_pair(client, monkeypatch):
    monkeypatch.setenv('PELAGO_TRUSTED_PROXY_IPS', '127.0.0.1')
    with TestClient(client.app, client=('127.0.0.1', 123)) as proxy:
        for source in ('192.0.2.1', '192.0.2.2'):
            for _ in range(10):
                assert proxy.post('/api/login', headers={'X-Forwarded-For': source},
                                  json={'username': 'owner', 'password': 'wrong'}).status_code == 401
            assert proxy.post('/api/login', headers={'X-Forwarded-For': source},
                              json={'username': 'owner', 'password': 'wrong'}).status_code == 429
        good = {'X-Forwarded-For': '198.51.100.1'}
        for _ in range(9):
            assert proxy.post('/api/login', headers=good,
                              json={'username': 'owner', 'password': 'wrong'}).status_code == 401
        assert proxy.post('/api/login', headers=good,
                          json={'username': 'owner', 'password': 'owner-dev-password'}).status_code == 200
        assert ('account', '198.51.100.1', 'owner') not in login_limits._attempts
        assert proxy.post('/api/login', headers=good,
                          json={'username': 'owner', 'password': 'wrong'}).status_code == 401
        assert proxy.get('/api/me', headers=auth('owner-dev-token')).status_code == 200


def test_distinct_source_memory_pressure_does_not_block_all(monkeypatch):
    monkeypatch.setattr(login_limits, 'MAX_KEYS', 8)
    for i in range(20):
        login_limits.check_login_limit(f'peer-{i}', 'owner')
        assert len(login_limits._attempts) <= 8
    login_limits.check_login_limit('legitimate-device', 'owner')


def fail_audit(*args, **kwargs):
    raise OSError('SECRET_TOKEN failed mirror')


@pytest.mark.parametrize('operation', ['profile', 'invite', 'register', 'login'])
def test_new_audits_survive_mirror_failure_and_recover_once(client, monkeypatch, operation):
    if operation == 'register':
        made = client.post('/api/admin/invites', headers=auth('owner-dev-token'), json={'code': 'SECRET_INVITE'})
        assert made.status_code == 201
    with monkeypatch.context() as patch:
        patch.setattr(forum, 'append_audit', fail_audit)
        if operation == 'profile':
            response = client.patch('/api/me/profile', headers=auth('rowan-dev-token'), json={'avatar_emoji': '🐋'})
        elif operation == 'invite':
            response = client.post('/api/admin/invites', headers=auth('owner-dev-token'), json={'code': 'SECRET_INVITE'})
        elif operation == 'register':
            response = client.post('/api/register', json={'username': 'newbie', 'display_name': 'Newbie', 'password': 'SECRET_PASSWORD', 'invite_code': 'SECRET_INVITE'})
        else:
            response = client.post('/api/login', json={'username': 'owner', 'password': 'owner-dev-password'})
    assert response.status_code in (200, 201), response.text
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM forum_mutation_audit WHERE delivered_at IS NULL').fetchall()
        assert len(rows) == 1
        event_id = rows[0]['id']
        assert 'SECRET' not in rows[0]['payload_json']
        assert rows[0]['last_error'] == 'OSError'
    assert audit.recover_mutation_audits() == {event_id: True}
    before = config.settings.audit_log_path.read_text()
    assert audit.recover_mutation_audits() == {}
    assert audit.deliver_mutation_audit(event_id)
    assert config.settings.audit_log_path.read_text() == before
    assert sum(json.loads(line).get('event_id') == event_id for line in before.splitlines()) == 1


def test_profile_enqueue_failure_rolls_back(client, monkeypatch):
    before = client.get('/api/me', headers=auth('rowan-dev-token')).json()
    monkeypatch.setattr(forum, 'queue_mutation_audit', fail_audit)
    with pytest.raises(OSError):
        client.patch('/api/me/profile', headers=auth('rowan-dev-token'), json={'avatar_emoji': '🐋'})
    assert client.get('/api/me', headers=auth('rowan-dev-token')).json() == before


def test_delivery_db_failure_does_not_turn_committed_mutation_into_http_failure(client, monkeypatch, caplog):
    with get_connection() as conn:
        event_id = audit.queue_mutation_audit(conn, 'test', {})
    monkeypatch.setattr(audit, '_deliver_mutation_audit', fail_audit)
    assert audit.deliver_mutation_audit(event_id) is False
    assert 'SECRET' not in caplog.text
    with get_connection() as conn:
        assert conn.execute('SELECT delivered_at FROM forum_mutation_audit WHERE id=?', (event_id,)).fetchone()[0] is None




@pytest.mark.parametrize('operation', ['register', 'invite', 'login'])
def test_account_mutation_enqueue_failure_rolls_back(client, monkeypatch, operation):
    if operation == 'register':
        assert client.post('/api/admin/invites', headers=auth('owner-dev-token'),
                           json={'code': 'rollback-invite'}).status_code == 201
    def counts():
        with get_connection() as conn:
            return tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('users', 'tokens', 'invites', 'forum_mutation_audit')) + (
                             conn.execute('SELECT COALESCE(SUM(used_count),0) FROM invites').fetchone()[0],)
    before = counts()
    monkeypatch.setattr(forum, 'queue_mutation_audit', fail_audit)
    with pytest.raises(OSError):
        if operation == 'register':
            client.post('/api/register', json={'username': 'rollback', 'display_name': 'Rollback',
                        'password': 'good-password', 'invite_code': 'rollback-invite'})
        elif operation == 'invite':
            client.post('/api/admin/invites', headers=auth('owner-dev-token'), json={})
        else:
            client.post('/api/login', json={'username': 'owner', 'password': 'owner-dev-password'})
    assert counts() == before




