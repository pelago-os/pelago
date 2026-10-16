"""Local operator tool: manage members in an already initialized Pelago database."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import getpass
import re
import sqlite3
from pathlib import Path

from app import config
from app.auth import hash_password
from app.services.audit import queue_mutation_audit


def manage(path: Path, username: str, *, create: bool = False,
           role: str | None = None, membership: str | None = None,
           display_name: str | None = None, password: str | None = None, approve: bool = False) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9_]{2,31}', username):
        raise ValueError('Username must be 3-32 lowercase letters, digits or underscores, starting with a letter.')
    if role is not None and role not in {'admin', 'moderator', 'member', 'guest'}:
        raise ValueError('Invalid role.')
    if membership is not None and membership not in {'crew', 'external'}:
        raise ValueError('Invalid membership.')
    if display_name is not None and not 1 <= len(display_name.strip()) <= 100:
        raise ValueError('Display name must be 1-100 characters.')
    if password is not None and not 12 <= len(password) <= 200:
        raise ValueError('Password must be 12-200 characters.')
    if not create and not approve and all(v is None for v in (role, membership, display_name, password)):
        raise ValueError('No changes requested.')
    # mode=rw never creates a missing DB. Do not run migrations or reseed here.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if create and old is not None:
            raise ValueError('User already exists; use update.')
        if not create and old is None:
            raise ValueError('User does not exist; use add.')
        if old is not None and (old['status'] != 'active' and not (approve and old['status'] == 'pending') or old['membership_kind'] == 'system'):
            raise ValueError('This tool only updates active, non-system accounts.')
        if approve and (old is None or old["status"] != "pending"):
            raise ValueError("Only a pending account can be approved.")
        new_role = role or (old['role'] if old else 'member')
        new_membership = membership or (old['membership_kind'] if old else 'external')
        if new_role in {'admin', 'moderator'} and new_membership != 'crew':
            raise ValueError('Administrators and moderators must explicitly be crew.')
        if new_role == 'admin' and password is None and not (old and old['password_hash']):
            raise ValueError('Administrator needs a password; use --set-password.')
        if old and old['role'] == 'admin' and old['membership_kind'] == 'crew' and (new_role != 'admin' or new_membership != 'crew'):
            count = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND membership_kind='crew' AND status='active' AND password_hash IS NOT NULL").fetchone()[0]
            if count <= 1:
                raise ValueError('Cannot demote the last active crew administrator.')
        name = display_name.strip() if display_name is not None else (old['display_name'] if old else username)
        if create:
            conn.execute('INSERT INTO users (username,display_name,role,membership_kind,status) VALUES (?,?,?,?,?)',
                         (username, name, new_role, new_membership, 'active'))
        else:
            conn.execute('UPDATE users SET display_name=?,role=?,membership_kind=? WHERE id=?',
                         (name, new_role, new_membership, old['id']))
        user_id = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()[0]
        if approve:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user_id,))
        now = datetime.now(UTC).isoformat()
        if password is not None:
            conn.execute('UPDATE users SET password_hash=?,password_changed_at=? WHERE id=?', (hash_password(password), now, user_id))
        access_changed = old and (new_role != old['role'] or new_membership != old['membership_kind'])
        if password is not None or access_changed:
            conn.execute('UPDATE tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (now, user_id))
        queue_mutation_audit(conn, 'local_member_management', {
            'actor': 'local_operator', 'username': username, 'created': create, 'approved': approve,
            'role': new_role, 'membership_kind': new_membership, 'password_changed': password is not None,
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['add', 'update', 'approve'])
    parser.add_argument('username')
    parser.add_argument('--role', choices=['admin', 'moderator', 'member', 'guest'])
    parser.add_argument('--membership', choices=['crew', 'external'])
    parser.add_argument('--display-name')
    parser.add_argument('--set-password', action='store_true')
    args = parser.parse_args(argv)
    password = None
    if args.set_password:
        password = getpass.getpass('New password (12+ characters): ')
        if password != getpass.getpass('Confirm password: '):
            parser.error('Passwords do not match.')
    try:
        manage(config.settings.db_path, args.username, create=args.action == 'add',
               role=args.role, membership=args.membership, display_name=args.display_name, password=password, approve=args.action == 'approve')
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(f'Updated {args.username}. Password/access changes revoke existing sessions; sign in again for a fresh token.')


if __name__ == '__main__':
    main()
