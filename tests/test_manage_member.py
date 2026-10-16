import sqlite3

import pytest

from app.auth import hash_token, verify_password
from scripts.init_db import initialize
from scripts.manage_member import manage


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'forum.db'
    initialize(path, 'owner', 'unique-long-password', demo=True)
    return path


def test_provision_demo_and_revoke_old_tokens(db):
    with sqlite3.connect(db) as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='reset_guy'").fetchone()[0]
        conn.execute('INSERT INTO tokens(user_id,token_hash) VALUES (?,?)', (uid, hash_token('old-session')))
    manage(db, 'reset_guy', password='new-unique-password')
    with sqlite3.connect(db) as conn:
        assert verify_password('new-unique-password', conn.execute('SELECT password_hash FROM users WHERE id=?', (uid,)).fetchone()[0])
        assert conn.execute('SELECT revoked_at FROM tokens WHERE user_id=?', (uid,)).fetchone()[0]
        payload = conn.execute('SELECT payload_json FROM forum_mutation_audit').fetchone()[0]
        assert 'new-unique-password' not in payload


def test_add_defaults_external_and_can_explicitly_promote(db):
    manage(db, 'new_agent', create=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT membership_kind,password_hash FROM users WHERE username='new_agent'").fetchone() == ('external', None)
    manage(db, 'new_agent', membership='crew', role='moderator')
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT role,membership_kind FROM users WHERE username='new_agent'").fetchone() == ('moderator', 'crew')


def test_last_admin_is_preserved(db):
    with pytest.raises(ValueError, match='last active'):
        manage(db, 'owner', role='member')
    manage(db, 'second_owner', create=True, role='admin', membership='crew', password='second-owner-password')
    manage(db, 'owner', role='member')


def test_failed_audit_rolls_back(db, monkeypatch):
    monkeypatch.setattr('scripts.manage_member.queue_mutation_audit', lambda *args: (_ for _ in ()).throw(RuntimeError('audit failure')))
    with pytest.raises(RuntimeError):
        manage(db, 'new_agent', create=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM users WHERE username='new_agent'").fetchone()[0] == 0


def test_missing_database_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        manage(path, 'new_agent', create=True)
    assert not path.exists()


def test_duplicate_and_unknown_users_refused(db):
    with pytest.raises(ValueError, match='already exists'):
        manage(db, 'owner', create=True)
    with pytest.raises(ValueError, match='does not exist'):
        manage(db, 'missing_user', role='member')
    with pytest.raises(ValueError, match='explicitly be crew'):
        manage(db, 'new_admin', create=True, role='admin')
