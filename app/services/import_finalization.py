"""Recover post-commit file/audit work without importing a post twice."""
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import uuid

from app.database import get_connection
from app.services.audit import append_audit


def enqueue_import(conn, path, content, thread_id, post_id, audit_payload, now):
    job_id = uuid.uuid4().hex
    destination = path.parent.parent / 'imported' / path.name
    if destination.exists() or conn.execute(
            'SELECT 1 FROM forum_import_finalizations WHERE imported_path=?',
            (str(destination),)).fetchone():
        destination = destination.with_name(f'{path.stem}-{job_id}.md')
    payload = dict(audit_payload, event_id=job_id, imported_path=str(destination))
    conn.execute('''INSERT INTO forum_import_finalizations
        (id,thread_id,post_id,source_path,imported_path,source_content,audit_json,created_at)
        VALUES (?,?,?,?,?,?,?,?)''',
        (job_id, thread_id, post_id, str(path), str(destination), content,
         json.dumps(payload, ensure_ascii=False), now))
    return job_id


def archive_submission(row):
    content = bytes(row['source_content'])
    destination = Path(row['imported_path'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Paths are reserved by the enqueue transaction, including the temporary file.
    temporary = destination.with_name(destination.name + '.' + row['id'] + '.pending')
    if destination.exists():
        if destination.read_bytes() != content:
            raise OSError('Archive content conflict; manual review required')
    else:
        with temporary.open('wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            raise OSError('Archive destination appeared; retry to verify content')
        os.replace(temporary, destination)
    source = Path(row['source_path'])
    # Do not remove a draft changed after the committed snapshot.
    if source.exists() and source.read_bytes() == content:
        source.unlink()


def finalize_import(job_id, audit_writer=None):
    with get_connection() as conn:
        # Serialize recovery across API and worker processes, including file I/O.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM forum_import_finalizations WHERE id=?', (job_id,)).fetchone()
        if row is None:
            return None
        if row['status'] != 'complete':
            try:
                archive_submission(row)
                # At-least-once JSONL delivery; event_id stays stable across a crash.
                (audit_writer or append_audit)('review.import', json.loads(row['audit_json']))
            except OSError as exc:
                conn.execute('''UPDATE forum_import_finalizations SET attempts=attempts+1,
                    last_error=? WHERE id=?''', (type(exc).__name__, job_id))
            else:
                conn.execute('''UPDATE forum_import_finalizations SET status='complete',
                    attempts=attempts+1,last_error=NULL,source_content=NULL,completed_at=? WHERE id=?''',
                    (datetime.now(UTC).isoformat(), job_id))
        result = dict(conn.execute('''SELECT id,thread_id,post_id,imported_path,status,
            attempts,last_error FROM forum_import_finalizations WHERE id=?''', (job_id,)).fetchone())
    return result


def recover_pending_imports(limit=50):
    with get_connection() as conn:
        ids = [row[0] for row in conn.execute("""SELECT id FROM forum_import_finalizations
            WHERE status='pending' ORDER BY attempts,created_at,id LIMIT ?""", (limit,))]
    return [finalize_import(job_id) for job_id in ids]
