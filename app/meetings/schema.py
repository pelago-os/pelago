"""Tables for meetings v0.2 (contract section 13). Old meeting tables are left untouched."""

_HOLDING = "('claimed','running','cancel_requested','quarantined')"
_NON_TERMINAL = "('queued','claimed','running','cancel_requested','quarantined')"

MEETINGS_V2_DDL = (
    """CREATE TABLE meetings (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        topic TEXT NOT NULL DEFAULT '',
        organizer_user_id INTEGER NOT NULL REFERENCES users(id),
        approved_by INTEGER REFERENCES users(id),
        state TEXT NOT NULL CHECK(state IN ('draft','running','paused','dormant','completed','closed')),
        exit_reason TEXT,
        rounds INTEGER NOT NULL,
        max_chars INTEGER NOT NULL,
        moderator_seat INTEGER,
        turn_timeout_s INTEGER NOT NULL,
        meeting_timeout_s INTEGER NOT NULL,
        run_seconds REAL NOT NULL DEFAULT 0,
        stale_after_s INTEGER NOT NULL,
        started_at REAL, paused_at REAL, dormant_at REAL, ended_at REAL,
        version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE meeting_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL CHECK(kind IN ('agent','human')),
        seat INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','left')),
        joined_from_round INTEGER NOT NULL DEFAULT 1,
        model TEXT,
        joined_at REAL NOT NULL, left_at REAL, left_by INTEGER REFERENCES users(id),
        UNIQUE(meeting_id, user_id), UNIQUE(meeting_id, seat))""",
    """CREATE TABLE meeting_slots (
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        round INTEGER NOT NULL, seat INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','filled','skipped')),
        filled_attempt_id TEXT, skip_reason TEXT, updated_at REAL NOT NULL,
        PRIMARY KEY(meeting_id, round, seat))""",
    """CREATE TABLE meeting_materials (
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        path TEXT NOT NULL, added_by INTEGER NOT NULL REFERENCES users(id), added_at REAL NOT NULL,
        PRIMARY KEY(meeting_id, path))""",
    """CREATE TABLE meeting_attempts (
        id TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        round INTEGER NOT NULL, seat INTEGER NOT NULL,
        participant_id INTEGER NOT NULL REFERENCES meeting_participants(id),
        agent_user_id INTEGER NOT NULL REFERENCES users(id),
        attempt INTEGER NOT NULL,
        state TEXT NOT NULL,
        cancel_reason TEXT,
        generation INTEGER,
        exec_host TEXT, exec_pid INTEGER, exec_pgid INTEGER, exec_started_at TEXT,
        queued_at REAL NOT NULL, claimed_at REAL, started_at REAL, cancel_requested_at REAL, ended_at REAL,
        raw_text TEXT, text TEXT, truncated INTEGER, char_count INTEGER,
        provider TEXT, model TEXT, provider_session_id TEXT,
        requested_model TEXT, fallback_used INTEGER, tool_surface TEXT, adapter_rev TEXT,
        error_code TEXT, error_detail TEXT,
        UNIQUE(meeting_id, round, seat, attempt))""",
    f"CREATE UNIQUE INDEX ux_meeting_attempt_agent ON meeting_attempts(agent_user_id) WHERE state IN {_HOLDING}",
    f"CREATE UNIQUE INDEX ux_meeting_attempt_meeting ON meeting_attempts(meeting_id) WHERE state IN {_HOLDING}",
    f"CREATE UNIQUE INDEX ux_meeting_attempt_slot ON meeting_attempts(meeting_id, round, seat) WHERE state IN {_NON_TERMINAL}",
    """CREATE TABLE meeting_requests (
        meeting_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(meeting_id, request_id))""",
    """CREATE TABLE meeting_snapshots (
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        taken_at REAL NOT NULL,
        view_json TEXT NOT NULL,
        PRIMARY KEY(meeting_id, user_id))""",
    """CREATE TABLE meeting_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL, at REAL NOT NULL,
        actor_user_id INTEGER, kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE meeting_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        kind TEXT NOT NULL, revision INTEGER NOT NULL,
        content_hash TEXT NOT NULL, body_md TEXT NOT NULL,
        state TEXT NOT NULL, exit_reason TEXT, created_at REAL NOT NULL,
        UNIQUE(meeting_id, kind, revision), UNIQUE(meeting_id, kind, content_hash))""",
    """CREATE TABLE meeting_exports (
        artifact_id INTEGER PRIMARY KEY REFERENCES meeting_artifacts(id),
        meeting_id TEXT NOT NULL, board TEXT NOT NULL,
        thread_id INTEGER NOT NULL, post_id INTEGER,
        created_by INTEGER NOT NULL, created_at REAL NOT NULL)""",
    """CREATE TABLE meeting_scheduler (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, renewed_at REAL)""",
    "CREATE INDEX ix_meeting_events_meeting ON meeting_events(meeting_id, id)",
    "CREATE INDEX ix_meeting_participants_user ON meeting_participants(user_id)",
)

# v0.2.3 (contract section 5): whether a meeting ends with a wrap-up turn, and whose. Included in public migration 5.
MEETING_WRAP_UP_DDL = (
    "ALTER TABLE meetings ADD COLUMN wrap_up INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE meetings ADD COLUMN wrap_up_seat INTEGER",
)

# v0.2.4 (contract 5.1 and 4): meeting modes, perspectives and the reason a meeting was rejected. Included in public migration 5.
MEETING_MODES_DDL = (
    ("meetings", "ALTER TABLE meetings ADD COLUMN mode TEXT NOT NULL DEFAULT 'discussion'"),
    ("meetings", "ALTER TABLE meetings ADD COLUMN decision_reason TEXT"),
    ("meeting_participants", "ALTER TABLE meeting_participants ADD COLUMN role TEXT"),
)

# Deleting a meeting only hides it (product requirement): the rows stay for audit and can be restored. Included in public migration 5.
MEETING_DELETE_DDL = (
    ("meetings", "ALTER TABLE meetings ADD COLUMN deleted_at REAL"),
    ("meetings", "ALTER TABLE meetings ADD COLUMN deleted_by INTEGER REFERENCES users(id)"),
)

# v0.2.1 (contract 6.1): what people in the room said between turns. Included in public migration 5.
MEETING_INTERJECTIONS_DDL = (
    """CREATE TABLE IF NOT EXISTS meeting_interjections (
        id TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL REFERENCES meetings(id),
        author_user_id INTEGER NOT NULL REFERENCES users(id),
        round INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_meeting_interjections_meeting ON meeting_interjections(meeting_id, created_at)",
)
