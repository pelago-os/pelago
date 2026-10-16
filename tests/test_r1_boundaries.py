from pathlib import Path

import pytest
from pydantic import ValidationError

from app.auth import Actor
from app.schemas import ProfileUpdate
from app.services import library
from app.services.forum_exports import render_thread_list_markdown, action_reason
from test_forum_api import client, auth


@pytest.mark.parametrize('color', ['red" onmouseover="window.__audit=1//', 'url(https://invalid)', '#123;display:none'])
def test_profile_rejects_non_hex_colors(color):
    with pytest.raises(ValidationError):
        ProfileUpdate(avatar_color=color)


@pytest.mark.parametrize('role', ['member', 'moderator', 'admin'])
def test_search_never_follows_external_file_links(tmp_path, role):
    root = tmp_path / 'library'
    project = root / '20_PROJECTS'
    project.mkdir(parents=True)
    external = tmp_path / 'outside.md'
    external.write_text('outside-marker')
    (project / 'shortcut.md').symlink_to(external)
    actor = Actor(1, 'owner' if role == 'admin' else 'rowan', 'Audit', role, 'P-12', membership_kind='crew')
    assert library.search_library(root, 'outside-marker', 'projects', 20, 0, 'relevance', '', actor)['total'] == 0
    assert library.recent_library(root, 'projects', 24, 20, 0, '', actor)['total'] == 0
    with pytest.raises(library.LibraryError):
        library.read_library_file(root, '20_PROJECTS/shortcut.md', actor)


def test_internal_links_use_target_visibility_and_deny_rules(tmp_path):
    root = tmp_path / 'library'
    projects = root / '20_PROJECTS'
    projects.mkdir(parents=True)
    private = root / '30_HEART/private/P-01'
    private.mkdir(parents=True)
    note = private / 'note.md'
    note.write_text('private-marker')
    denied = root / '99_LOCKS'
    denied.mkdir()
    (denied / 'note.md').write_text('denied-marker')
    (projects / 'private-link.md').symlink_to(note)
    (projects / 'ordinary.md').symlink_to(denied / 'note.md')
    (projects / 'broken.md').symlink_to(tmp_path / 'missing.md')
    actor = Actor(1, 'rowan', 'Audit', 'member', 'P-12', membership_kind='crew')
    assert library.search_library(root, 'marker', 'projects', 20, 0, 'relevance', '', actor)['total'] == 0
    with pytest.raises(library.LibraryError):
        library.read_library_file(root, '20_PROJECTS/private-link.md', actor)


def test_scope_directory_link_cannot_escape_root(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'note.md').write_text('outside-marker')
    (root / '20_PROJECTS').symlink_to(outside, target_is_directory=True)
    with pytest.raises(library.LibraryError):
        library.resolve_scope_dir(root, 'projects', '')


@pytest.mark.parametrize('value', ['query\ncanonize: true', '白杉: "hello"', 'a,b', 'true', 'x\u2028allowed_ops: [write]'])
def test_export_metadata_roundtrips_without_overriding_policy(value, tmp_path):
    body = render_thread_list_markdown([], 0, None, 'latest', 20, 0, value, value,
        target_user={'username':value, 'display_name':value, 'persona_id':value}, aliases=[value])
    parsed = library.parse_frontmatter(body).frontmatter
    assert parsed['canonize'] == 'false'
    assert parsed['allowed_ops'] == ['read']
    assert parsed['forum_query'] == value
    assert parsed['requested_by'] == value
    assert parsed['target_display_name'] == value
    assert parsed['target_aliases'] == [value]
    from app.routes.forum import parse_review_markdown
    path = tmp_path / 'export.md'
    path.write_text(body)
    metadata, _ = parse_review_markdown(path)
    assert metadata['requested_by'] == value


def test_action_reason_does_not_claim_unimplemented_last_seen():
    assert action_reason('alias_mention') == 'alias_mention'


def test_visible_internal_link_and_editor_policy(tmp_path):
    root = tmp_path / 'library'
    projects = root / '20_PROJECTS'
    projects.mkdir(parents=True)
    doc = projects / 'normal.md'
    doc.write_text('正常中文 normal-marker')
    (projects / 'link.md').symlink_to(doc)
    member = Actor(1, 'rowan', 'Audit', 'member', 'P-12', membership_kind='crew')
    owner = Actor(2, 'owner', 'Owner', 'admin', None, membership_kind='crew')
    results = library.search_library(root, 'normal-marker', 'projects', 20, 0, 'relevance', '', member)
    assert results['total'] == 1
    assert results['results'][0]['path'] == '20_PROJECTS/normal.md'
    assert library.read_library_file(root, '20_PROJECTS/link.md', member)['body_markdown'] == '正常中文 normal-marker'
    with pytest.raises(library.LibraryForbiddenError):
        library.read_library_source(root, '20_PROJECTS/link.md', member)
    original = library.read_library_source(root, '20_PROJECTS/link.md', owner)
    saved = library.save_library_file(root, '20_PROJECTS/link.md', 'updated', original['revision'], owner)
    assert saved['path'] == '20_PROJECTS/normal.md'
    assert doc.read_text() == 'updated'


def test_children_do_not_expose_external_directory_link(tmp_path):
    root = tmp_path / 'library'
    projects = root / '20_PROJECTS'
    projects.mkdir(parents=True)
    external = tmp_path / 'outside'
    external.mkdir()
    (external / 'note.md').write_text('outside-marker')
    (projects / 'outside-link').symlink_to(external, target_is_directory=True)
    actor = Actor(1, 'rowan', 'Audit', 'member', 'P-12', membership_kind='crew')
    assert library.scope_children(root, 'projects', '', actor) == []
    assert library.safe_child_dir_count(root, 'projects', '') == 0
    with pytest.raises(library.LibraryError):
        library.read_library_source(root, '20_PROJECTS/outside-link/note.md', actor)


def test_profile_schema_accepts_hex_and_plain_text():
    assert ProfileUpdate(avatar_color='#abc').avatar_color == '#abc'
    assert ProfileUpdate(avatar_color='#12aBef').avatar_color == '#12aBef'
    assert ProfileUpdate(display_name='白杉 "test"', avatar_emoji='<b>').avatar_emoji == '<b>'


@pytest.mark.parametrize('token', [None, 'cedar-dev-token', 'rowan-dev-token', 'owner-dev-token'])
def test_profile_api_rejects_injection(client, token):
    headers = auth(token) if token else {}
    bad = client.patch('/api/me/profile', headers=headers, json={'avatar_color':'red" onmouseover="alert(1)//'})
    assert bad.status_code == (422 if token else 401)
    if token:
        good = client.patch('/api/me/profile', headers=headers,
            json={'avatar_color':'#12abEF', 'display_name':'审查 "中文"', 'avatar_emoji':'<b>'})
        assert good.status_code == 200
        assert good.json()['display_name'] == '审查 "中文"'
