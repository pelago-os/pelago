from __future__ import annotations

import logging
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import config


def append_audit(event: str, payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or config.settings.audit_log_path
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event": event,
        "created_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def queue_mutation_audit(conn, event: str, payload: dict[str, Any]) -> str:
    """Canonical audit record committed atomically with the selected mutation."""
    event_id = uuid.uuid4().hex
    conn.execute('''INSERT INTO forum_mutation_audit(id,event,payload_json)
        VALUES (?,?,?)''', (event_id, event, json.dumps(dict(payload, event_id=event_id), ensure_ascii=False)))
    return event_id


def deliver_mutation_audit(event_id: str, writer=None) -> bool:
    # Mutation has already committed. Mirror failure must not invite a duplicate retry.
    try:
        return _deliver_mutation_audit(event_id, writer)
    except Exception as exc:
        logging.getLogger(__name__).error(
            "audit mirror delivery failed event_id=%s error_type=%s", event_id, type(exc).__name__
        )
        return False


def _deliver_mutation_audit(event_id: str, writer=None) -> bool:
    from app.database import get_connection
    with get_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM forum_mutation_audit WHERE id=?', (event_id,)).fetchone()
        if row is None:
            return False
        if row['delivered_at']:
            return True
        try:
            (writer or append_audit)(row['event'], json.loads(row['payload_json']))
        except Exception as exc:
            conn.execute('''UPDATE forum_mutation_audit SET attempts=attempts+1,last_error=?
                WHERE id=?''', (type(exc).__name__, event_id))
            return False
        conn.execute('''UPDATE forum_mutation_audit SET attempts=attempts+1,last_error=NULL,
            delivered_at=? WHERE id=?''', (datetime.now(UTC).isoformat(), event_id))
    return True


def recover_mutation_audits(limit=50):
    from app.database import get_connection
    with get_connection() as conn:
        ids = [row[0] for row in conn.execute('''SELECT id FROM forum_mutation_audit
            WHERE delivered_at IS NULL ORDER BY attempts,created_at,id LIMIT ?''', (limit,))]
    return {event_id: deliver_mutation_audit(event_id) for event_id in ids}
