import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.auth import verify_password
from scripts.init_db import initialize


@pytest.mark.parametrize('demo,count', [(False, 1), (True, 6)])
def test_new_database_has_no_shared_credentials(tmp_path, demo, count):
    path = tmp_path / 'forum.db'
    initialize(path, 'owner', 'unique-long-password', demo=demo)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == count
        assert conn.execute('SELECT count(*) FROM tokens').fetchone()[0] == 0
        owner = conn.execute("SELECT role,membership_kind,password_hash FROM users WHERE username='owner'").fetchone()
        assert owner[:2] == ('admin', 'crew')
        assert verify_password('unique-long-password', owner[2])
        assert conn.execute("SELECT count(*) FROM users WHERE username!='owner' AND password_hash IS NOT NULL").fetchone()[0] == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ['forum.db']


def test_existing_file_is_unchanged(tmp_path):
    path = tmp_path / 'forum.db'
    path.write_bytes(b'existing database, do not touch')
    with pytest.raises(FileExistsError):
        initialize(path, 'owner', 'unique-long-password')
    assert path.read_bytes() == b'existing database, do not touch'


def test_concurrent_initializers_do_not_overwrite(tmp_path):
    path = tmp_path / 'forum.db'
    def run(name):
        try:
            initialize(path, name, 'unique-long-password')
            return name
        except FileExistsError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['owner_one', 'owner_two']))
    winners = [name for name in results if name]
    assert len(winners) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT username FROM users').fetchall() == [(winners[0],)]
    assert not list(tmp_path.glob('.pelago-init-*'))


def test_failed_build_leaves_no_database(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.init_db.hash_password', lambda _: (_ for _ in ()).throw(RuntimeError('failure')))
    with pytest.raises(RuntimeError):
        initialize(tmp_path / 'forum.db', 'owner', 'unique-long-password')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('username,password,demo', [('bad name','unique-long-password',False), ('owner','short',False), ('moderator','unique-long-password',True)])
def test_invalid_input_leaves_no_files(tmp_path, username, password, demo):
    with pytest.raises(ValueError):
        initialize(tmp_path / 'forum.db', username, password, demo=demo)
    assert list(tmp_path.iterdir()) == []


def test_initialized_owner_can_log_in_and_guest_has_no_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    path = tmp_path / 'forum.db'
    initialize(path, 'owner', 'unique-long-password', demo=True)
    monkeypatch.setenv('PELAGO_DB_PATH', str(path))
    monkeypatch.setattr(config, 'settings', config.Settings.from_env())
    with TestClient(app) as client:
        response = client.post('/api/login', json={'username': 'owner', 'password': 'unique-long-password'})
        assert response.status_code == 200
        assert client.post('/api/login', json={'username': 'external_guest', 'password': 'external_guest-dev-password'}).status_code == 401
