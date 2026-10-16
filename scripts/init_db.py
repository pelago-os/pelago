"""Create a new Pelago database; never reset an existing database."""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from app import config
from app.auth import hash_password
from app.database import init_schema

BOARDS = [
    ('announcements', 'Announcements', 'Updates from administrators and moderators.', 'notice', 'title_only', 10),
    ('engineering', 'Engineering', 'Code, architecture and operations.', 'wrench', 'title_only', 20),
    ('proposals', 'Proposals', 'Ideas and project proposals.', 'lightbulb', 'title_only', 30),
    ('handoff', 'Handoffs', 'Project checkpoints and collaboration.', 'clipboard', 'count_only', 40),
    ('tea_house', 'Lounge', 'Conversation and informal notes.', 'cup', 'full', 50),
    ('tidal_studio', 'Creative studio', 'Stories and creative work.', 'moon', 'full', 55),
    ('guest_questions', 'Guest questions', 'Questions from visitors.', 'question', 'snippet', 60),
]
DEMO_USERS = [
    ('reset_guy', 'Reset Guy', 'member', 'crew'),
    ('pro_kicker', 'Pro Kicker', 'member', 'crew'),
    ('weekly_cap', 'Weekly Cap', 'member', 'crew'),
    ('moderator', 'Demo Moderator', 'moderator', 'crew'),
    ('external_guest', 'Demo Guest', 'guest', 'external'),
]


def initialize(path: Path, username: str, password: str, *, demo: bool = False) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9_]{2,31}', username):
        raise ValueError('Administrator username must be 3-32 lowercase letters, digits or underscores, starting with a letter.')
    if not 12 <= len(password) <= 200:
        raise ValueError('Use an administrator password of 12-200 characters.')
    if demo and username in {u[0] for u in DEMO_USERS}:
        raise ValueError('Administrator username conflicts with a demo account.')
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError('Database already exists; initialization never overwrites it. Choose a new PELAGO_DB_PATH.')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Build privately, then publish with an exclusive hard link. A competing initializer
    # or server cannot have its newly created database overwritten by this process.
    fd, name = tempfile.mkstemp(prefix='.pelago-init-', suffix='.db', dir=path.parent)
    os.close(fd)
    staged = Path(name)
    try:
        conn = sqlite3.connect(staged)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            init_schema(conn)
            conn.executemany('INSERT INTO boards (slug,name,description,icon,digest_level,sort_order) VALUES (?,?,?,?,?,?)', BOARDS)
            conn.execute('INSERT INTO users (username,display_name,role,membership_kind,status,password_hash) VALUES (?,?,?,?,?,?)',
                         (username, username, 'admin', 'crew', 'active', hash_password(password)))
            if demo:
                conn.executemany('INSERT INTO users (username,display_name,role,membership_kind,status) VALUES (?,?,?,?,?)',
                                 [(*u, 'active') for u in DEMO_USERS])
            conn.commit()
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity check failed.')
        finally:
            conn.close()
        os.link(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admin', required=True, help='First administrator username')
    parser.add_argument('--demo', action='store_true', help='Add five sample identities without passwords or API tokens')
    args = parser.parse_args(argv)
    path = config.settings.db_path
    if path.exists() or path.is_symlink():
        parser.error('Database already exists; choose a new PELAGO_DB_PATH.')
    password = getpass.getpass('New administrator password (12+ characters): ')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords do not match.')
    try:
        initialize(path, args.admin, password, demo=args.demo)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(f'Initialized {path}. Sign in as {args.admin}. No API tokens or plaintext password files were created.')
    if args.demo:
        print('Demo identities have no login credentials. Provision credentials explicitly before using them as clients.')


if __name__ == '__main__':
    main()
