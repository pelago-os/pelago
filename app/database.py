from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or config.settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return conn.execute(sql, tuple(params))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  persona_id TEXT,
  avatar_color TEXT,
  avatar_emoji TEXT,
  avatar_image TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS boards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  icon TEXT NOT NULL DEFAULT '',
  digest_level TEXT NOT NULL DEFAULT 'snippet',
  sort_order INTEGER NOT NULL DEFAULT 0,
  is_archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  title TEXT NOT NULL,
  author_user_id INTEGER NOT NULL REFERENCES users(id),
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  submitter_identity_id INTEGER REFERENCES users(id),
  source_channel TEXT,
  source_message_id TEXT,
  source_submission_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  is_pinned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  parent_post_id INTEGER REFERENCES posts(id),
  author_user_id INTEGER NOT NULL REFERENCES users(id),
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  quoted_excerpt TEXT,
  body_markdown TEXT NOT NULL,
  body_html TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'visible',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS post_likes (
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_board_updated ON threads(board_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_thread_created ON posts(thread_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_post_likes_user ON post_likes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);

CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code_hash TEXT NOT NULL UNIQUE,
  created_by_user_id INTEGER REFERENCES users(id),
  default_role TEXT NOT NULL DEFAULT 'guest',
  allowed_boards TEXT NOT NULL DEFAULT '[]',
  digest_scope TEXT,
  requires_approval INTEGER NOT NULL DEFAULT 1,
  max_uses INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  revoked_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS meeting_agent_sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  model_route TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  cwd TEXT,
  command TEXT,
  permission_mode TEXT NOT NULL DEFAULT 'default',
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  provider_session_id TEXT,
  provider_session_started INTEGER NOT NULL DEFAULT 0,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_heartbeat_at TEXT,
  exit_code INTEGER,
  exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS meeting_session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES meeting_agent_sessions(id) ON DELETE CASCADE,
  event_index INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(session_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_meeting_sessions_status ON meeting_agent_sessions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_events_session ON meeting_session_events(session_id, event_index ASC);

CREATE TABLE IF NOT EXISTS meeting_session_approvals (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES meeting_agent_sessions(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending',
  action_type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL,
  decided_by_user_id INTEGER REFERENCES users(id),
  decision_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_meeting_approvals_session ON meeting_session_approvals(session_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS meeting_proposals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  meeting_type TEXT NOT NULL DEFAULT 'project_sync',
  priority TEXT NOT NULL DEFAULT 'P3',
  status TEXT NOT NULL DEFAULT 'pending',
  target_agent_ids_json TEXT NOT NULL DEFAULT '[]',
  suggested_adapter TEXT NOT NULL DEFAULT '',
  suggested_model_route TEXT NOT NULL DEFAULT '',
  max_turns INTEGER NOT NULL DEFAULT 6,
  context_markdown TEXT NOT NULL DEFAULT '',
  related_session_id TEXT REFERENCES meeting_agent_sessions(id) ON DELETE SET NULL,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  decided_by_user_id INTEGER REFERENCES users(id),
  decision_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_meeting_proposals_status ON meeting_proposals(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_proposals_created_by ON meeting_proposals(created_by_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS meeting_proposal_participants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL REFERENCES meeting_proposals(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL DEFAULT '',
  model_route TEXT NOT NULL DEFAULT '',
  participant_role TEXT NOT NULL DEFAULT 'participant',
  turn_budget INTEGER NOT NULL DEFAULT 2,
  status TEXT NOT NULL DEFAULT 'invited',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(proposal_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_meeting_proposal_participants_proposal ON meeting_proposal_participants(proposal_id, id ASC);

CREATE TABLE IF NOT EXISTS meeting_roundtables (
  id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES meeting_proposals(id) ON DELETE CASCADE,
  organizer_agent_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  meeting_mode TEXT NOT NULL DEFAULT 'freeform',
  protocol_version TEXT NOT NULL DEFAULT 'legacy-v1',
  protocol_cycle INTEGER NOT NULL DEFAULT 1,
  current_stage TEXT NOT NULL DEFAULT 'discussion',
  protocol_snapshot_json TEXT NOT NULL DEFAULT '{}',
  opening_prompt TEXT NOT NULL DEFAULT '',
  max_turns INTEGER NOT NULL DEFAULT 6,
  current_round INTEGER NOT NULL DEFAULT 1,
  next_participant_index INTEGER NOT NULL DEFAULT 0,
  turns_completed INTEGER NOT NULL DEFAULT 0,
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT,
  exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_meeting_roundtables_proposal ON meeting_roundtables(proposal_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_roundtables_status ON meeting_roundtables(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS meeting_roundtable_participants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL DEFAULT '',
  model_route TEXT NOT NULL DEFAULT '',
  participant_role TEXT NOT NULL DEFAULT 'participant',
  perspective_role TEXT NOT NULL DEFAULT '',
  private_instruction TEXT NOT NULL DEFAULT '',
  turn_budget INTEGER NOT NULL DEFAULT 2,
  turns_completed INTEGER NOT NULL DEFAULT 0,
  session_id TEXT REFERENCES meeting_agent_sessions(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'active',
  absence_reason TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  dispatch_status TEXT NOT NULL DEFAULT 'idle',
  dispatch_started_at TEXT,
  dispatch_deadline_at TEXT,
  dispatch_completed_at TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(roundtable_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_meeting_roundtable_participants_roundtable ON meeting_roundtable_participants(roundtable_id, sort_order ASC);

CREATE TABLE IF NOT EXISTS meeting_roundtable_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
  participant_id INTEGER NOT NULL REFERENCES meeting_roundtable_participants(id) ON DELETE CASCADE,
  turn_index INTEGER NOT NULL,
  round_number INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT REFERENCES meeting_agent_sessions(id) ON DELETE SET NULL,
  stage_key TEXT NOT NULL DEFAULT 'discussion',
  stage_cycle INTEGER NOT NULL DEFAULT 1,
  visibility_scope TEXT NOT NULL DEFAULT 'full',
  prompt TEXT NOT NULL DEFAULT '',
  response TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'completed',
  error_message TEXT,
  interjection_scope TEXT NOT NULL DEFAULT '',
  target_agent_id TEXT,
  target_stage_key TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT,
  UNIQUE(roundtable_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_meeting_roundtable_turns_roundtable ON meeting_roundtable_turns(roundtable_id, turn_index ASC);

CREATE TABLE IF NOT EXISTS meeting_roundtable_interjection_deliveries (
  interjection_turn_id INTEGER NOT NULL REFERENCES meeting_roundtable_turns(id) ON DELETE CASCADE,
  participant_id INTEGER NOT NULL REFERENCES meeting_roundtable_participants(id) ON DELETE CASCADE,
  delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(interjection_turn_id, participant_id)
);

CREATE TABLE IF NOT EXISTS meeting_roundtable_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
  stage_key TEXT NOT NULL,
  stage_cycle INTEGER NOT NULL DEFAULT 1,
  artifact_type TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  created_by_agent_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_roundtable_artifacts_roundtable
ON meeting_roundtable_artifacts(roundtable_id, id ASC);

CREATE TABLE IF NOT EXISTS meeting_machine_jobs (
  id TEXT PRIMARY KEY,
  roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
  requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'queued',
  rounds INTEGER NOT NULL DEFAULT 1,
  soft_deadline_seconds INTEGER NOT NULL DEFAULT 120,
  continuation_note TEXT NOT NULL DEFAULT '',
  current_stage TEXT NOT NULL DEFAULT '',
  protocol_cycle INTEGER NOT NULL DEFAULT 1,
  turns_run INTEGER NOT NULL DEFAULT 0,
  brief_markdown TEXT NOT NULL DEFAULT '',
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_machine_jobs_roundtable
ON meeting_machine_jobs(roundtable_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_machine_jobs_status
ON meeting_machine_jobs(status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_machine_jobs_one_active
ON meeting_machine_jobs(roundtable_id)
WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS meeting_detached_calls (
  id TEXT PRIMARY KEY,
  roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
  participant_id INTEGER NOT NULL REFERENCES meeting_roundtable_participants(id) ON DELETE CASCADE,
  session_id TEXT REFERENCES meeting_agent_sessions(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'detached',
  reason TEXT NOT NULL DEFAULT '',
  error_message TEXT,
  detached_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meeting_detached_calls_status
ON meeting_detached_calls(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS meeting_worker_sites (
  site_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'provisioned',
  worker_version TEXT NOT NULL DEFAULT '',
  registration_id TEXT,
  last_heartbeat_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS meeting_worker_instances (
  instance_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  route_key TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'unavailable',
  detail TEXT,
  last_heartbeat_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_worker_instances_site
ON meeting_worker_instances(site_id, status, instance_id);

CREATE TABLE IF NOT EXISTS meeting_worker_tasks (
  id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
  instance_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  route_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  roundtable_id TEXT,
  stage_key TEXT NOT NULL,
  context_generation INTEGER NOT NULL DEFAULT 1,
  prompt TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL DEFAULT 30,
  max_output_chars INTEGER NOT NULL DEFAULT 2000,
  permission_policy TEXT NOT NULL DEFAULT 'deny_interactive',
  status TEXT NOT NULL DEFAULT 'queued',
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  lease_id TEXT,
  lease_expires_at TEXT,
  completion_id TEXT,
  failure_id TEXT,
  turn_id TEXT,
  result_body TEXT,
  provider_session_ref TEXT,
  diagnostics_json TEXT NOT NULL DEFAULT '{}',
  failure_category TEXT,
  failure_message TEXT,
  retryable INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_worker_tasks_lease
ON meeting_worker_tasks(site_id, status, instance_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_worker_tasks_completion
ON meeting_worker_tasks(completion_id)
WHERE completion_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS meeting_worker_windows (
  id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
  instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL REFERENCES meeting_agent_sessions(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  route_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  poll_interval_seconds INTEGER NOT NULL DEFAULT 10,
  idle_timeout_seconds INTEGER NOT NULL DEFAULT 180,
  hard_stop_at TEXT NOT NULL,
  last_task_completed_at TEXT,
  close_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_meeting_worker_windows_active
ON meeting_worker_windows(site_id, instance_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_meeting_worker_windows_session
ON meeting_worker_windows(session_id, status);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    ensure_schema_migrations(conn)
    from app.migrations import apply_migrations
    apply_migrations(conn)
    conn.commit()


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "password_changed_at" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
    if "avatar_emoji" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_emoji TEXT")
    if "avatar_image" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_image TEXT")

    board_columns = {row["name"] for row in conn.execute("PRAGMA table_info(boards)").fetchall()}
    if "digest_level" not in board_columns:
        conn.execute("ALTER TABLE boards ADD COLUMN digest_level TEXT NOT NULL DEFAULT 'snippet'")

    thread_columns = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
    if "submitter_identity_id" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN submitter_identity_id INTEGER REFERENCES users(id)")
    if "source_channel" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN source_channel TEXT")
    if "source_message_id" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN source_message_id TEXT")
    if "source_submission_id" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN source_submission_id TEXT")
    if "edited_at" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN edited_at TEXT")
    if "edited_by_user_id" not in thread_columns:
        conn.execute("ALTER TABLE threads ADD COLUMN edited_by_user_id INTEGER REFERENCES users(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_submitter ON threads(submitter_identity_id, updated_at DESC)")

    post_columns = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "parent_post_id" not in post_columns:
        conn.execute("ALTER TABLE posts ADD COLUMN parent_post_id INTEGER REFERENCES posts(id)")
    if "quoted_excerpt" not in post_columns:
        conn.execute("ALTER TABLE posts ADD COLUMN quoted_excerpt TEXT")
    if "edited_at" not in post_columns:
        conn.execute("ALTER TABLE posts ADD COLUMN edited_at TEXT")
    if "edited_by_user_id" not in post_columns:
        conn.execute("ALTER TABLE posts ADD COLUMN edited_by_user_id INTEGER REFERENCES users(id)")

    meeting_session_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(meeting_agent_sessions)").fetchall()
    }
    if "permission_mode" not in meeting_session_columns:
        conn.execute("ALTER TABLE meeting_agent_sessions ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'default'")
    if "allowed_tools_json" not in meeting_session_columns:
        conn.execute("ALTER TABLE meeting_agent_sessions ADD COLUMN allowed_tools_json TEXT NOT NULL DEFAULT '[]'")
    if "provider_session_id" not in meeting_session_columns:
        conn.execute("ALTER TABLE meeting_agent_sessions ADD COLUMN provider_session_id TEXT")
    if "provider_session_started" not in meeting_session_columns:
        conn.execute("ALTER TABLE meeting_agent_sessions ADD COLUMN provider_session_started INTEGER NOT NULL DEFAULT 0")

    roundtable_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(meeting_roundtables)").fetchall()
    }
    if "meeting_mode" not in roundtable_columns:
        conn.execute("ALTER TABLE meeting_roundtables ADD COLUMN meeting_mode TEXT NOT NULL DEFAULT 'freeform'")
    if "protocol_version" not in roundtable_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtables ADD COLUMN protocol_version TEXT NOT NULL DEFAULT 'legacy-v1'"
        )
    if "protocol_cycle" not in roundtable_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtables ADD COLUMN protocol_cycle INTEGER NOT NULL DEFAULT 1"
        )
    if "current_stage" not in roundtable_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtables ADD COLUMN current_stage TEXT NOT NULL DEFAULT 'discussion'"
        )
    if "protocol_snapshot_json" not in roundtable_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtables ADD COLUMN protocol_snapshot_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "organizer_agent_id" not in roundtable_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtables ADD COLUMN organizer_agent_id TEXT NOT NULL DEFAULT ''"
        )

    participant_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(meeting_roundtable_participants)").fetchall()
    }
    if "perspective_role" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN perspective_role TEXT NOT NULL DEFAULT ''"
        )
    if "private_instruction" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN private_instruction TEXT NOT NULL DEFAULT ''"
        )
    if "absence_reason" not in participant_columns:
        conn.execute("ALTER TABLE meeting_roundtable_participants ADD COLUMN absence_reason TEXT")
    if "consecutive_failures" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "dispatch_status" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN dispatch_status TEXT NOT NULL DEFAULT 'idle'"
        )
    if "dispatch_started_at" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN dispatch_started_at TEXT"
        )
    if "dispatch_deadline_at" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN dispatch_deadline_at TEXT"
        )
    if "dispatch_completed_at" not in participant_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_participants ADD COLUMN dispatch_completed_at TEXT"
        )

    turn_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(meeting_roundtable_turns)").fetchall()
    }
    if "stage_key" not in turn_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_turns ADD COLUMN stage_key TEXT NOT NULL DEFAULT 'discussion'"
        )
    if "stage_cycle" not in turn_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_turns ADD COLUMN stage_cycle INTEGER NOT NULL DEFAULT 1"
        )
    if "visibility_scope" not in turn_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_turns ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'full'"
        )
    if "interjection_scope" not in turn_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_turns ADD COLUMN interjection_scope TEXT NOT NULL DEFAULT ''"
        )
    if "target_agent_id" not in turn_columns:
        conn.execute("ALTER TABLE meeting_roundtable_turns ADD COLUMN target_agent_id TEXT")
    if "target_stage_key" not in turn_columns:
        conn.execute("ALTER TABLE meeting_roundtable_turns ADD COLUMN target_stage_key TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_roundtable_interjection_deliveries (
          interjection_turn_id INTEGER NOT NULL REFERENCES meeting_roundtable_turns(id) ON DELETE CASCADE,
          participant_id INTEGER NOT NULL REFERENCES meeting_roundtable_participants(id) ON DELETE CASCADE,
          delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          PRIMARY KEY(interjection_turn_id, participant_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_roundtable_artifacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
          stage_key TEXT NOT NULL,
          stage_cycle INTEGER NOT NULL DEFAULT 1,
          artifact_type TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          created_by_agent_id TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    artifact_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(meeting_roundtable_artifacts)").fetchall()
    }
    if "stage_cycle" not in artifact_columns:
        conn.execute(
            "ALTER TABLE meeting_roundtable_artifacts ADD COLUMN stage_cycle INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_meeting_roundtable_artifacts_roundtable
        ON meeting_roundtable_artifacts(roundtable_id, id ASC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_machine_jobs (
          id TEXT PRIMARY KEY,
          roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
          requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
          status TEXT NOT NULL DEFAULT 'queued',
          rounds INTEGER NOT NULL DEFAULT 1,
          continuation_note TEXT NOT NULL DEFAULT '',
          current_stage TEXT NOT NULL DEFAULT '',
          protocol_cycle INTEGER NOT NULL DEFAULT 1,
          turns_run INTEGER NOT NULL DEFAULT 0,
          brief_markdown TEXT NOT NULL DEFAULT '',
          error_message TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          started_at TEXT,
          completed_at TEXT,
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_meeting_machine_jobs_roundtable
        ON meeting_machine_jobs(roundtable_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_meeting_machine_jobs_status
        ON meeting_machine_jobs(status, updated_at DESC)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_machine_jobs_one_active
        ON meeting_machine_jobs(roundtable_id)
        WHERE status IN ('queued', 'running')
        """
    )
    job_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(meeting_machine_jobs)").fetchall()
    }
    if "soft_deadline_seconds" not in job_columns:
        conn.execute(
            "ALTER TABLE meeting_machine_jobs ADD COLUMN soft_deadline_seconds INTEGER NOT NULL DEFAULT 120"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_detached_calls (
          id TEXT PRIMARY KEY,
          roundtable_id TEXT NOT NULL REFERENCES meeting_roundtables(id) ON DELETE CASCADE,
          participant_id INTEGER NOT NULL REFERENCES meeting_roundtable_participants(id) ON DELETE CASCADE,
          session_id TEXT REFERENCES meeting_agent_sessions(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'detached',
          reason TEXT NOT NULL DEFAULT '',
          error_message TEXT,
          detached_at TEXT NOT NULL,
          completed_at TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_meeting_detached_calls_status
        ON meeting_detached_calls(status, updated_at DESC)
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meeting_worker_sites (
          site_id TEXT PRIMARY KEY,
          worker_id TEXT NOT NULL UNIQUE,
          token_hash TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'provisioned',
          worker_version TEXT NOT NULL DEFAULT '',
          registration_id TEXT,
          last_heartbeat_at TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS meeting_worker_instances (
          instance_id TEXT PRIMARY KEY,
          site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL,
          adapter TEXT NOT NULL,
          route_key TEXT NOT NULL,
          capabilities_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'unavailable',
          detail TEXT,
          last_heartbeat_at TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_worker_instances_site
        ON meeting_worker_instances(site_id, status, instance_id);
        CREATE TABLE IF NOT EXISTS meeting_worker_tasks (
          id TEXT PRIMARY KEY,
          site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
          instance_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          adapter TEXT NOT NULL,
          route_key TEXT NOT NULL,
          session_id TEXT NOT NULL,
          roundtable_id TEXT,
          stage_key TEXT NOT NULL,
          context_generation INTEGER NOT NULL DEFAULT 1,
          prompt TEXT NOT NULL,
          timeout_seconds INTEGER NOT NULL DEFAULT 30,
          max_output_chars INTEGER NOT NULL DEFAULT 2000,
          permission_policy TEXT NOT NULL DEFAULT 'deny_interactive',
          status TEXT NOT NULL DEFAULT 'queued',
          attempt INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 2,
          lease_id TEXT,
          lease_expires_at TEXT,
          completion_id TEXT,
          failure_id TEXT,
          turn_id TEXT,
          result_body TEXT,
          provider_session_ref TEXT,
          diagnostics_json TEXT NOT NULL DEFAULT '{}',
          failure_category TEXT,
          failure_message TEXT,
          retryable INTEGER,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          started_at TEXT,
          completed_at TEXT,
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_worker_tasks_lease
        ON meeting_worker_tasks(site_id, status, instance_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_worker_tasks_completion
        ON meeting_worker_tasks(completion_id)
        WHERE completion_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS meeting_worker_windows (
          id TEXT PRIMARY KEY,
          site_id TEXT NOT NULL REFERENCES meeting_worker_sites(site_id) ON DELETE CASCADE,
          instance_id TEXT NOT NULL,
          session_id TEXT NOT NULL REFERENCES meeting_agent_sessions(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL,
          adapter TEXT NOT NULL,
          route_key TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          poll_interval_seconds INTEGER NOT NULL DEFAULT 10,
          idle_timeout_seconds INTEGER NOT NULL DEFAULT 180,
          hard_stop_at TEXT NOT NULL,
          last_task_completed_at TEXT,
          close_reason TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_worker_windows_active
        ON meeting_worker_windows(site_id, instance_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_meeting_worker_windows_session
        ON meeting_worker_windows(session_id, status);
        """
    )
