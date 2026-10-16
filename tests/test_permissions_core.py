"""Negative authorization tests use isolated DBs and mocked provider execution."""
import base64
import dataclasses
import pytest
from app.auth import hash_token
from app.database import get_connection
from test_forum_api import auth, client


def external():
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO users(username,display_name,role,persona_id) VALUES ('outside','Outside','member','rowan')")
        conn.execute("INSERT INTO tokens(user_id,token_hash) VALUES (?,?)", (cur.lastrowid, hash_token('outside-token')))
    return auth('outside-token')


def thread(client, board='engineering', token='rowan-dev-token', body='private-marker'):
    response = client.post('/api/threads', headers=auth(token), json={'board_slug': board, 'title': 'permission case', 'body_markdown': body})
    assert response.status_code == 201, response.text
    return response.json()


def test_membership_is_explicit_and_seed_does_not_promote(client):
    headers = external()
    assert client.get('/api/me', headers=headers).json()['membership_kind'] == 'external'
    assert client.get('/api/me', headers=auth('linden-dev-token')).json()['membership_kind'] == 'crew'
    with get_connection() as conn:
        conn.execute("UPDATE users SET membership_kind='external' WHERE username='cedar'")
    from legacy_seed import main
    main()
    assert client.get('/api/me', headers=auth('cedar-dev-token')).json()['membership_kind'] == 'external'


@pytest.mark.parametrize('board', ['engineering','proposals','handoff'])
def test_external_and_anonymous_cannot_read_or_mutate_hidden_board(client, board):
    private = thread(client, board)
    outsider = external()
    for headers in ({}, outsider):
        assert client.get(f"/api/threads/{private['id']}", headers=headers).status_code == 403
        assert client.get('/api/threads', params={'board': board}, headers=headers).status_code == 403
        result = client.get('/api/threads', params={'q': 'private-marker'}, headers=headers)
        assert result.json()['total'] == 0
        assert board not in [b['slug'] for b in client.get('/api/boards', headers=headers).json()]
        assert 'private-marker' not in client.get('/api/station/snapshot', headers=headers).text
    tid = private['id']; pid = private['posts'][0]['id']
    for method, url, body in [('post',f'/api/threads/{tid}/posts',{'body_markdown':'attack'}), ('post', f'/api/posts/{pid}/like',None), ('delete',f'/api/posts/{pid}/like',None), ('patch',f'/api/threads/{tid}',{'title':'attack'}), ('delete',f'/api/threads/{tid}',None)]:
        kwargs={'headers':outsider}
        if body: kwargs['json']=body
        assert client.request(method,url,**kwargs).status_code == 403
    assert client.get(f'/api/export/thread/{tid}/markdown',headers=outsider).status_code == 403
    assert 'private-marker' not in client.get('/api/threads/export.md',headers=outsider).text
    assert 'private-marker' not in client.get('/api/threads/personal-export.md?target=rowan',headers=outsider).text


def test_external_public_scope_and_crew_gates(client):
    outsider=external()
    for board in ('tea_house','guest_questions','tidal_studio'):
        result=client.post('/api/threads',headers=outsider,json={'board_slug':board,'title':'public','body_markdown':'hello'})
        assert result.status_code==201, result.text
        assert client.get(f"/api/threads/{result.json()['id']}").status_code==200
    public=thread(client,'announcements')
    assert client.get(f"/api/threads/{public['id']}",headers=outsider).status_code==200
    assert client.post(f"/api/threads/{public['id']}/posts",headers=outsider,json={'body_markdown':'no'}).status_code==403
    for url in ('/api/library/status',):
        assert client.get(url,headers=outsider).status_code==403
        assert client.get(url,headers=auth('cedar-dev-token')).status_code==200
    snapshot=client.get('/api/station/snapshot',headers=outsider).json()
    assert not snapshot['systems']['meeting_access']
    assert not snapshot['systems']['library_available']
    assert snapshot['crew']==[]


def session(client, token='cedar-dev-token', **kwargs):
    response=client.post('/api/meeting-room/sessions',headers=auth(token),json={'agent_id':'lark','adapter':'mock',**kwargs})
    assert response.status_code==201,response.text
    return response.json()['id']






def test_media_private_public_and_cookie_not_api_auth(client):
    outsider=external()
    payload={'filename':'test.png','content_type':'image/png','data_base64':base64.b64encode(b'\x89PNG\r\n\x1a\nTEST').decode()}
    uploaded=client.post('/api/uploads/images',headers=auth('rowan-dev-token'),json=payload)
    assert uploaded.status_code==201,uploaded.text
    url=uploaded.json()['url']
    thread(client,body=f'![private]({url})')
    assert client.get(url).status_code==403
    assert client.get(url,headers=outsider).status_code==403
    assert client.get(url,headers=auth('rowan-dev-token')).status_code==200
    # An outsider cannot make someone else's private file public by guessing its URL.
    thread(client,'tea_house','outside-token',f'![stolen]({url})')
    assert client.get(url,headers=outsider).status_code==403
    client.get('/api/me',headers=auth('rowan-dev-token'))
    assert client.get(url).status_code==200
    assert client.post('/api/threads',json={'board_slug':'engineering','title':'x','body_markdown':'x'}).status_code==401
    client.cookies.clear()
    thread(client,'tea_house',body=f'![shared]({url})')
    assert client.get(url).status_code==200


def test_logout_revokes_only_current_device_and_media_cookie(client):
    login=client.post('/api/login',json={'username':'cedar','password':'cedar-dev-password'})
    token=login.json()['token']
    assert client.post('/api/logout',headers=auth(token)).status_code==200
    assert client.get('/api/me',headers=auth(token)).status_code==401
    assert client.get('/api/me',headers=auth('cedar-dev-token')).status_code==200
    # Cookie alone is not API authorization.
    assert client.get('/api/me').status_code==401


def test_invite_member_cannot_self_promote(client):
    from app.auth import hash_token
    with get_connection() as conn:
        uid=conn.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
        conn.execute("INSERT INTO invites(code_hash,notes,default_role,max_uses,requires_approval,created_by_user_id) VALUES (?,'test','member',2,0,?)", (hash_token('invite-secret'),uid))
    response=client.post('/api/register',json={'username':'visitor','display_name':'Visitor','password':'a-long-enough-password','invite_code':'invite-secret','membership_kind':'crew'})
    # Extra self-asserted membership is rejected by schema, or ignored; never promoted.
    if response.status_code==422:
        response=client.post('/api/register',json={'username':'visitor','display_name':'Visitor','password':'a-long-enough-password','invite_code':'invite-secret'})
    assert response.status_code==201,response.text
    with get_connection() as conn:
        row=conn.execute("SELECT membership_kind FROM users WHERE username='visitor'").fetchone()
        assert row[0]=='external'


@pytest.mark.parametrize('operation', ['reply', 'like', 'unlike'])
def test_board_move_between_request_guard_and_mutation_is_rechecked(client, monkeypatch, operation):
    from app.routes import forum
    target=thread(client,'tea_house')
    outsider=external()
    original=forum.require_thread_board
    calls=0
    def move_after_first_check(conn, actor, thread_id, **kwargs):
        nonlocal calls
        original(conn,actor,thread_id,**kwargs)
        calls+=1
        if calls==1:
            with get_connection() as writer:
                writer.execute("UPDATE threads SET board_id=(SELECT id FROM boards WHERE slug='engineering') WHERE id=?",(thread_id,))
    monkeypatch.setattr(forum,'require_thread_board',move_after_first_check)
    if operation=='reply':
        response=client.post(f"/api/threads/{target['id']}/posts",headers=outsider,json={'body_markdown':'must not persist'})
    else:
        method=client.post if operation=='like' else client.delete
        response=method(f"/api/posts/{target['posts'][0]['id']}/like",headers=outsider)
    assert response.status_code==403,response.text
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts WHERE body_markdown='must not persist'").fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM post_likes').fetchone()[0]==0
