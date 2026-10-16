from __future__ import annotations

import hashlib
import json
import time
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory_root = tmp_path / "PelagoLibrary"
    monkeypatch.setenv("PELAGO_DB_PATH", str(tmp_path / "forum.db"))
    monkeypatch.setenv("PELAGO_AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    monkeypatch.setenv("PELAGO_LIBRARY_ROOT", str(memory_root))
    monkeypatch.setenv("FORUM_TEST_SEED_TOKENS", "1")

    import app.config as config

    config.settings = config.Settings.from_env()

    import app.routes.forum as forum_routes
    import app.routes.library as library_routes
    import app.services.audit as audit_service


    import app.database as database


    with database.get_connection(config.settings.db_path) as conn:
        database.init_schema(conn)

    import legacy_seed as init_db

    init_db.main()
    seed_library(memory_root)

    from app.main import app

    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_library(memory_root: Path) -> None:
    now = time.time()

    project_dir = memory_root / "20_PROJECTS" / "deepsea_base"
    project_dir.mkdir(parents=True)
    (project_dir / "local_library_note.md").write_text(
        """---
title: 本地图书馆接入记录
visible_to:
  - all_agents
memory_scope: project
---

# 本地图书馆接入记录

潮汐索引 是一个用于测试的唯一关键词。
""",
        encoding="utf-8",
    )
    (project_dir / "sort_old.md").write_text(
        """---
title: 排序测试旧文档
visible_to:
  - all_agents
---

排序测试 共同关键词。
""",
        encoding="utf-8",
    )
    (project_dir / "sort_new.md").write_text(
        """---
title: 排序测试新文档
visible_to:
  - all_agents
---

排序测试 共同关键词。
""",
        encoding="utf-8",
    )

    private_dir = memory_root / "30_HEART" / "private" / "P-01"
    private_dir.mkdir(parents=True)
    (private_dir / "private_note.md").write_text(
        """---
title: 杉哥私密记录
visible_to:
  - persona:P-01
memory_scope: persona_private
---

private-cedar-signal 只应该给杉哥和管理员看到。
""",
        encoding="utf-8",
    )

    infra_dir = memory_root / "60_INFRASTRUCTURE"
    infra_dir.mkdir(parents=True)
    (infra_dir / "mcp-server-registry.md").write_text(
        "---\nvisible_to:\n  - owner\n---\n\ninfra-scope-signal 注册表。\n",
        encoding="utf-8",
    )

    journal_dir = memory_root / "30_JOURNAL" / "daily"
    journal_dir.mkdir(parents=True)
    (journal_dir / "journal_note.md").write_text(
        """---
title: 日记 scope 测试
visible_to:
  - owner
---

journal-only-signal 只属于 30_JOURNAL。
""",
        encoding="utf-8",
    )

    index_dir = memory_root / "70_INDEX"
    index_dir.mkdir(parents=True)
    (index_dir / "index_note.md").write_text("index-scope-signal", encoding="utf-8")

    templates_dir = memory_root / "80_TEMPLATES"
    templates_dir.mkdir(parents=True)
    (templates_dir / "template_note.md").write_text("template-scope-signal", encoding="utf-8")

    archive_dir = memory_root / "90_RAW_ARCHIVE"
    archive_dir.mkdir(parents=True)
    (archive_dir / "archive_note.md").write_text("archive-scope-signal", encoding="utf-8")

    inbox_chatgpt_che_dir = memory_root / "40_INBOX" / "chatgpt" / "cedar"
    inbox_chatgpt_che_dir.mkdir(parents=True)
    (inbox_chatgpt_che_dir / "che_drop.md").write_text(
        """---
title: Cedar inbox drop
visible_to:
  - all_agents
---

recipient-inbox-signal cedar-only-inbox-signal
""",
        encoding="utf-8",
    )
    inbox_chatgpt_chen_dir = memory_root / "40_INBOX" / "chatgpt" / "chase"
    inbox_chatgpt_chen_dir.mkdir(parents=True)
    (inbox_chatgpt_chen_dir / "chen_drop.md").write_text(
        """---
title: Chase inbox drop
visible_to:
  - all_agents
---

recipient-inbox-signal chase-only-inbox-signal
""",
        encoding="utf-8",
    )
    inbox_hermes_che_dir = memory_root / "40_INBOX" / "agents" / "host-a" / "cedar"
    inbox_hermes_che_dir.mkdir(parents=True)
    (inbox_hermes_che_dir / "hermes_che_drop.md").write_text(
        """---
title: Hermes Cedar inbox drop
visible_to:
  - all_agents
---

hermes-cedar-inbox-signal
""",
        encoding="utf-8",
    )

    secret_dir = memory_root / "20_PROJECTS" / "deepsea_base"
    (secret_dir / "token_cache_note.md").write_text(
        "token-like path should not be exposed even if it is markdown",
        encoding="utf-8",
    )

    old_mtime = now - (72 * 60 * 60)
    new_mtime = now - (60 * 60)
    for path in [
        project_dir / "sort_old.md",
        archive_dir / "archive_note.md",
    ]:
        os.utime(path, (old_mtime, old_mtime))
    os.utime(project_dir / "sort_new.md", (new_mtime, new_mtime))
    os.utime(journal_dir / "journal_note.md", (new_mtime, new_mtime))
    os.utime(inbox_chatgpt_che_dir / "che_drop.md", (new_mtime, new_mtime))
    os.utime(inbox_chatgpt_chen_dir / "chen_drop.md", (new_mtime, new_mtime))


def test_library_status_and_search(client: TestClient):
    status = client.get("/api/library/status", headers=auth("owner-dev-token"))
    assert status.status_code == 200
    assert status.json()["exists"] is True
    assert status.json()["can_write"] is True
    assert status.json()["writable_scopes"] == [
        "projects",
        "inbox",
        "forum",
        "handoffs",
        "reports",
    ]
    assert any(scope["slug"] == "projects" for scope in status.json()["scopes"])
    slugs = {scope["slug"] for scope in status.json()["scopes"]}
    assert {"infrastructure", "index", "templates", "raw_archive"}.issubset(slugs)
    assert "journal" not in slugs  # Owner 2026-10-10：撤掉日记分类
    inbox_scope = next(scope for scope in status.json()["scopes"] if scope["slug"] == "inbox")
    inbox_children = {child["path"]: child for child in inbox_scope["children"]}
    assert "chatgpt" in inbox_children
    assert inbox_children["chatgpt"]["document_count"] == 2
    assert inbox_children["chatgpt"]["has_children"] is True

    searched = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "潮汐索引", "scope": "projects"},
    )
    assert searched.status_code == 200
    body = searched.json()
    assert body["total"] == 1
    assert body["has_more"] is False
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert body["sort"] == "relevance"
    assert body["results"][0]["path"] == "20_PROJECTS/deepsea_base/local_library_note.md"
    assert "潮汐索引" in body["results"][0]["excerpt"]
    assert body["results"][0]["frontmatter"]["title"] == "本地图书馆接入记录"


def test_library_file_returns_markdown_and_sanitized_html(client: TestClient):
    fetched = client.get(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        params={"path": "20_PROJECTS/deepsea_base/local_library_note.md"},
    )
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["title"] == "本地图书馆接入记录"
    assert "潮汐索引" in body["body_markdown"]
    assert "<h1>本地图书馆接入记录</h1>" in body["body_html"]
    assert body["editable"] is True
    assert len(body["revision"]) == 64


def test_library_rejects_path_escape(client: TestClient):
    escaped = client.get(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        params={"path": "../outside.md"},
    )
    assert escaped.status_code == 400


def test_library_private_heart_visibility(client: TestClient):
    chen_search = client.get(
        "/api/library/search",
        headers=auth("chase-dev-token"),
        params={"q": "private-cedar-signal", "scope": "heart"},
    )
    assert chen_search.status_code == 200
    assert chen_search.json()["total"] == 0

    che_search = client.get(
        "/api/library/search",
        headers=auth("cedar-dev-token"),
        params={"q": "private-cedar-signal", "scope": "heart"},
    )
    assert che_search.status_code == 200
    assert che_search.json()["total"] == 1

    chen_fetch = client.get(
        "/api/library/file",
        headers=auth("chase-dev-token"),
        params={"path": "30_HEART/private/P-01/private_note.md"},
    )
    assert chen_fetch.status_code == 403


def test_library_skips_sensitive_paths(client: TestClient):
    searched = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "token-like", "scope": "projects"},
    )
    assert searched.status_code == 200
    assert searched.json()["total"] == 0


def test_library_new_scopes_and_30_prefix_do_not_conflict(client: TestClient):
    journal = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "journal-only-signal", "scope": "journal"},
    )
    assert journal.status_code == 400
    everywhere = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "journal-only-signal", "scope": "all"},
    )
    assert everywhere.json()["total"] == 1

    infra = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "infra-scope-signal", "scope": "infrastructure"},
    )
    assert infra.json()["total"] == 1
    reports = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "infra-scope-signal", "scope": "reports"},
    )
    assert reports.json()["total"] == 0

    heart = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "journal-only-signal", "scope": "heart"},
    )
    assert heart.status_code == 200
    assert heart.json()["total"] == 0

    for scope, signal in [
        ("index", "index-scope-signal"),
        ("templates", "template-scope-signal"),
        ("raw_archive", "archive-scope-signal"),
    ]:
        response = client.get(
            "/api/library/search",
            headers=auth("owner-dev-token"),
            params={"q": signal, "scope": scope},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1


def test_library_search_sort_and_pagination(client: TestClient):
    newest = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "排序测试", "scope": "projects", "sort": "newest", "limit": 1},
    )
    assert newest.status_code == 200
    newest_body = newest.json()
    assert newest_body["total"] == 2
    assert newest_body["has_more"] is True
    assert newest_body["results"][0]["path"] == "20_PROJECTS/deepsea_base/sort_new.md"

    second_page = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "排序测试", "scope": "projects", "sort": "newest", "limit": 1, "offset": 1},
    )
    assert second_page.status_code == 200
    assert second_page.json()["has_more"] is False
    assert second_page.json()["results"][0]["path"] == "20_PROJECTS/deepsea_base/sort_old.md"

    oldest = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "排序测试", "scope": "projects", "sort": "oldest", "limit": 2},
    )
    assert oldest.status_code == 200
    assert oldest.json()["results"][0]["path"] == "20_PROJECTS/deepsea_base/sort_old.md"


def test_library_recent_endpoint(client: TestClient):
    recent = client.get(
        "/api/library/recent",
        headers=auth("owner-dev-token"),
        params={"hours": 24, "scope": "projects", "limit": 10},
    )
    assert recent.status_code == 200
    body = recent.json()
    assert body["hours"] == 24
    assert body["sort"] == "newest"
    paths = [result["path"] for result in body["results"]]
    assert "20_PROJECTS/deepsea_base/sort_new.md" in paths
    assert "20_PROJECTS/deepsea_base/sort_old.md" not in paths


def test_library_inbox_children_and_subpath_search(client: TestClient):
    children = client.get(
        "/api/library/children",
        headers=auth("owner-dev-token"),
        params={"scope": "inbox", "subpath": "chatgpt"},
    )
    assert children.status_code == 200
    body = children.json()
    assert body["scope"] == "inbox"
    assert body["subpath"] == "chatgpt"
    child_paths = {child["path"] for child in body["children"]}
    assert child_paths == {"chatgpt/cedar", "chatgpt/chase"}

    all_chatgpt = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "recipient-inbox-signal", "scope": "inbox", "subpath": "chatgpt"},
    )
    assert all_chatgpt.status_code == 200
    assert all_chatgpt.json()["total"] == 2
    assert all_chatgpt.json()["subpath"] == "chatgpt"

    che_only = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "recipient-inbox-signal", "scope": "inbox", "subpath": "chatgpt/cedar"},
    )
    assert che_only.status_code == 200
    assert che_only.json()["total"] == 1
    assert che_only.json()["results"][0]["path"] == "40_INBOX/chatgpt/cedar/che_drop.md"

    recent_che = client.get(
        "/api/library/recent",
        headers=auth("owner-dev-token"),
        params={"scope": "inbox", "subpath": "chatgpt/cedar", "hours": 24},
    )
    assert recent_che.status_code == 200
    assert recent_che.json()["subpath"] == "chatgpt/cedar"
    assert [result["path"] for result in recent_che.json()["results"]] == [
        "40_INBOX/chatgpt/cedar/che_drop.md"
    ]


def test_library_rejects_invalid_subpath(client: TestClient):
    children = client.get(
        "/api/library/children",
        headers=auth("owner-dev-token"),
        params={"scope": "inbox", "subpath": "../projects"},
    )
    assert children.status_code == 400

    searched = client.get(
        "/api/library/search",
        headers=auth("owner-dev-token"),
        params={"q": "recipient-inbox-signal", "scope": "inbox", "subpath": "../projects"},
    )
    assert searched.status_code == 400


def test_library_write_status_and_source_are_vv_only(client: TestClient):
    chen_status = client.get("/api/library/status", headers=auth("chase-dev-token"))
    assert chen_status.status_code == 200
    assert chen_status.json()["can_write"] is False
    assert chen_status.json()["writable_scopes"] == []

    path = "20_PROJECTS/deepsea_base/local_library_note.md"
    vv_source = client.get("/api/library/source", headers=auth("owner-dev-token"), params={"path": path})
    assert vv_source.status_code == 200
    assert vv_source.json()["raw_markdown"].startswith("---\ntitle: 本地图书馆接入记录")
    assert vv_source.json()["revision"] == hashlib.sha256(
        vv_source.json()["raw_markdown"].encode("utf-8")
    ).hexdigest()

    chen_file = client.get("/api/library/file", headers=auth("chase-dev-token"), params={"path": path})
    assert chen_file.status_code == 200
    assert chen_file.json()["editable"] is False

    chen_source = client.get("/api/library/source", headers=auth("chase-dev-token"), params={"path": path})
    assert chen_source.status_code == 403


def test_library_mobile_editor_creates_updates_backs_up_and_audits(client: TestClient):
    memory_root = Path(os.environ["PELAGO_LIBRARY_ROOT"])
    audit_path = Path(os.environ["PELAGO_AUDIT_LOG_PATH"])
    relative_path = "40_INBOX/owner/mobile/trip_note.md"
    first_content = """---
title: 出差随手记
visible_to:
  - owner
---

# 出差随手记

第一版。
"""

    created = client.put(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        json={"path": relative_path, "content": first_content, "expected_revision": None},
    )
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["operation"] == "created"
    assert created_body["backup_path"] is None
    assert created_body["editable"] is True
    assert (memory_root / relative_path).read_text(encoding="utf-8") == first_content

    second_content = first_content.replace("第一版。", "第二版，手机保存。")
    updated = client.put(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        json={
            "path": relative_path,
            "content": second_content,
            "expected_revision": created_body["revision"],
        },
    )
    assert updated.status_code == 200
    updated_body = updated.json()
    assert updated_body["operation"] == "updated"
    assert updated_body["previous_revision"] == created_body["revision"]
    assert updated_body["revision"] != created_body["revision"]
    assert (memory_root / relative_path).read_text(encoding="utf-8") == second_content

    backup_path = memory_root / updated_body["backup_path"]
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == first_content
    assert "99_LOCKS/library_editor_backups" in updated_body["backup_path"]

    audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    save_records = [record for record in audit_records if record["event"] == "library.file_saved"]
    assert [record["operation"] for record in save_records[-2:]] == ["created", "updated"]
    assert save_records[-1]["performed_by"] == "owner"
    assert save_records[-1]["path"] == relative_path
    assert "content" not in save_records[-1]


def test_library_mobile_editor_rejects_stale_or_blind_overwrites(client: TestClient):
    memory_root = Path(os.environ["PELAGO_LIBRARY_ROOT"])
    relative_path = "20_PROJECTS/deepsea_base/local_library_note.md"
    source = client.get(
        "/api/library/source",
        headers=auth("owner-dev-token"),
        params={"path": relative_path},
    ).json()

    blind = client.put(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        json={"path": relative_path, "content": source["raw_markdown"], "expected_revision": None},
    )
    assert blind.status_code == 409

    external_content = source["raw_markdown"] + "\n外部编辑器的新内容。\n"
    (memory_root / relative_path).write_text(external_content, encoding="utf-8")
    stale = client.put(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        json={
            "path": relative_path,
            "content": source["raw_markdown"] + "\n手机上的旧草稿。\n",
            "expected_revision": source["revision"],
        },
    )
    assert stale.status_code == 409
    assert (memory_root / relative_path).read_text(encoding="utf-8") == external_content


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "00_BOOTSTRAP/CORE.md",
        "10_PERSONAS/owner.md",
        "30_HEART/private/Owner/note.md",
        "70_INDEX/index.md",
        "20_PROJECTS/deepsea_base/token_notes.md",
        "20_PROJECTS/deepsea_base/not_markdown.txt",
    ],
)
def test_library_mobile_editor_rejects_unsafe_paths(client: TestClient, path: str):
    response = client.put(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        json={"path": path, "content": "# blocked\n", "expected_revision": None},
    )
    assert response.status_code in {400, 403}


def test_library_mobile_editor_rejects_non_vv_writes(client: TestClient):
    response = client.put(
        "/api/library/file",
        headers=auth("chase-dev-token"),
        json={
            "path": "40_INBOX/chase/mobile-note.md",
            "content": "# should not write\n",
            "expected_revision": None,
        },
    )
    assert response.status_code == 403


def _write_moves(memory_root, rows):
    index_dir = memory_root / "70_INDEX"
    index_dir.mkdir(parents=True, exist_ok=True)
    lines = ["old_path\tnew_path\tmoved_at\tbatch_id"] + ["\t".join(row) for row in rows]
    (index_dir / "path_moves.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _memory_root(client):
    from app import config
    return config.settings.deepsea_memory_root


def test_library_file_follows_path_moves(client: TestClient):
    root = _memory_root(client)
    target = root / "60_REPORTS" / "chase" / "github-trending"
    target.mkdir(parents=True)
    (target / "2026-07-28_github-trending-chase.md").write_text(
        "---\nvisible_to:\n  - owner\n---\n\n搬家后的报告。\n", encoding="utf-8"
    )
    _write_moves(root, [
        ("40_INBOX/2026-07-28_github-trending-chase.md", "40_INBOX/agents/host-a/chase/github-trending/2026-07-28_github-trending-chase.md", "2026-10-10", "a"),
        ("40_INBOX/agents/host-a/chase/github-trending/2026-07-28_github-trending-chase.md", "60_REPORTS/chase/github-trending/2026-07-28_github-trending-chase.md", "2026-10-10", "b"),
    ])
    response = client.get(
        "/api/library/file",
        headers=auth("owner-dev-token"),
        params={"path": "40_INBOX/2026-07-28_github-trending-chase.md"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "60_REPORTS/chase/github-trending/2026-07-28_github-trending-chase.md"
    assert body["moved_from"] == "40_INBOX/2026-07-28_github-trending-chase.md"


def test_library_path_moves_refuse_loops_escapes_and_private_targets(client: TestClient):
    root = _memory_root(client)
    _write_moves(root, [
        ("40_INBOX/loop-a.md", "40_INBOX/loop-b.md", "2026-10-10", "x"),
        ("40_INBOX/loop-b.md", "40_INBOX/loop-a.md", "2026-10-10", "x"),
        ("40_INBOX/escape.md", "../outside.md", "2026-10-10", "x"),
        ("40_INBOX/to-locks.md", "99_LOCKS/secret.md", "2026-10-10", "x"),
    ])
    for old, allowed in [("40_INBOX/loop-a.md", {404}), ("40_INBOX/escape.md", {400}), ("40_INBOX/to-locks.md", {403, 404})]:
        response = client.get("/api/library/file", headers=auth("owner-dev-token"), params={"path": old})
        assert response.status_code in allowed, (old, response.status_code)


def test_library_inbox_machine_dirs_sink_to_bottom(client: TestClient):
    root = _memory_root(client)
    machine = root / "40_INBOX" / "forum_digest"
    machine.mkdir(parents=True, exist_ok=True)
    (machine / "digest.md").write_text("---\nvisible_to:\n  - owner\n---\n\n机器流水。\n", encoding="utf-8")
    response = client.get(
        "/api/library/children", headers=auth("owner-dev-token"), params={"scope": "inbox", "subpath": ""}
    )
    assert response.status_code == 200
    children = response.json()["children"]
    groups = [child["group"] for child in children]
    assert "machine" in groups
    assert groups == sorted(groups, key=lambda group: group == "machine")
    assert next(c for c in children if c["path"] == "forum_digest")["group"] == "machine"
    assert next(c for c in children if c["path"] == "chatgpt")["group"] == ""
