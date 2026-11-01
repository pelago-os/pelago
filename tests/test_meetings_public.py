"""Public-install boundaries; no private database or live provider is used."""
import sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.meetings.adapter_config import _parse, AdapterConfigError
from test_meetings import env, create, act, drive, state, tok  # noqa: F401


@pytest.mark.parametrize('adapter', ['openclaw', 'remote_hermes', 'antigravity', 'codex', 'hermes', 'claude_code', 'unknown'])
def test_unavailable_adapter_is_rejected(adapter):
    with pytest.raises(AdapterConfigError):
        _parse({'members': {'agent': {'adapter': adapter, 'command': ['/bin/false'], 'cwd': '/tmp'}}})


def test_disabled_feature_has_no_routes_or_runner(env, monkeypatch):
    from app.main import app
    from app.meetings.runner import Runner
    monkeypatch.setenv('PELAGO_MEETINGS_ENABLED', '0')
    monkeypatch.setattr(Runner, 'start', lambda _: pytest.fail('disabled runner was started'))
    with TestClient(app) as client:
        for path in ('/api/meetings', '/api/meetings/adapters'):
            assert client.get(path, headers=tok('owner')).status_code == 404
        assert client.get('/api/station/meta').json()['meetings']['enabled'] is False


def test_mock_badge_survives_database_and_restart_view(env):
    m = create(env)
    act(env, m['id'], 'start')
    drive(env, lambda: state(env, m['id'])['state'] == 'completed')
    assert all(a['support_tier'] == 'mock' for a in state(env, m['id'])['attempts'])
    minutes = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok('owner')).json()
    assert 'Mock 演示' in str(minutes)
    members = env.client.get('/api/meetings/adapters', headers=tok('owner')).json()['members']
    assert all(m['support_tier'] == 'mock' for m in members)


def test_public_v01_upgrade_rolls_back_and_preserves_data(monkeypatch, tmp_path):
    # Exact public v0.1 migration function is a checked-in fixture, not the home database.
    import runpy
    from app import migrations, database
    old = runpy.run_path(str(Path(__file__).parent / 'fixtures/pelago-v01-migrations.py'))['apply_migrations']
    conn = sqlite3.connect(tmp_path / 'upgrade.db')
    conn.row_factory = sqlite3.Row
    with monkeypatch.context() as patch:
        patch.setattr(migrations, 'apply_migrations', old)
        database.init_schema(conn)
    conn.execute("INSERT INTO users(username,display_name,role,status,membership_kind) VALUES ('old_member','Existing member','member','active','crew')")
    conn.commit()
    before = tuple(conn.execute('SELECT * FROM users').fetchone())
    from app.meetings import schema
    with monkeypatch.context() as patch:
        patch.setattr(schema, 'MEETINGS_V2_DDL', schema.MEETINGS_V2_DDL + ('INVALID SQL',))
        with pytest.raises(sqlite3.OperationalError):
            migrations.apply_migrations(conn)
        conn.rollback()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='meetings'").fetchone() is None
    assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 4
    for _ in range(2):
        migrations.apply_migrations(conn)
        conn.commit()
    assert tuple(conn.execute('SELECT * FROM users').fetchone()) == before
    assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 5
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='meeting_project_drafts'").fetchone() is None
    conn.close()


def test_enabled_lifespan_drives_and_stops_real_mock_children(env):
    import time
    from app.main import app
    with TestClient(app) as client:
        env.client = client
        meeting = create(env, rounds=1)
        assert act(env, meeting['id'], 'start').status_code == 200
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            current = state(env, meeting['id'])
            if current['state'] == 'completed':
                break
            time.sleep(0.05)
        assert current['state'] == 'completed'
        assert len([a for a in current['attempts'] if a['state'] == 'accepted']) == 2
    # The shared fixture's process-group scan verifies no children remain.


def test_private_migration_five_is_refused():
    from app.migrations import apply_migrations
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)')
    apply_migrations(conn)
    conn.execute("UPDATE schema_migrations SET name='meetings_v2' WHERE version=5")
    conn.commit()
    with pytest.raises(RuntimeError, match='lineage'):
        apply_migrations(conn)
    conn.close()
