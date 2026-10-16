from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from app.routes import forum
from app.services import forum_exports, forum_queries


@pytest.fixture
def export_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE users (id INTEGER, username TEXT, role TEXT DEFAULT 'member', membership_kind TEXT DEFAULT 'crew', status TEXT DEFAULT 'active');
        CREATE TABLE threads (id INTEGER, title TEXT);
        CREATE TABLE posts (id INTEGER, thread_id INTEGER, author_user_id INTEGER,
            parent_post_id INTEGER, body_markdown TEXT, status TEXT, created_at TEXT);
        INSERT INTO users(id,username) VALUES (1, 'cedar'), (2, 'owner'), (3, 'rowan');
        UPDATE users SET role='admin' WHERE id=2;
    """)
    statements = []
    conn.set_trace_callback(statements.append)

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(forum_queries, "get_connection", connection)
    yield conn, statements
    conn.close()


def test_personal_reasons_preserve_priority_visibility_and_snippet_order(export_db):
    conn, statements = export_db
    conn.executemany("INSERT INTO threads VALUES (?, ?)", [(1, 'A'), (2, 'B'), (3, 'C'), (4, 'D')])
    conn.executemany("INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (1, 1, 1, None, 'start', 'visible', '01'),
        (2, 1, 2, 1, 'hello 白杉 first', 'visible', '02'),
        (3, 1, 3, None, '白杉 later', 'visible', '03'),
        (4, 2, 1, None, 'start', 'visible', '01'),
        (5, 2, 3, None, 'update', 'visible', '02'),
        (6, 3, 1, None, 'start', 'visible', '01'),
        (7, 3, 2, 6, '白杉 hidden', 'deleted', '02'),
        (8, 3, 1, 6, 'self reply', 'visible', '03'),
        (9, 4, 2, None, 'Owner context', 'visible', '01'),
    ])
    threads = [SimpleNamespace(id=i, title=title) for i, title in [(1, 'A'), (2, 'B'), (3, 'C'), (4, 'D')]]
    statements.clear()
    assert forum.personal_match_reasons(threads, 1, ['白杉']) == {
        1: 'alias_mention, direct_reply, owner_participated',
        2: 'thread_update_after_target',
        4: 'owner_participated',
    }
    assert forum.alias_match_snippets(threads, ['白杉']) == {1: 'hello 白杉 first'}
    assert len(statements) == 2


@pytest.mark.parametrize('count, queries', [(1, 2), (100, 2), (201, 4)])
def test_export_query_count_is_per_batch_not_per_thread(export_db, count, queries):
    conn, statements = export_db
    threads = [SimpleNamespace(id=i, title='No title mention') for i in range(1, count + 1)]
    conn.executemany('INSERT INTO threads VALUES (?, ?)', [(t.id, t.title) for t in threads])
    conn.executemany('INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?, ?)', [
        (t.id, t.id, 3, None, 'hello 白杉', 'visible', '01') for t in threads
    ])
    statements.clear()
    assert len(forum.personal_match_reasons(threads, 1, ['白杉'])) == count
    assert len(forum.alias_match_snippets(threads, ['白杉'])) == count
    assert len(statements) == queries


def test_empty_inputs_and_title_matches_skip_queries(export_db):
    _, statements = export_db
    assert forum.personal_match_reasons([], 1, []) == {}
    assert forum.alias_match_snippets([], ['白杉']) == {}
    assert forum.alias_match_snippets([SimpleNamespace(id=1, title='白杉 title')], []) == {}
    assert forum.alias_match_snippets([SimpleNamespace(id=1, title='白杉 title')], ['白杉']) == {1: '白杉 title'}
    assert statements == []


def test_formatter_compatibility_and_empty_export():
    assert forum.render_export_markdown is forum_exports.render_export_markdown
    assert forum.render_thread_list_markdown is forum_exports.render_thread_list_markdown
    assert forum_exports.blockquote('a\n\nb') == '> a\n>\n> b'
    assert forum_exports.thread_export_slug('Hello / World') == 'hello-world'
    body = forum_exports.render_thread_list_markdown(
        [], 0, None, 'latest', 20, 0, None, 'rowan',
    )
    assert 'allowed_ops:\n  - read\n' in body
    assert '# Forum Latest List' in body
    assert body.endswith('No matching threads in this list snapshot.\n')
