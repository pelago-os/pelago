import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import os

import pytest

from test_forum_api import client, auth
from test_forum_automation import submission


def send(client, path):
    from app.routes.forum import encode_submission_id
    return client.post('/api/review/submissions/' + encode_submission_id(path) + '/import',
                       headers=auth('owner-dev-token'), json={})


@pytest.mark.parametrize('failure', ['archive', 'audit'])
def test_post_commit_recovery(client, tmp_path, monkeypatch, failure):
    from app.services import import_finalization as service
    from app.services import forum_imports as forum
    from app.database import get_connection
    path = submission(tmp_path, 'recover-' + failure)
    def fail(*args, **kwargs):
        raise OSError('injected disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(service if failure == 'archive' else forum,
                      'archive_submission' if failure == 'archive' else 'append_audit', fail)
        response = send(client, path)
    assert response.status_code == 200
    result = response.json()
    assert result['finalization_status'] == 'pending'
    # Recovery must not depend on a review source surviving worker cleanup.
    path.unlink(missing_ok=True)
    url = '/api/review/import-finalizations/' + result['finalization_id'] + '/retry'
    assert client.post(url, headers=auth('cedar-dev-token')).status_code == 403
    recovered = client.post(url, headers=auth('owner-dev-token')).json()
    assert recovered['finalization_status'] == 'complete'
    assert Path(recovered['imported_path']).exists()
    assert client.post(url, headers=auth('owner-dev-token')).json() == recovered
    with get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts WHERE thread_id=?',
                            (result['thread_id'],)).fetchone()[0] == 1
        row = conn.execute('SELECT * FROM forum_import_finalizations').fetchone()
        assert row['attempts'] == 2
        assert row['source_content'] is None


def test_archive_keeps_changed_draft(client, tmp_path, monkeypatch):
    from app.services import import_finalization as service
    original = service.archive_submission
    path = submission(tmp_path, 'changed')
    def change(row):
        path.write_text('A new draft must survive')
        return original(row)
    monkeypatch.setattr(service, 'archive_submission', change)
    assert send(client, path).status_code == 200
    assert path.read_text() == 'A new draft must survive'


def test_enqueue_failure_rolls_back_post_and_receipt(client, tmp_path, monkeypatch):
    from app.services import forum_imports as forum
    from app.database import get_connection
    path = submission(tmp_path, 'rollback')
    with get_connection() as conn:
        from app.services.forum_automation import ensure_import_ledger
        ensure_import_ledger(conn)
        before = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    def fail(*args):
        raise OSError('disk full before commit')
    monkeypatch.setattr(forum, 'enqueue_import', fail)
    with pytest.raises(OSError):
        send(client, path)
    with get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == before
        assert conn.execute('SELECT COUNT(*) FROM forum_import_receipts').fetchone()[0] == 0
    assert path.exists()


def test_migration_atomic_and_idempotent():
    from app.migrations import apply_migrations
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)')
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    apply_migrations(conn)
    conn.rollback()
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchall()
    apply_migrations(conn)
    conn.commit()
    apply_migrations(conn)
    assert conn.execute('SELECT version,name FROM schema_migrations').fetchall() == [
        (1, 'durable_import_finalization'), (2, 'durable_mutation_audit'),
        (3, 'explicit_crew_membership'), (4, 'upload_ownership')]
    conn.close()


def test_two_processes_import_same_identity_once(client, tmp_path):
    from app.routes.forum import encode_submission_id
    from app.database import get_connection
    a = submission(tmp_path, 'concurrent')
    b = a.with_name('copy.md')
    b.write_bytes(a.read_bytes())
    gate = tmp_path / 'start'
    code = '''
import sys,time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
while not Path(sys.argv[2]).exists(): time.sleep(.01)
r=TestClient(app).post('/api/review/submissions/'+sys.argv[1]+'/import',
    headers={'Authorization':'Bearer owner-dev-token'},json={})
print(r.status_code)
'''
    workers = [subprocess.Popen([sys.executable, '-c', code, encode_submission_id(p), str(gate)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=os.environ.copy()) for p in (a,b)]
    gate.touch()
    try:
        outputs = [worker.communicate(timeout=30) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    assert sorted(out.strip() for out, err in outputs) == ['200', '409'], outputs
    with get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM forum_import_finalizations').fetchone()[0] == 1


@pytest.mark.parametrize('operation', ['delete_thread', 'delete_post', 'convert'])
def test_mutation_audit_failure_is_recoverable(client, tmp_path, monkeypatch, operation):
    from test_forum_api import create_thread
    from app.routes import forum
    from app.services.audit import recover_mutation_audits
    from app.database import get_connection
    thread = create_thread(client)
    if operation == 'delete_post':
        reply = client.post('/api/threads/' + str(thread['id']) + '/posts',
            headers=auth('rowan-dev-token'), json={'body_markdown':'reply'})
        assert reply.status_code == 201
        post_id = reply.json()['posts'][-1]['id']
    elif operation == 'convert':
        target = create_thread(client)
    def fail(*args, **kwargs):
        raise OSError('injected audit failure')
    with monkeypatch.context() as patch:
        patch.setattr(forum, 'append_audit', fail)
        if operation == 'convert':
            response = client.post(f"/api/threads/{thread['id']}/convert-to-reply",
                headers=auth('owner-dev-token'), json={'target_thread_id':target['id']})
        elif operation == 'delete_post':
            response = client.delete(f'/api/posts/{post_id}', headers=auth('owner-dev-token'))
        else:
            response = client.delete(f"/api/threads/{thread['id']}", headers=auth('owner-dev-token'))
    assert response.status_code == 200, response.text
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM forum_mutation_audit').fetchone()
        assert row['delivered_at'] is None
        assert row['attempts'] == 1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert all(recover_mutation_audits().values())
    assert recover_mutation_audits() == {}




def test_crash_after_archive_can_resume(client, tmp_path, monkeypatch):
    from app.routes import forum
    from app.services import forum_imports
    from app.database import get_connection
    path = submission(tmp_path, 'crash')
    def crash(*args):
        raise SystemExit('simulated process death')
    # Call directly: BaseException escaping a TestClient can strand its portal.
    from app.auth import Actor
    from app.schemas import ReviewImportRequest
    with get_connection() as conn:
        owner = conn.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    with monkeypatch.context() as patch:
        patch.setattr(forum_imports, 'append_audit', crash)
        with pytest.raises(SystemExit):
            forum.import_review_submission(forum.encode_submission_id(path), ReviewImportRequest(),
                                           Actor(id=owner,username='owner',role='admin',display_name='Owner',persona_id=None, membership_kind='crew'))
    assert not path.exists()
    from app.services.import_finalization import recover_pending_imports
    assert recover_pending_imports()[0]['status'] == 'complete'


def test_two_processes_recover_one_job(client, tmp_path, monkeypatch):
    from app.services import import_finalization as service
    from app.database import get_connection
    path = submission(tmp_path, 'parallel-recovery')
    def fail(*args):
        raise OSError('archive unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(service, 'archive_submission', fail)
        result = send(client, path).json()
    gate = tmp_path / 'recovery-start'
    code = '''
import sys,time,json
from pathlib import Path
from app.services.import_finalization import finalize_import
while not Path(sys.argv[2]).exists(): time.sleep(.01)
print(finalize_import(sys.argv[1])['status'])
'''
    workers = [subprocess.Popen([sys.executable, '-c', code, result['finalization_id'], str(gate)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy()) for _ in range(2)]
    gate.touch()
    try:
        outputs = [worker.communicate(timeout=30) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    assert [out.strip() for out, err in outputs] == ['complete','complete'], outputs
    events = [json.loads(line) for line in (tmp_path / 'audit_log.jsonl').read_text().splitlines()]
    assert len([e for e in events if e.get('event_id') == result['finalization_id']]) == 1
    with get_connection() as conn:
        assert conn.execute('SELECT attempts FROM forum_import_finalizations').fetchone()[0] == 2


def test_mutation_audit_enqueue_failure_rolls_back_conversion(client, monkeypatch):
    from test_forum_api import create_thread
    from app.routes import forum
    from app.database import get_connection
    source, target = create_thread(client), create_thread(client)
    def fail(*args):
        raise OSError('canonical audit unavailable')
    monkeypatch.setattr(forum, 'queue_mutation_audit', fail)
    with pytest.raises(OSError):
        client.post(f"/api/threads/{source['id']}/convert-to-reply", headers=auth('owner-dev-token'),
                    json={'target_thread_id':target['id']})
    with get_connection() as conn:
        assert conn.execute('SELECT status FROM threads WHERE id=?', (source['id'],)).fetchone()[0] != 'deleted'
        assert conn.execute('SELECT thread_id FROM posts WHERE id=?',
                            (source['posts'][0]['id'],)).fetchone()[0] == source['id']
