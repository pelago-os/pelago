from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_library_api import auth, client  # noqa: F401  (fixture)
from app.auth import Actor
from app.services import batons

NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def board(root: Path, directory: str, frontmatter: str, body: str = '# board\n') -> Path:
    path = root / '20_PROJECTS' / directory / 'ACTIVE_WORKBOARD.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\n{frontmatter}---\n{body}', encoding='utf-8')
    return path


BATON = """id: x
updated_at: 2026-09-30T21:25:00-04:00
visible_to:
  - owner
  - project:example-project
baton:
  project_id: example-project
  aliases: [deepsea_base_forum]
  version: 3
  holder: lark
  status: awaiting_accept
  handoff_to: fern
  next: "做面板前端"
  blockers: "waiting for review quota"
  handoff_ref: 50_HANDOFF/project/example-project/x.md
  updated_at: 2026-09-30T21:25:00-04:00
  updated_by: lark
  mirror: confirmed
  password: "secret"
"""


def actor(username='owner', admin=True, persona=None):
    return Actor(id=1, username=username, display_name=username, role='admin' if admin else 'member', persona_id=persona, membership_kind='crew')


@pytest.fixture
def root(tmp_path):
    return tmp_path / 'PelagoLibrary'


@pytest.fixture(autouse=True)
def isolated_mirror(tmp_path, monkeypatch):
    # 默认镜像指向临时目录，测试绝不碰真实 Drive
    monkeypatch.setattr(batons, 'MIRROR_ROOT', tmp_path / 'DriveMirror-default')
    batons._inflight.clear()
    yield
    batons._inflight.clear()


def test_baton_whitelist_and_fields(root):
    board(root, 'deepsea_base_forum', BATON)
    out = batons.list_batons(root, actor(), NOW)
    item = out['items'][0]
    assert out['withBaton'] == 1 and item['hasBaton']
    b = item['baton']
    assert b['holder'] == 'lark' and b['status'] == 'awaiting_accept' and b['handoffTo'] == 'fern'
    assert b['version'] == 3 and b['mirrorDeclared'] == 'confirmed' and b['stale'] is False
    assert 'secret' not in str(out)


def test_legacy_board_listed_without_baton_and_sorted_last(root):
    board(root, 'aaa_legacy', 'id: y\nupdated_at: 2026-06-01T00:00:00-04:00\n')
    board(root, 'zzz_forum', BATON)
    items = batons.list_batons(root, actor(), NOW)['items']
    assert [i['directory'] for i in items] == ['zzz_forum', 'aaa_legacy']
    assert items[1]['hasBaton'] is False and items[1]['workboardUpdatedAt'].startswith('2026-06-01')


def test_invalid_values_are_normalized(root):
    board(root, 'p', BATON.replace('status: awaiting_accept', 'status: hacked')
          .replace('version: 3', 'version: "3"').replace('mirror: confirmed', 'mirror: yes')
          .replace('updated_at: 2026-09-30T21:25:00-04:00\n  updated_by', 'updated_at: soon\n  updated_by'))
    b = batons.list_batons(root, actor(), NOW)['items'][0]['baton']
    assert b['status'] == 'unknown' and b['version'] is None and b['mirrorDeclared'] == 'unconfirmed'
    assert b['updatedAt'] is None and b['stale'] is True


def test_stale_after_seven_days(root):
    board(root, 'p', BATON.replace('updated_at: 2026-09-30T21:25:00-04:00\n  updated_by',
                                   'updated_at: 2026-09-20T00:00:00-04:00\n  updated_by'))
    b = batons.list_batons(root, actor(), NOW)['items'][0]['baton']
    assert b['stale'] is True and b['daysSinceUpdate'] > 7


def test_broken_frontmatter_and_symlink(root, tmp_path):
    board(root, 'broken', 'baton: [unclosed\n')
    target = tmp_path / 'outside.md'
    target.write_text('---\nbaton:\n  holder: evil\n---\n')
    link_dir = root / '20_PROJECTS' / 'linked'
    link_dir.mkdir(parents=True)
    (link_dir / 'ACTIVE_WORKBOARD.md').symlink_to(target)
    items = batons.list_batons(root, actor(), NOW)['items']
    assert [i['directory'] for i in items] == ['broken'] and items[0]['hasBaton'] is False


def test_route_requires_login_and_reads_primary_root(client: TestClient):
    import app.config as config
    root = config.settings.deepsea_memory_root
    board(root, 'deepsea_base_forum', BATON)
    assert client.get('/api/library/batons').status_code == 401
    response = client.get('/api/library/batons', headers=auth('owner-dev-token'))
    assert response.status_code == 200
    data = response.json()
    assert data['source'] == 'library'
    assert any(i.get('baton', {}).get('holder') == 'lark' for i in data['items'])


def test_visibility_follows_workboard_visible_to(root):
    board(root, 'vv_only', 'id: z\nvisible_to:\n  - owner\nbaton:\n  holder: lark\n  status: active\n')
    assert batons.list_batons(root, actor('fern', admin=False, persona='P-11'), NOW)['items'] == []
    assert len(batons.list_batons(root, actor('owner', admin=False), NOW)['items']) == 1


LANES = """id: l
updated_at: 2026-10-01T10:00:00-04:00
baton:
  project_id: deepsea-baton-tool
  version: 5
  updated_at: 2026-10-01T10:00:00-04:00
  mirror: unconfirmed
  lanes:
    - id: prototype
      holder: lark
      status: active
      next: "做原型"
    - id: review
      holder: lark
      status: awaiting_accept
      handoff_to: rowan
      next: "review plan v1"
      handoff_ref: 50_HANDOFF/project/deepsea-baton-tool/x.md
    - id: docs
      holder: fern
      status: done
"""


def test_parallel_lanes(root):
    board(root, 'deepsea-baton-tool', LANES)
    b = batons.list_batons(root, actor(), NOW)['items'][0]['baton']
    assert [lane['id'] for lane in b['lanes']] == ['review', 'prototype', 'docs']
    assert b['status'] == 'awaiting_accept' and b['handoffTo'] == 'rowan' and b['done'] is False
    assert b['lanes'][2]['status'] == 'done' and b['version'] == 5


def test_all_lanes_done_is_not_stale(root):
    board(root, 'p', LANES.replace('status: active', 'status: done').replace('status: awaiting_accept', 'status: done')
          .replace('updated_at: 2026-10-01T10:00:00-04:00\n  mirror', 'updated_at: 2026-08-01T10:00:00-04:00\n  mirror'))
    b = batons.list_batons(root, actor(), NOW)['items'][0]['baton']
    assert b['done'] is True and b['stale'] is False


# ---------- provenance 与镜像核对 ----------

@pytest.fixture
def mirror(tmp_path, monkeypatch):
    m = tmp_path / 'DriveMirror'
    m.mkdir()
    monkeypatch.setattr(batons, 'MIRROR_ROOT', m)
    return m


def copy_to_mirror(root, mirror, directory):
    src = root / '20_PROJECTS' / directory / 'ACTIVE_WORKBOARD.md'
    dst = mirror / '20_PROJECTS' / directory / 'ACTIVE_WORKBOARD.md'
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return dst


def test_declared_confirmed_but_mirror_differs_shows_conflict(root, mirror):
    """条件 1：手填 confirmed 不算数，两端不同就是 conflict。"""
    board(root, 'p', BATON)  # BATON 手填 mirror: confirmed
    dst = copy_to_mirror(root, mirror, 'p')
    dst.write_text(dst.read_text() + '\n旧镜像多一行\n')
    item = batons.list_batons(root, actor(), NOW)['items'][0]
    assert item['baton']['mirrorDeclared'] == 'confirmed'
    assert item['baton']['mirror'] == 'conflict' and item['mirrorCheck']['verdict'] == 'conflict'


def test_identical_mirror_is_confirmed_and_hash_matches_same_read(root, mirror):
    """条件 2：路径、hash、读取时间来自同一次读取。"""
    path = board(root, 'p', BATON)
    copy_to_mirror(root, mirror, 'p')
    item = batons.list_batons(root, actor(), NOW)['items'][0]
    import hashlib
    assert item['provenance']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert item['provenance']['sourceRef'] == '20_PROJECTS/p/ACTIVE_WORKBOARD.md' and item['provenance']['readAt']
    assert item['baton']['mirror'] == 'confirmed' and item['mirrorCheck']['sha256'] == item['provenance']['sha256']


def test_mirror_missing_denied_too_large_and_unconfigured(root, mirror, monkeypatch):
    """条件 3：缺失、无权限、超限、未配置分别显示。"""
    board(root, 'p', BATON)
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'missing'
    dst = copy_to_mirror(root, mirror, 'p')
    dst.write_bytes(b'x' * (batons.MAX_BYTES + 1))
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'too_large'
    copy_to_mirror(root, mirror, 'p')
    real_open = Path.open
    def deny_open(self, *a, **k):
        if 'DriveMirror' in str(self):
            raise PermissionError('nope')
        return real_open(self, *a, **k)
    monkeypatch.setattr(Path, 'open', deny_open)
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'denied'
    monkeypatch.setattr(Path, 'open', real_open)
    monkeypatch.setattr(batons, 'MIRROR_ROOT', root / 'no-such-mirror')
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'unavailable'
    monkeypatch.setattr(batons, 'MIRROR_ROOT', None)
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'not_configured'


def test_mirror_timeout_does_not_hang(root, mirror, monkeypatch):
    import time
    board(root, 'p', BATON)
    monkeypatch.setattr(batons, 'MIRROR_TIMEOUT', 0.2)
    monkeypatch.setattr(batons, '_mirror_digest', lambda root, relative: time.sleep(1) or ('read', 'x'))
    started = time.time()
    assert batons.list_batons(root, actor(), NOW)['items'][0]['baton']['mirror'] == 'timeout'
    assert time.time() - started < 1.0


def test_change_during_read_is_marked_unstable(root, mirror, monkeypatch):
    """条件 4：读的过程中被改，重读一次仍变化就标不稳定。"""
    path = board(root, 'p', BATON)
    real_stat = type(path).stat
    calls = {'n': 0}
    def flapping_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self.name == 'ACTIVE_WORKBOARD.md' and self.parent.name == 'p':
            calls['n'] += 1
            import os
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size + calls['n'], int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        return st
    monkeypatch.setattr(type(path), 'stat', flapping_stat)
    item = batons.list_batons(root, actor(), NOW)['items'][0]
    assert item['provenance']['stable'] is False


def test_unauthorized_actor_gets_no_hash_or_mirror(root, mirror):
    """条件 5：看不了这份看板的人，连 hash 和镜像状态也拿不到。"""
    board(root, 'vv_only', 'id: z\nvisible_to:\n  - owner\nbaton:\n  holder: lark\n  status: active\n')
    copy_to_mirror(root, mirror, 'vv_only')
    out = batons.list_batons(root, actor('fern', admin=False, persona='P-11'), NOW)
    assert out['items'] == [] and 'sha256' not in str(out)
    board(root, 'broken', 'baton: [unclosed\n')
    member = batons.list_batons(root, actor('fern', admin=False, persona='P-11'), NOW)
    assert all(i['directory'] != 'broken' for i in member['items'])



# ---------- 根目录探测也受超时和错误隔离约束，反复超时不积压 ----------

def test_mirror_root_probe_blocking_is_bounded_by_timeout(root, mirror, monkeypatch):
    import time
    board(root, 'p', BATON)
    monkeypatch.setattr(batons, 'MIRROR_TIMEOUT', 0.05)
    real_is_dir = Path.is_dir
    def slow_is_dir(self):
        if self == mirror:
            time.sleep(0.5)
        return real_is_dir(self)
    monkeypatch.setattr(Path, 'is_dir', slow_is_dir)
    started = time.time()
    item = batons.list_batons(root, actor(), NOW)['items'][0]
    assert item['baton']['mirror'] == 'timeout' and time.time() - started < 0.4


def test_mirror_root_probe_permission_error_is_isolated(root, mirror, monkeypatch):
    board(root, 'p', BATON)
    real_is_dir = Path.is_dir
    def deny_is_dir(self):
        if self == mirror:
            raise PermissionError('root denied')
        return real_is_dir(self)
    monkeypatch.setattr(Path, 'is_dir', deny_is_dir)
    out = batons.list_batons(root, actor(), NOW)
    assert out['items'][0]['baton']['mirror'] == 'denied' and out['items'][0]['mirrorCheck']['scope'] == 'local-drive-mount'


def test_repeated_timeouts_do_not_pile_up(root, mirror, monkeypatch):
    import threading, time
    for n in range(6):
        board(root, f'p{n}', BATON)
    release = threading.Event()
    calls = []
    def stuck(root_, relative):
        calls.append(str(relative)); release.wait(2); return ('read', 'x')
    monkeypatch.setattr(batons, '_mirror_digest', stuck)
    monkeypatch.setattr(batons, 'MIRROR_TIMEOUT', 0.02)
    try:
        for _ in range(3):
            verdicts = [i['baton']['mirror'] for i in batons.list_batons(root, actor(), NOW)['items']]
        assert set(verdicts) <= {'timeout', 'busy'} and 'busy' in verdicts
        assert len(calls) <= batons.MIRROR_MAX_INFLIGHT      # 三轮请求，在途核对始终不超过上限
    finally:
        release.set()


def test_baton_panel_and_library_agree_on_visibility(root):
    """R1: the panel must not show a workboard the library would hide, or the other way round."""
    from app.services.library import actor_can_read, parse_frontmatter

    # An inline comment: strict YAML reads "owner", the lenient library parser reads "owner # private".
    text = 'id: c\nvisible_to: owner # private\nbaton:\n  holder: lark\n  status: active\n'
    path = board(root, 'commented', text)
    relative = path.relative_to(root)
    meta = parse_frontmatter(path.read_text(encoding='utf-8')).frontmatter
    for who in (actor('owner', admin=False), actor('pro_kicker', admin=False)):
        library_says = actor_can_read(who, relative, meta)
        panel_says = bool(batons.list_batons(root, who, NOW)['items'])
        assert panel_says == library_says, who.username


def test_nested_visible_to_does_not_open_a_private_workboard(root):
    text = 'id: n\nvisible_to:\n  - owner\nbaton:\n  holder: lark\n  status: active\n  visible_to:\n    - all_agents\n'
    board(root, 'nested_acl', text)
    assert batons.list_batons(root, actor('pro_kicker', admin=False), NOW)['items'] == []
    assert len(batons.list_batons(root, actor('owner', admin=False), NOW)['items']) == 1


def test_reserved_acl_key_cannot_open_a_workboard(root):
    text = 'id: r\nvisible_to: [owner]\nvisible_to: [all_agents]\n_acl_error: []\nbaton:\n  holder: lark\n  status: active\n'
    board(root, 'reserved', text)
    assert batons.list_batons(root, actor('pro_kicker', admin=False), NOW)['items'] == []
