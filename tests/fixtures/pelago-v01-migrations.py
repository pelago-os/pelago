"""Versioned additive migrations after the legacy schema bootstrap.

Never use executescript here: it commits the caller's transaction implicitly.
"""


def apply_migrations(conn):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")
    if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=1').fetchone():
        conn.execute("""CREATE TABLE IF NOT EXISTS forum_import_finalizations (
        id TEXT PRIMARY KEY, thread_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        source_path TEXT NOT NULL, imported_path TEXT NOT NULL UNIQUE,
        source_content BLOB, audit_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','complete')),
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        created_at TEXT NOT NULL, completed_at TEXT)""")
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (1,'durable_import_finalization')")
    if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
        conn.execute('''CREATE TABLE forum_mutation_audit (
            id TEXT PRIMARY KEY, event TEXT NOT NULL, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)''')
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (2,'durable_mutation_audit')")

    if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
        conn.execute("ALTER TABLE users ADD COLUMN membership_kind TEXT NOT NULL DEFAULT 'external' CHECK(membership_kind IN ('crew','external','system'))")
        # Membership is assigned explicitly by initialization or the local operator.
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (3,'explicit_crew_membership')")

    if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
        conn.execute("CREATE TABLE upload_owners (url TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id))")
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (4,'upload_ownership')")
