from concurrent.futures import ThreadPoolExecutor

from app import config
from app.database import get_connection
from scripts.manage_member import manage
from test_forum_api import client, auth  # noqa: F401


def test_roster_requires_login_and_external_sees_public_authors_only(client):
    assert client.get('/api/agents').status_code == 401
    with get_connection() as conn:
        conn.execute("UPDATE users SET membership_kind='external' WHERE username='cedar'")
    client.post('/api/threads', headers=auth('rowan-dev-token'), json={'board_slug':'engineering','title':'Hidden','body_markdown':'hidden'})
    client.post('/api/threads', headers=auth('owner-dev-token'), json={'board_slug':'tea_house','title':'Public','body_markdown':'public'})
    rows=client.get('/api/agents',headers=auth('cedar-dev-token')).json()
    assert {r['username'] for r in rows} == {'owner'}
    assert all('role' not in r and 'membership_kind' not in r for r in rows)


def test_invitation_expiry_password_limit_and_approval(client):
    data={'code':'invite-valid-1234','requires_approval':True,'expires_at':'bad'}
    assert client.post('/api/admin/invites',headers=auth('owner-dev-token'),json=data).status_code==422
    data['expires_at']='2099-01-01T00:00:00Z'
    assert client.post('/api/admin/invites',headers=auth('owner-dev-token'),json=data).status_code==201
    registration={'username':'new_person','display_name':'New','password':'shortpass','invite_code':data['code']}
    assert client.post('/api/register',json=registration).status_code==422
    registration['password']='unique-long-password'
    response=client.post('/api/register',json=registration)
    assert response.status_code==201 and response.json()['status']=='pending'
    login={'username':'new_person','password':registration['password']}
    assert client.post('/api/login',json=login).status_code!=200
    manage(config.settings.db_path,'new_person',approve=True)
    assert client.post('/api/login',json=login).status_code==200
    with get_connection() as conn:
        assert conn.execute("SELECT membership_kind FROM users WHERE username='new_person'").fetchone()[0]=='external'


def test_single_use_invite_is_atomic(client):
    code='single-use-123456'
    assert client.post('/api/admin/invites',headers=auth('owner-dev-token'),json={'code':code,'max_uses':1,'requires_approval':False}).status_code==201
    def register(i):
        return client.post('/api/register',json={'username':f'new_user_{i}','display_name':'Test','password':'unique-long-password','invite_code':code}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register,[1,2]))==[201,400]
    with get_connection() as conn:
        assert conn.execute('SELECT used_count FROM invites WHERE code_hash IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()[0]==1


def test_invalid_stored_expiry_is_rejected():
    from app.routes.forum import invite_expired
    assert invite_expired('invalid')
    assert invite_expired('2099-01-01T00:00:00')


def test_encoded_upload_bound():
    from app.schemas import ImageUploadRequest
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ImageUploadRequest(filename='test.png',content_type='image/png',data_base64='a'*11_200_001)


def test_startup_recovers_audit(client,monkeypatch):
    from app.services import audit
    from app.main import app
    from fastapi.testclient import TestClient
    with get_connection() as conn:
        event=audit.queue_mutation_audit(conn,'recovery_probe',{})
    with TestClient(app):
        pass
    with get_connection() as conn:
        assert conn.execute('SELECT delivered_at FROM forum_mutation_audit WHERE id=?',(event,)).fetchone()[0]
