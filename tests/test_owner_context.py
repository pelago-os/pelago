from pathlib import Path

from app.auth import Actor, hash_token
from app.database import get_connection
from app.services.forum_queries import target_aliases
from app.services.library import actor_can_write_library
from test_forum_api import auth, client  # noqa: F401


def test_library_write_uses_capability_not_username():
    assert actor_can_write_library(Actor(1, 'new_owner', 'Owner', 'admin', None, 'crew'))
    assert not actor_can_write_library(Actor(2, 'owner', 'Not owner', 'admin', None, 'external'))
    assert not actor_can_write_library(Actor(3, 'moderator', 'Moderator', 'moderator', None, 'crew'))


def test_aliases_come_only_from_account_and_explicit_input():
    assert target_aliases({'username': 'rowan', 'display_name': 'Example', 'persona_id': None}) == ['rowan', 'Example']
    assert 'custom_alias' in target_aliases({'username': 'agent', 'display_name': 'Example'}, 'custom_alias')


def test_owner_context_follows_active_admin_role(client):
    with get_connection() as conn:
        conn.execute("UPDATE users SET username='new_owner' WHERE username='owner'")
        cur = conn.execute("INSERT INTO users(username,display_name,role,membership_kind,status) VALUES ('owner','Impostor','admin','external','active')")
        conn.execute('INSERT INTO tokens(user_id,token_hash) VALUES (?,?)', (cur.lastrowid, hash_token('impostor-token')))
    for token, title in [('owner-dev-token', 'actual-owner-marker'), ('impostor-token', 'impostor-marker')]:
        result = client.post('/api/threads', headers=auth(token), json={'board_slug': 'tea_house', 'title': title, 'body_markdown': 'A message'})
        assert result.status_code == 201
    response = client.get('/api/threads/personal-export.md?target=cedar&mode=owner_context', headers=auth('rowan-dev-token'))
    assert response.status_code == 200
    assert 'actual-owner-marker' in response.text
    assert 'impostor-marker' not in response.text
    assert 'owner_participated' in response.text
    with get_connection() as conn:
        conn.execute("UPDATE users SET role='member' WHERE username='new_owner'")
    response = client.get('/api/threads/personal-export.md?target=cedar&mode=owner_context', headers=auth('rowan-dev-token'))
    assert 'actual-owner-marker' not in response.text


def test_r1_reserved_key_cannot_override_rejection():
    from app.services.frontmatter import split_markdown
    from app.services.library import actor_can_read
    actor = Actor(1, 'reset_guy', 'Alice', 'member', None, 'crew')
    for header in ['visible_to: [owner]\nvisible_to: [all_agents]\n_acl_error: []', '_acl_error: []\nvisible_to: [all_agents]']:
        meta, _ = split_markdown('---\n' + header + '\n---\nprivate')
        assert not actor_can_read(actor, Path('20_PROJECTS/test.md'), meta)
