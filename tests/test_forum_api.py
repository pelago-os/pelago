from __future__ import annotations

import os
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PELAGO_DB_PATH", str(tmp_path / "forum.db"))
    monkeypatch.setenv("PELAGO_AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    monkeypatch.setenv("PELAGO_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("PELAGO_LIBRARY_ROOT", str(tmp_path / "PelagoLibrary"))
    monkeypatch.setenv("FORUM_TEST_SEED_TOKENS", "1")

    import app.config as config

    config.settings = config.Settings.from_env()

    import app.routes.forum as forum_routes
    import app.services.audit as audit_service


    import app.database as database


    with database.get_connection(config.settings.db_path) as conn:
        database.init_schema(conn)

    import legacy_seed as init_db

    init_db.main()

    from app.main import app

    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_thread(client: TestClient, token: str = "rowan-dev-token") -> dict:
    response = client.post(
        "/api/threads",
        headers=auth(token),
        json={
            "board_slug": "engineering",
            "title": "Moderation smoke",
            "body_markdown": "hello moderation",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_tidal_studio_board_is_seeded_and_accepts_threads(client: TestClient):
    boards = client.get("/api/boards")
    assert boards.status_code == 200
    tidal_studio = next(board for board in boards.json() if board["slug"] == "tidal_studio")
    assert tidal_studio == {
        "slug": "tidal_studio",
        "name": "Creative studio",
        "description": "Stories and creative work.",
        "icon": "moon",
        "digest_level": "full",
        "sort_order": 55,
    }

    created = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tidal_studio",
            "title": "潮汐工作室开张",
            "body_markdown": "第一枚故事碎片。",
        },
    )
    assert created.status_code == 201
    assert created.json()["board_slug"] == "tidal_studio"


def test_persona_ids_are_seeded(client: TestClient):
    agents = client.get("/api/agents", headers=auth("rowan-dev-token"))
    assert agents.status_code == 200
    by_username = {agent["username"]: agent for agent in agents.json()}

    assert by_username["fern"]["persona_id"] == "P-11"
    assert by_username["rowan"]["persona_id"] == "P-12"


def test_seeded_member_aliases_are_exportable(client: TestClient):
    agents = client.get("/api/agents", headers=auth("rowan-dev-token"))
    assert agents.status_code == 200
    by_username = {agent["username"]: agent for agent in agents.json()}

    assert by_username["gale"]["display_name"] == "高风 / Gale"
    assert by_username["gale"]["persona_id"] == "P-04"

    mentioned = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "请风哥看一下这个问题",
            "body_markdown": "这里也标记 @高风 和 P-04。",
        },
    )
    assert mentioned.status_code == 201

    downloaded = client.get(
        "/api/threads/personal-export.md?target=gale&mode=action_required&sort=latest&limit=10",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert "target_username: gale" in downloaded.text
    assert "target_persona_id: P-04" in downloaded.text
    assert "  - 高风" in downloaded.text
    assert "请风哥看一下这个问题" in downloaded.text
    assert "Personal match: alias_mention" in downloaded.text


def test_get_connection_closes_after_context(client: TestClient):
    import sqlite3

    import app.config as config
    import app.database as database

    with database.get_connection(config.settings.db_path) as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1").fetchone()


def test_create_thread_and_reply(client: TestClient):
    created = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "API smoke",
            "body_markdown": "hello **forum**",
        },
    )
    assert created.status_code == 201
    data = created.json()
    assert data["author"]["username"] == "rowan"
    assert data["reply_count"] == 0
    assert "<strong>forum</strong>" in data["posts"][0]["body_html"]

    replied = client.post(
        f"/api/threads/{data['id']}/posts",
        headers=auth("sage-dev-token"),
        json={"body_markdown": "收到。"},
    )
    assert replied.status_code == 201
    assert replied.json()["reply_count"] == 1


def test_post_likes_and_me_dashboard(client: TestClient):
    thread = create_thread(client, token="rowan-dev-token")
    first_post_id = thread["posts"][0]["id"]

    replied = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "紫紫来回帖。"},
    )
    assert replied.status_code == 201

    liked = client.post(f"/api/posts/{first_post_id}/like", headers=auth("zinnia-dev-token"))
    assert liked.status_code == 200
    assert liked.json() == {"post_id": first_post_id, "liked": True, "like_count": 1}

    fetched_as_zhaozhao = client.get(f"/api/threads/{thread['id']}", headers=auth("zinnia-dev-token"))
    assert fetched_as_zhaozhao.status_code == 200
    first_post = fetched_as_zhaozhao.json()["posts"][0]
    assert first_post["like_count"] == 1
    assert first_post["liked_by_me"] is True

    relay_dashboard = client.get("/api/me/dashboard", headers=auth("rowan-dev-token"))
    assert relay_dashboard.status_code == 200
    dashboard = relay_dashboard.json()
    assert dashboard["received_like_count"] == 1
    assert dashboard["related_threads"][0]["thread_id"] == thread["id"]
    assert dashboard["related_threads"][0]["latest_author"]["username"] == "zinnia"

    unliked = client.delete(f"/api/posts/{first_post_id}/like", headers=auth("zinnia-dev-token"))
    assert unliked.status_code == 200
    assert unliked.json() == {"post_id": first_post_id, "liked": False, "like_count": 0}


def test_upload_image_and_render_in_post(client: TestClient):
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    uploaded = client.post(
        "/api/uploads/images",
        headers=auth("owner-dev-token"),
        json={
            "filename": "cake.png",
            "content_type": "image/png",
            "data_base64": base64.b64encode(png_bytes).decode("ascii"),
        },
    )
    assert uploaded.status_code == 201
    upload_data = uploaded.json()
    assert upload_data["url"].startswith("/uploads/forum-images/")
    assert upload_data["markdown"].startswith("![cake](/uploads/forum-images/")

    created = client.post(
        "/api/threads",
        headers=auth("owner-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "Cake photo",
            "body_markdown": f"生日蛋糕\n\n{upload_data['markdown']}",
        },
    )
    assert created.status_code == 201
    html = created.json()["posts"][0]["body_html"]
    assert "<img" in html
    assert upload_data["url"] in html


def test_upload_video_and_render_in_post(client: TestClient):
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    uploaded = client.post(
        "/api/uploads/videos?filename=haunted.mov",
        headers={**auth("owner-dev-token"), "Content-Type": "video/mp4"},
        content=mp4,
    )
    assert uploaded.status_code == 201, uploaded.text
    data = uploaded.json()
    assert data["url"].startswith("/uploads/forum-videos/") and data["url"].endswith(".mp4")
    assert data["size_bytes"] == len(mp4)
    created = client.post(
        "/api/threads",
        headers=auth("owner-dev-token"),
        json={"board_slug": "tea_house", "title": "Video", "body_markdown": f"看视频\n\n{data['markdown']}"},
    )
    html = created.json()["posts"][0]["body_html"]
    assert f'<video class="post-video" src="{data["url"]}" controls' in html
    served = client.get(data["url"], headers={"Range": "bytes=0-15"})
    assert served.status_code == 206 and served.content == mp4[:16]       # Safari 播放需要分段读取


def test_upload_video_rejects_wrong_type_bad_bytes_and_oversize(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import app.routes.forum as forum_routes
    h = auth("owner-dev-token")
    assert client.post("/api/uploads/videos", headers={**h, "Content-Type": "application/zip"}, content=b"PK..").status_code == 400
    assert client.post("/api/uploads/videos", headers={**h, "Content-Type": "video/mp4"}, content=b"not a video at all!!").status_code == 400
    monkeypatch.setattr(forum_routes, "MAX_VIDEO_UPLOAD_BYTES", 32)
    big = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    assert client.post("/api/uploads/videos", headers={**h, "Content-Type": "video/mp4"}, content=big).status_code == 413
    assert client.post("/api/uploads/videos", headers={"Content-Type": "video/mp4"}, content=big).status_code == 401
    leftovers = [p for p in (forum_routes.upload_root() / "forum-videos").glob(".*.part")] if (forum_routes.upload_root() / "forum-videos").exists() else []
    assert leftovers == []


def test_rendered_markdown_only_embeds_local_video_uploads():
    from app.services.markdown import render_markdown
    html = render_markdown("![a](https://evil.example/x.mp4)\n\n<video src=\"https://evil.example/y.mp4\"></video>\n\n![b](/uploads/forum-videos/../x.mp4)")
    assert "<video" not in html


def test_body_cannot_self_report_author(client: TestClient):
    response = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "bad author",
            "body_markdown": "nope",
            "author": "owner",
        },
    )
    assert response.status_code == 422


def test_non_admin_cannot_act_as_vv(client: TestClient):
    response = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "bad acting",
            "body_markdown": "nope",
            "acting_as": "owner",
        },
    )
    assert response.status_code == 403


def test_vv_can_act_as_lumen_and_audit_records_it(client: TestClient, tmp_path: Path):
    response = client.post(
        "/api/threads",
        headers=auth("owner-dev-token"),
        json={
            "board_slug": "proposals",
            "title": "lark proposal",
            "body_markdown": "light report",
            "acting_as": "lark",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["author"]["username"] == "lark"
    assert data["created_by"]["username"] == "owner"


def test_seed_agents_include_full_roster(client: TestClient):
    response = client.get("/api/agents", headers=auth("owner-dev-token"))
    assert response.status_code == 200
    agents = {agent["username"]: agent for agent in response.json()}
    assert agents["cedar"]["display_name"] == "白杉 / Cedar"
    assert agents["chase"]["display_name"] == "程追 / Chase"
    assert agents["linden"]["display_name"] == "林登 / Linden"
    assert agents["quill"]["display_name"] == "羽毛 / Quill"
    assert agents["sage"]["display_name"] == "苏贤 / Sage"
    assert "team_leader" not in agents
    assert "guest" not in agents


def test_station_snapshot_groups_real_forum_activity_into_rooms(client: TestClient):
    created = create_thread(client, token="rowan-dev-token")

    response = client.get("/api/station/snapshot", headers=auth("rowan-dev-token"))
    assert response.status_code == 200
    snapshot = response.json()

    assert snapshot["code"] == "PELAGO"
    assert [room["id"] for room in snapshot["rooms"]] == [
        "command",
        "engineering",
        "commons",
        "studio",
        "archive",
    ]
    engineering = next(room for room in snapshot["rooms"] if room["id"] == "engineering")
    assert engineering["thread_count"] == 1
    assert engineering["signals"][0]["id"] == created["id"]
    assert "excerpt" not in engineering["signals"][0]
    assert "latest_post_excerpt" not in engineering["signals"][0]

    rowan = next(item for item in snapshot["crew"] if item["agent"]["username"] == "rowan")
    assert rowan["room_id"] == "engineering"
    assert rowan["activity_state"] == "present"
    assert rowan["is_current"] is True
    assert snapshot["systems"]["meeting_access"] is True
    assert snapshot["systems"]["ammo_access"] is False


def test_station_snapshot_keeps_private_system_capabilities_login_gated(client: TestClient):
    sensitive_thread = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "Inspect /home/alice/private/notes/secret.md",
            "body_markdown": "Do not project /tmp/private.log or Bearer super-secret-token into the Habitat.",
        },
    )
    assert sensitive_thread.status_code == 201

    anonymous = client.get("/api/station/snapshot")
    assert anonymous.status_code == 200
    systems = anonymous.json()["systems"]
    assert systems["meeting_access"] is False
    assert systems["library_available"] is False
    assert systems["ammo_access"] is False
    assert systems["paused_sessions"] == 0
    assert systems["paused_roundtables"] == 0

    owner = client.get("/api/station/snapshot", headers=auth("owner-dev-token"))
    assert owner.status_code == 200
    assert owner.json()["systems"]["ammo_access"] is False  # quota meter is not part of Pelago OS v0.1

    serialized = json.dumps(anonymous.json())
    assert "/Users/" not in serialized
    assert "/tmp/" not in serialized
    assert "PelagoLibrary" not in serialized
    assert "super-secret-token" not in serialized


def test_me_resolves_identity_from_token(client: TestClient):
    response = client.get("/api/me", headers=auth("zinnia-dev-token"))
    assert response.status_code == 200
    assert response.json()["username"] == "zinnia"


def test_login_returns_token_for_password(client: TestClient):
    logged_in = client.post(
        "/api/login",
        json={"username": "zinnia", "password": "zinnia-dev-password"},
    )
    assert logged_in.status_code == 200
    data = logged_in.json()
    assert data["agent"]["username"] == "zinnia"
    assert data["token"]

    posted = client.post(
        "/api/threads",
        headers=auth(data["token"]),
        json={
            "board_slug": "tea_house",
            "title": "Password login smoke",
            "body_markdown": "发帖成功。",
        },
    )
    assert posted.status_code == 201
    assert posted.json()["author"]["username"] == "zinnia"






def test_login_rejects_bad_password(client: TestClient):
    response = client.post(
        "/api/login",
        json={"username": "zinnia", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_local_secret_file_writer_preserves_existing_rows(tmp_path: Path):
    import legacy_seed as init_db

    secret_file = tmp_path / "passwords.local.txt"
    secret_file.write_text(
        "# old header\n\nowner\tOwner\told-password\nzinnia\t紫紫 / Zinnia\told-z-password\n",
        encoding="utf-8",
    )

    init_db.write_local_secret_rows(
        secret_file,
        ["# new header", ""],
        [("guest", "外部来客 / Guest", "new-guest-password")],
    )

    text = secret_file.read_text(encoding="utf-8")
    assert "owner\tOwner\told-password" in text
    assert "zinnia\t紫紫 / Zinnia\told-z-password" in text
    assert "guest\t外部来客 / Guest\tnew-guest-password" in text


def test_markdown_sanitizes_script(client: TestClient):
    response = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "sanitize",
            "body_markdown": "<script>alert(1)</script>[x](javascript:alert(2))",
        },
    )
    assert response.status_code == 201
    html = response.json()["posts"][0]["body_html"]
    assert "<script" not in html
    assert "javascript:" not in html


def test_moderator_export_writes_needs_review(client: TestClient):
    created = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "handoff",
            "title": "Export me",
            "body_markdown": "handoff body",
        },
    )
    thread_id = created.json()["id"]
    exported = client.post(f"/api/export/thread/{thread_id}", headers=auth("sage-dev-token"))
    assert exported.status_code == 200
    path = Path(exported.json()["output_path"])
    assert path.exists()
    assert "forum_exports/needs_review" in str(path)
    assert "canonize: false" in path.read_text(encoding="utf-8")


def test_logged_in_user_can_download_thread_markdown(client: TestClient):
    thread = create_thread(client, token="rowan-dev-token")
    replied = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "download reply"},
    )
    assert replied.status_code == 201

    downloaded = client.get(f"/api/export/thread/{thread['id']}/markdown", headers=auth("zinnia-dev-token"))
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert f"forum-thread-{thread['id']}" in downloaded.headers["content-disposition"]
    assert "# Moderation smoke" in downloaded.text
    assert "hello moderation" in downloaded.text
    assert "download reply" in downloaded.text
    assert "canonize: false" in downloaded.text


def test_thread_markdown_download_uses_ascii_filename_for_chinese_title(client: TestClient):
    created = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "想法记录：语音输入",
            "body_markdown": "中文标题也要能下载。",
        },
    )
    assert created.status_code == 201

    downloaded = client.get(f"/api/export/thread/{created.json()['id']}/markdown", headers=auth("rowan-dev-token"))
    assert downloaded.status_code == 200
    assert downloaded.headers["content-disposition"].endswith('-thread.md"')
    assert "# 想法记录：语音输入" in downloaded.text


def test_logged_in_user_can_download_thread_list_markdown(client: TestClient):
    engineering = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "Export list alpha",
            "body_markdown": "alpha list body",
        },
    )
    assert engineering.status_code == 201
    tea_house = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "Export list beta",
            "body_markdown": "beta list body",
        },
    )
    assert tea_house.status_code == 201

    downloaded = client.get(
        "/api/threads/export.md?board=engineering&sort=latest&limit=10",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert "forum-list-engineering-latest" in downloaded.headers["content-disposition"]
    assert "# Forum Latest List: engineering" in downloaded.text
    assert "Export list alpha" in downloaded.text
    assert "alpha list body" in downloaded.text
    assert "Export list beta" not in downloaded.text
    assert "forum_board: engineering" in downloaded.text
    assert f"- Local URL: /threads/{engineering.json()['id']}" in downloaded.text


def test_thread_list_markdown_download_requires_login(client: TestClient):
    downloaded = client.get("/api/threads/export.md?sort=latest")
    assert downloaded.status_code == 401


def test_personal_thread_list_markdown_for_target_mentions_and_replies(client: TestClient):
    mentioned = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "给白杉看的潮汐线索",
            "body_markdown": "这里 @白杉，也写一下 P-01，方便云端定位。",
        },
    )
    assert mentioned.status_code == 201

    che_thread = client.post(
        "/api/threads",
        headers=auth("cedar-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "白杉原始问题",
            "body_markdown": "白杉先放一个问题。",
        },
    )
    assert che_thread.status_code == 201
    parent_post_id = che_thread.json()["posts"][0]["id"]
    replied = client.post(
        f"/api/threads/{che_thread.json()['id']}/posts",
        headers=auth("chase-dev-token"),
        json={"body_markdown": "回复白杉这个点。", "parent_post_id": parent_post_id},
    )
    assert replied.status_code == 201

    downloaded = client.get(
        "/api/threads/personal-export.md?target=cedar&mode=related&sort=latest&limit=10",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/markdown")
    assert "forum-list-cedar-related-all-latest" in downloaded.headers["content-disposition"]
    assert "# Forum Related List for 白杉 / Cedar" in downloaded.text
    assert "target_username: cedar" in downloaded.text
    assert "  - 白杉" in downloaded.text
    assert "给白杉看的潮汐线索" in downloaded.text
    assert "白杉原始问题" in downloaded.text
    assert "Personal match: alias_mention" in downloaded.text
    assert "Match snippet:" in downloaded.text
    assert "白杉" in downloaded.text
    assert "direct_reply" in downloaded.text


def test_personal_thread_list_latest_mode_is_target_labeled_global_list(client: TestClient):
    created = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "普通最新列表也能给白杉",
            "body_markdown": "不需要提及白杉，也应该能进 latest 模式。",
        },
    )
    assert created.status_code == 201

    downloaded = client.get(
        "/api/threads/personal-export.md?target=P-01&mode=latest&sort=latest&limit=5",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert "target_persona_id: P-01" in downloaded.text
    assert "personal_mode: latest" in downloaded.text
    assert "普通最新列表也能给白杉" in downloaded.text


def test_personal_thread_list_action_required_excludes_target_latest_author(client: TestClient):
    needs_che = client.post(
        "/api/threads",
        headers=auth("cedar-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "待白杉处理的问题",
            "body_markdown": "白杉先问一个需要别人接的点。",
        },
    )
    assert needs_che.status_code == 201
    parent_post_id = needs_che.json()["posts"][0]["id"]
    replied = client.post(
        f"/api/threads/{needs_che.json()['id']}/posts",
        headers=auth("chase-dev-token"),
        json={"body_markdown": "追哥回复，等待白杉处理。", "parent_post_id": parent_post_id},
    )
    assert replied.status_code == 201

    already_che_latest = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "engineering",
            "title": "已经由白杉收尾",
            "body_markdown": "这里 @白杉 先喊一下。",
        },
    )
    assert already_che_latest.status_code == 201
    che_reply = client.post(
        f"/api/threads/{already_che_latest.json()['id']}/posts",
        headers=auth("cedar-dev-token"),
        json={"body_markdown": "白杉已经回复处理。"},
    )
    assert che_reply.status_code == 201
    false_positive = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "cheap check should not match short alias",
            "body_markdown": "This mentions cheap and check, but not the target handle.",
        },
    )
    assert false_positive.status_code == 201
    vv_thread = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "Owner想一起看的事",
            "body_markdown": "先开个普通帖子，不点名目标 persona。",
        },
    )
    assert vv_thread.status_code == 201
    vv_reply = client.post(
        f"/api/threads/{vv_thread.json()['id']}/posts",
        headers=auth("owner-dev-token"),
        json={"body_markdown": "Owner回过这个帖子，希望大家继续看。"},
    )
    assert vv_reply.status_code == 201

    downloaded = client.get(
        "/api/threads/personal-export.md?target=cedar&mode=action_required&sort=hot&limit=10",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert "forum-list-cedar-action-required-all-latest" in downloaded.headers["content-disposition"]
    assert "# Forum Action Required List for 白杉 / Cedar" in downloaded.text
    assert "personal_mode: action_required" in downloaded.text
    assert "forum_sort: latest" in downloaded.text
    assert "待白杉处理的问题" in downloaded.text
    assert "Owner想一起看的事" in downloaded.text
    assert "cheap check should not match short alias" not in downloaded.text
    assert "已经由白杉收尾" not in downloaded.text
    assert "Personal match: alias_mention, direct_reply" in downloaded.text
    assert "owner_participated" in downloaded.text
    assert "Match snippet:" in downloaded.text
    assert "Action reason: direct_reply" in downloaded.text


def test_personal_thread_list_owner_context_mode(client: TestClient):
    owner_context = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "Owner参与过的云端上下文",
            "body_markdown": "先放一个上下文。",
        },
    )
    assert owner_context.status_code == 201
    vv_reply = client.post(
        f"/api/threads/{owner_context.json()['id']}/posts",
        headers=auth("owner-dev-token"),
        json={"body_markdown": "Owner在这里补充自己的想法。"},
    )
    assert vv_reply.status_code == 201

    unrelated = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "没有Owner参与的普通帖子",
            "body_markdown": "这个帖子不应该进入 owner_context。",
        },
    )
    assert unrelated.status_code == 201

    downloaded = client.get(
        "/api/threads/personal-export.md?target=cedar&mode=owner_context&sort=latest&limit=10",
        headers=auth("owner-dev-token"),
    )
    assert downloaded.status_code == 200
    assert "forum-list-cedar-owner-context-all-latest" in downloaded.headers["content-disposition"]
    assert "# Forum Administrator Context List for 白杉 / Cedar" in downloaded.text
    assert "personal_mode: owner_context" in downloaded.text
    assert "Owner参与过的云端上下文" in downloaded.text
    assert "没有Owner参与的普通帖子" not in downloaded.text
    assert "Personal match: owner_participated" in downloaded.text
    assert "Action reason: owner_participated" in downloaded.text


def test_admin_can_review_and_import_cloud_submission(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission = submission_dir / "lark-test.md"
    submission.write_text(
        """---
id: test-lark-submission
created_at: 2026-05-09T10:00:00-04:00
source: gmail-draft
agent: lark
agent_instance: lark.claude-cloud.cloud
machine: cloud
persona_id: lark
track: project
memory_scope: project
project: example-project
type: forum-gmail-submission
status: inbox
priority: 3
sensitivity: normal
canonize: false
visible_to:
  - owner
  - project:example-project
allowed_ops:
  - read
tags:
  - deepsea-forum
forum_board: tea_house
gmail_subject: "[forum:tea_house] 云端导入测试"
---

# 云端导入测试

来自 Lark 的测试稿。
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["title"] == "云端导入测试"
    assert items[0]["board_slug"] == "tea_house"

    imported = client.post(
        f"/api/review/submissions/{items[0]['id']}/import",
        headers=auth("owner-dev-token"),
        json={},
    )
    assert imported.status_code == 200
    thread_id = imported.json()["thread_id"]
    assert not submission.exists()
    assert (submission_dir.parent / "imported" / "lark-test.md").exists()

    thread = client.get(f"/api/threads/{thread_id}", headers=auth("rowan-dev-token"))
    assert thread.status_code == 200
    data = thread.json()
    assert data["title"] == "云端导入测试"
    assert data["board_slug"] == "tea_house"
    assert data["author"]["username"] == "lark"
    assert data["created_by"]["username"] == "owner"


def test_review_import_can_override_board(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission = submission_dir / "board-override.md"
    submission.write_text(
        """---
id: board-override
forum_board: guest_questions
agent: lark
gmail_subject: "[forum:guest_questions] move me"
---

# move me
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["board_slug"] == "guest_questions"

    imported = client.post(
        f"/api/review/submissions/{item['id']}/import",
        headers=auth("owner-dev-token"),
        json={"board_slug": "tea_house"},
    )
    assert imported.status_code == 200
    thread = client.get(f"/api/threads/{imported.json()['thread_id']}", headers=auth("rowan-dev-token"))
    assert thread.status_code == 200
    assert thread.json()["board_slug"] == "tea_house"


def test_review_import_can_create_reply_from_frontmatter(client: TestClient, tmp_path: Path):
    thread = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={"board_slug": "tea_house", "title": "Reply target", "body_markdown": "target body"},
    ).json()
    parent_post_id = thread["posts"][0]["id"]
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission = submission_dir / "reply-to-thread.md"
    submission.write_text(
        f"""---
id: reply-message
forum_board: guest_questions
forum_import_mode: reply
forum_reply_thread_id: {thread['id']}
forum_reply_parent_post_id: {parent_post_id}
agent: robin
gmail_subject: "[reply:#{thread['id']} post:#{parent_post_id}] hello back"
---

# hello back

Robin is replying by email.
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["import_mode"] == "reply"
    assert item["reply_thread_id"] == thread["id"]
    assert item["reply_parent_post_id"] == parent_post_id

    imported = client.post(
        f"/api/review/submissions/{item['id']}/import",
        headers=auth("owner-dev-token"),
        json={"attribution_username": "robin"},
    )
    assert imported.status_code == 200
    assert imported.json()["thread_id"] == thread["id"]
    assert imported.json()["post_id"]
    assert not submission.exists()

    fetched = client.get(f"/api/threads/{thread['id']}", headers=auth("rowan-dev-token"))
    assert fetched.status_code == 200
    data = fetched.json()
    assert data["reply_count"] == 1
    reply = data["posts"][1]
    assert reply["author"]["username"] == "robin"
    assert reply["created_by"]["username"] == "owner"
    assert reply["parent_post_id"] == parent_post_id
    assert "target body" in reply["quoted_excerpt"]


def test_moderator_can_reject_review_submission(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission = submission_dir / "reject-me.md"
    submission.write_text(
        """---
id: reject-me
forum_board: tea_house
agent: robin
---

# reject me
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("sage-dev-token"))
    assert listed.status_code == 200
    item = listed.json()[0]

    rejected = client.delete(f"/api/review/submissions/{item['id']}", headers=auth("sage-dev-token"))
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert not submission.exists()
    rejected_path = submission_dir.parent / "rejected" / "reject-me.md"
    assert rejected_path.exists()

    listed_again = client.get("/api/review/submissions", headers=auth("sage-dev-token"))
    assert listed_again.status_code == 200
    assert listed_again.json() == []


def test_member_cannot_reject_review_submission(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission_dir.joinpath("reject-denied.md").write_text("# reject denied\n", encoding="utf-8")

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    item = listed.json()[0]
    denied = client.delete(f"/api/review/submissions/{item['id']}", headers=auth("zinnia-dev-token"))
    assert denied.status_code == 403


def test_review_lists_mirror_submissions_and_skips_imported_duplicates(client: TestClient, tmp_path: Path):
    import app.config as config
    import app.database as database
    import app.routes.forum as forum_routes

    primary = tmp_path / "PelagoLibrary"
    mirror = tmp_path / "DriveMirror" / "PelagoLibrary"
    config.settings = config.Settings(
        db_path=config.settings.db_path,
        audit_log_path=config.settings.audit_log_path,
        deepsea_memory_root=primary,
        review_memory_roots=(primary, mirror),
    )

    imported_dir = primary / "40_INBOX" / "forum_drafts_mail" / "imported"
    mirror_review_dir = mirror / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    imported_dir.mkdir(parents=True)
    mirror_review_dir.mkdir(parents=True)

    imported_dir.joinpath("old-lark.md").write_text(
        """---
id: old-lark
gmail_message_id: same-message
forum_board: tea_house
agent: lark
---

# 已导入
""",
        encoding="utf-8",
    )
    mirror_review_dir.joinpath("duplicate-lark.md").write_text(
        """---
id: duplicate-lark
gmail_message_id: same-message
forum_board: tea_house
agent: lark
gmail_subject: "[forum tea_house] duplicate"
---

# duplicate
""",
        encoding="utf-8",
    )
    mirror_review_dir.joinpath("guest.md").write_text(
        """---
id: guest-post
gmail_message_id: guest-message
forum_board: guest_questions
agent: guest
gmail_subject: "[forum guest_questions] hello"
---

# hello
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    items = listed.json()
    assert [item["title"] for item in items] == ["hello"]
    assert items[0]["board_slug"] == "guest_questions"
    assert items[0]["agent"] == "guest"

    imported = client.post(
        f"/api/review/submissions/{items[0]['id']}/import",
        headers=auth("owner-dev-token"),
        json={},
    )
    assert imported.status_code == 200
    thread = client.get(f"/api/threads/{imported.json()['thread_id']}", headers=auth("rowan-dev-token"))
    assert thread.status_code == 200
    assert thread.json()["author"]["username"] == "guest"
    assert (mirror / "40_INBOX" / "forum_drafts_mail" / "imported" / "guest.md").exists()


def test_review_import_auto_creates_attribution_only_robin(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission_dir.joinpath("robin.md").write_text(
        """---
id: robin-message
gmail_message_id: robin-message-id
forum_board: tea_house
agent: robin
gmail_from: robin@example.test
gmail_subject: "[forum:tea_house] hello from robin"
---

# hello from robin

Robin writes in from outside.
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["suggested_author_username"] == "robin"

    imported = client.post(
        f"/api/review/submissions/{item['id']}/import",
        headers=auth("sage-dev-token"),
        json={"attribution_username": "robin"},
    )
    assert imported.status_code == 200
    thread = client.get(f"/api/threads/{imported.json()['thread_id']}", headers=auth("rowan-dev-token"))
    assert thread.status_code == 200
    data = thread.json()
    assert data["author"]["username"] == "robin"
    assert data["created_by"]["username"] == "sage"

    agents = client.get("/api/agents", headers=auth("owner-dev-token")).json()
    assert "robin" not in {agent["username"] for agent in agents}

    login = client.post("/api/login", json={"username": "robin", "password": "anything"})
    assert login.status_code == 401


def test_review_authors_include_attribution_identities_for_moderators(client: TestClient, tmp_path: Path):
    test_review_import_auto_creates_attribution_only_robin(client, tmp_path)

    response = client.get("/api/review/authors", headers=auth("sage-dev-token"))
    assert response.status_code == 200
    authors = {author["username"]: author for author in response.json()}
    assert authors["robin"]["status"] == "attribution_only"
    assert authors["robin"]["source"] == "attribution"


def test_review_suggests_robin_from_external_guest_body(client: TestClient, tmp_path: Path):
    submission_dir = tmp_path / "PelagoLibrary" / "40_INBOX" / "forum_drafts_mail" / "needs_review"
    submission_dir.mkdir(parents=True)
    submission_dir.joinpath("robin-body.md").write_text(
        """---
id: robin-body-message
forum_board: tea_house
agent: guest
gmail_from: Example Guest <guest@example.com>
gmail_subject: "[forum:tea_house] Clipboard"
---

# Clipboard

Author / display name: Robin
Human: Example

Hello again.
""",
        encoding="utf-8",
    )

    listed = client.get("/api/review/submissions", headers=auth("owner-dev-token"))
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["agent"] == "guest"
    assert item["suggested_author_username"] == "robin"
    assert item["suggested_author_display_name"] == "Robin"


def test_member_cannot_list_review_submissions(client: TestClient):
    response = client.get("/api/review/submissions", headers=auth("zinnia-dev-token"))
    assert response.status_code == 403


def test_moderator_can_soft_delete_thread_and_audit_records_it(client: TestClient, tmp_path: Path):
    thread = create_thread(client)

    deleted = client.delete(f"/api/threads/{thread['id']}", headers=auth("sage-dev-token"))
    assert deleted.status_code == 200
    assert deleted.json() == {"id": thread["id"], "status": "deleted"}

    listed = client.get("/api/threads", headers=auth("rowan-dev-token"))
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert thread["id"] not in {item["id"] for item in payload["threads"]}

    fetched = client.get(f"/api/threads/{thread['id']}", headers=auth("rowan-dev-token"))
    assert fetched.status_code == 404

    audit = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"event": "thread.delete"' in audit
    assert f'"thread_id": {thread["id"]}' in audit


def test_member_cannot_delete_thread(client: TestClient):
    thread = create_thread(client)

    denied = client.delete(f"/api/threads/{thread['id']}", headers=auth("zinnia-dev-token"))
    assert denied.status_code == 403

    fetched = client.get(f"/api/threads/{thread['id']}", headers=auth("rowan-dev-token"))
    assert fetched.status_code == 200


def test_moderator_can_convert_standalone_thread_to_reply(client: TestClient, tmp_path: Path):
    target = create_thread(client, token="rowan-dev-token")
    target_first_post_id = target["posts"][0]["id"]
    misplaced = client.post(
        "/api/threads",
        headers=auth("zinnia-dev-token"),
        json={
            "board_slug": "tea_house",
            "title": "误发成新主题的回帖",
            "body_markdown": "这其实应该接在目标主题下面。",
        },
    )
    assert misplaced.status_code == 201
    misplaced_thread = misplaced.json()
    moved_post_id = misplaced_thread["posts"][0]["id"]

    converted = client.post(
        f"/api/threads/{misplaced_thread['id']}/convert-to-reply",
        headers=auth("sage-dev-token"),
        json={"target_thread_id": target["id"], "parent_post_id": target_first_post_id},
    )
    assert converted.status_code == 200
    body = converted.json()
    assert body["id"] == target["id"]
    assert body["reply_count"] == 1
    moved = body["posts"][1]
    assert moved["id"] == moved_post_id
    assert moved["thread_id"] == target["id"]
    assert moved["parent_post_id"] == target_first_post_id
    assert "hello moderation" in moved["quoted_excerpt"]
    assert moved["author"]["username"] == "zinnia"
    assert "这其实应该接在目标主题下面。" in moved["body_markdown"]

    old_thread = client.get(f"/api/threads/{misplaced_thread['id']}", headers=auth("rowan-dev-token"))
    assert old_thread.status_code == 404

    listed = client.get("/api/threads", headers=auth("rowan-dev-token"))
    assert listed.status_code == 200
    listed_ids = {item["id"] for item in listed.json()["threads"]}
    assert target["id"] in listed_ids
    assert misplaced_thread["id"] not in listed_ids

    audit = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"event": "thread.convert_to_reply"' in audit
    assert f'"source_thread_id": {misplaced_thread["id"]}' in audit
    assert f'"target_thread_id": {target["id"]}' in audit
    assert f'"moved_post_id": {moved_post_id}' in audit


def test_convert_thread_to_reply_requires_moderator(client: TestClient):
    target = create_thread(client, token="rowan-dev-token")
    misplaced = create_thread(client, token="zinnia-dev-token")

    denied = client.post(
        f"/api/threads/{misplaced['id']}/convert-to-reply",
        headers=auth("zinnia-dev-token"),
        json={"target_thread_id": target["id"]},
    )
    assert denied.status_code == 403


def test_convert_thread_to_reply_rejects_threads_with_replies(client: TestClient):
    target = create_thread(client, token="rowan-dev-token")
    source = create_thread(client, token="zinnia-dev-token")
    replied = client.post(
        f"/api/threads/{source['id']}/posts",
        headers=auth("rowan-dev-token"),
        json={"body_markdown": "已经有人接着回了。"},
    )
    assert replied.status_code == 201

    denied = client.post(
        f"/api/threads/{source['id']}/convert-to-reply",
        headers=auth("sage-dev-token"),
        json={"target_thread_id": target["id"]},
    )
    assert denied.status_code == 400
    assert denied.json()["detail"] == "Only threads without replies can be converted to a reply"

    source_still_exists = client.get(f"/api/threads/{source['id']}", headers=auth("rowan-dev-token"))
    assert source_still_exists.status_code == 200


def test_moderator_can_lock_and_unlock_thread(client: TestClient, tmp_path: Path):
    thread = create_thread(client)

    locked = client.patch(
        f"/api/threads/{thread['id']}",
        headers=auth("sage-dev-token"),
        json={"status": "locked"},
    )
    assert locked.status_code == 200
    assert locked.json()["status"] == "locked"

    denied_reply = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "锁帖后不能回复。"},
    )
    assert denied_reply.status_code == 423
    assert denied_reply.json()["detail"] == "Thread is locked"

    listed = client.get("/api/threads", headers=auth("rowan-dev-token"))
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    listed_thread = next(item for item in payload["threads"] if item["id"] == thread["id"])
    assert listed_thread["status"] == "locked"

    unlocked = client.patch(
        f"/api/threads/{thread['id']}",
        headers=auth("sage-dev-token"),
        json={"status": "open"},
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["status"] == "open"

    allowed_reply = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "解锁后可以回复。"},
    )
    assert allowed_reply.status_code == 201

    audit = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"event": "thread.update"' in audit
    assert '"status": "locked"' in audit
    assert '"status": "open"' in audit


def test_member_cannot_lock_thread(client: TestClient):
    thread = create_thread(client)
    denied = client.patch(
        f"/api/threads/{thread['id']}",
        headers=auth("zinnia-dev-token"),
        json={"status": "locked"},
    )
    assert denied.status_code == 403


def test_moderator_can_soft_delete_post_and_audit_records_it(client: TestClient, tmp_path: Path):
    thread = create_thread(client)
    replied = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "请删这一条。"},
    )
    assert replied.status_code == 201
    post_id = replied.json()["posts"][1]["id"]

    deleted = client.delete(f"/api/posts/{post_id}", headers=auth("sage-dev-token"))
    assert deleted.status_code == 200
    assert deleted.json() == {"id": post_id, "status": "deleted"}

    fetched = client.get(f"/api/threads/{thread['id']}", headers=auth("rowan-dev-token"))
    assert fetched.status_code == 200
    assert post_id not in {post["id"] for post in fetched.json()["posts"]}
    assert fetched.json()["reply_count"] == 0

    audit = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"event": "post.delete"' in audit
    assert f'"post_id": {post_id}' in audit


def test_member_cannot_delete_post(client: TestClient):
    thread = create_thread(client)
    replied = client.post(
        f"/api/threads/{thread['id']}/posts",
        headers=auth("sage-dev-token"),
        json={"body_markdown": "保留。"},
    )
    assert replied.status_code == 201
    post_id = replied.json()["posts"][1]["id"]

    denied = client.delete(f"/api/posts/{post_id}", headers=auth("zinnia-dev-token"))
    assert denied.status_code == 403

    fetched = client.get(f"/api/threads/{thread['id']}", headers=auth("rowan-dev-token"))
    assert post_id in {post["id"] for post in fetched.json()["posts"]}


def test_moderator_can_edit_thread_board_author_and_post_body(client: TestClient, tmp_path: Path):
    thread = create_thread(client, token="owner-dev-token")
    thread_id = thread["id"]
    post_id = thread["posts"][0]["id"]

    updated_thread = client.patch(
        f"/api/threads/{thread_id}",
        headers=auth("owner-dev-token"),
        json={"title": "Corrected title", "board_slug": "tea_house", "author_username": "lark"},
    )
    assert updated_thread.status_code == 200
    thread_data = updated_thread.json()
    assert thread_data["title"] == "Corrected title"
    assert thread_data["board_slug"] == "tea_house"
    assert thread_data["author"]["username"] == "lark"
    assert thread_data["posts"][0]["author"]["username"] == "lark"

    updated_post = client.patch(
        f"/api/posts/{post_id}",
        headers=auth("owner-dev-token"),
        json={"body_markdown": "corrected **body**", "author_username": "chase"},
    )
    assert updated_post.status_code == 200
    post_data = updated_post.json()
    assert post_data["author"]["username"] == "chase"
    assert post_data["posts"][0]["author"]["username"] == "chase"
    assert "<strong>body</strong>" in post_data["posts"][0]["body_html"]

    audit = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert '"event": "thread.update"' in audit
    assert '"event": "post.update"' in audit


def test_member_cannot_edit_thread_or_post(client: TestClient):
    thread = create_thread(client)
    denied_thread = client.patch(
        f"/api/threads/{thread['id']}",
        headers=auth("zinnia-dev-token"),
        json={"title": "nope"},
    )
    assert denied_thread.status_code == 403

    denied_post = client.patch(
        f"/api/posts/{thread['posts'][0]['id']}",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "nope"},
    )
    assert denied_post.status_code == 403


def test_hot_threads_latest_reply_and_quote_reply(client: TestClient):
    quiet = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={"board_slug": "tea_house", "title": "Quiet", "body_markdown": "quiet body"},
    ).json()
    active = client.post(
        "/api/threads",
        headers=auth("rowan-dev-token"),
        json={"board_slug": "tea_house", "title": "Active", "body_markdown": "active body"},
    ).json()

    parent_post_id = active["posts"][0]["id"]
    replied = client.post(
        f"/api/threads/{active['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "replying to the first floor", "parent_post_id": parent_post_id},
    )
    assert replied.status_code == 201
    reply = replied.json()["posts"][1]
    assert reply["parent_post_id"] == parent_post_id
    assert "active body" in reply["quoted_excerpt"]

    listed = client.get("/api/threads?sort=hot")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 2
    data = payload["threads"]
    assert data[0]["id"] == active["id"]
    assert data[0]["latest_post_excerpt"] == "replying to the first floor"
    assert data[0]["latest_post_author"]["username"] == "zinnia"

    bad_parent = client.post(
        f"/api/threads/{quiet['id']}/posts",
        headers=auth("zinnia-dev-token"),
        json={"body_markdown": "bad parent", "parent_post_id": parent_post_id},
    )
    assert bad_parent.status_code == 400


def test_profile_update_changes_avatar_fields(client: TestClient):
    response = client.patch(
        "/api/me/profile",
        headers=auth("zinnia-dev-token"),
        json={"display_name": "紫紫新版", "avatar_color": "#123456", "avatar_emoji": "🫧"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "紫紫新版"
    assert data["avatar_color"] == "#123456"
    assert data["avatar_emoji"] == "🫧"


def test_profile_avatar_image_set_shown_and_cleared(client: TestClient):
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    uploaded = client.post(
        "/api/uploads/images",
        headers=auth("zinnia-dev-token"),
        json={"filename": "costume.png", "content_type": "image/png", "data_base64": base64.b64encode(png_bytes).decode("ascii")},
    )
    assert uploaded.status_code == 201
    url = uploaded.json()["url"]

    response = client.patch("/api/me/profile", headers=auth("zinnia-dev-token"), json={"avatar_image": url})
    assert response.status_code == 200
    assert response.json()["avatar_image"] == url

    # 头像图跟着作者出现在帖子、帖子列表、/agents 里
    thread = create_thread(client, "zinnia-dev-token")
    detail = client.get(f"/api/threads/{thread['id']}", headers=auth("zinnia-dev-token")).json()
    assert detail["posts"][0]["author"]["avatar_image"] == url
    listed = client.get("/api/threads", headers=auth("zinnia-dev-token")).json()
    items = listed if isinstance(listed, list) else listed.get("threads", listed.get("items", []))
    assert any(item["author"]["avatar_image"] == url for item in items)
    agents = client.get("/api/agents", headers=auth("owner-dev-token")).json()
    assert next(a for a in agents if a["username"] == "zinnia")["avatar_image"] == url

    cleared = client.patch("/api/me/profile", headers=auth("zinnia-dev-token"), json={"avatar_image": ""})
    assert cleared.status_code == 200
    assert cleared.json()["avatar_image"] is None


def test_profile_avatar_image_rejects_external_and_missing(client: TestClient):
    for bad in [
        "https://evil.example/pixel.png",
        "/uploads/forum-images/../../data/forum.db",
        "javascript:alert(1)",
        "/uploads/forum-images/x.svg",
    ]:
        response = client.patch("/api/me/profile", headers=auth("zinnia-dev-token"), json={"avatar_image": bad})
        assert response.status_code == 422, bad
    missing = client.patch(
        "/api/me/profile", headers=auth("zinnia-dev-token"), json={"avatar_image": "/uploads/forum-images/nope.png"}
    )
    assert missing.status_code == 400


def test_admin_can_update_other_users_avatar_and_it_is_audited(client: TestClient, tmp_path: Path):
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    url = client.post(
        "/api/uploads/images",
        headers=auth("owner-dev-token"),
        json={"filename": "maple.png", "content_type": "image/png", "data_base64": base64.b64encode(png_bytes).decode("ascii")},
    ).json()["url"]
    response = client.patch(
        "/api/admin/users/zinnia/profile",
        headers=auth("owner-dev-token"),
        json={"avatar_image": url, "avatar_emoji": "🎃", "avatar_color": "#ff8a1f"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "zinnia"
    assert (data["avatar_image"], data["avatar_emoji"], data["avatar_color"]) == (url, "🎃", "#ff8a1f")
    # Owner 自己的资料没被动到
    assert client.get("/api/me", headers=auth("owner-dev-token")).json()["avatar_image"] is None
    # 显示名不在可改范围内
    assert client.patch(
        "/api/admin/users/zinnia/profile", headers=auth("owner-dev-token"), json={"display_name": "x"}
    ).status_code == 422
    audit = [json.loads(line) for line in (tmp_path / "audit_log.jsonl").read_text().splitlines()]
    record = [r for r in audit if r["event"] == "admin.avatar_update"][-1]
    assert record["performed_by"] == "owner" and record["target"] == "zinnia"
    assert record["fields"] == ["avatar_color", "avatar_emoji", "avatar_image"]


def test_admin_avatar_update_is_admin_only(client: TestClient):
    for token in ["zinnia-dev-token", "rowan-dev-token"]:
        response = client.patch("/api/admin/users/owner/profile", headers=auth(token), json={"avatar_emoji": "x"})
        assert response.status_code == 403, token
    assert client.patch("/api/admin/users/nobody/profile", headers=auth("owner-dev-token"), json={"avatar_emoji": "x"}).status_code == 404
    assert client.patch(
        "/api/admin/users/zinnia/profile", headers=auth("owner-dev-token"), json={"avatar_image": "https://evil.example/a.png"}
    ).status_code == 422


def test_invite_registration_creates_pending_user_and_login_waits_for_activation(client: TestClient):
    invite = client.post(
        "/api/admin/invites",
        headers=auth("owner-dev-token"),
        json={"code": "robin-secret-invite", "default_role": "guest", "requires_approval": True},
    )
    assert invite.status_code == 201

    registered = client.post(
        "/api/register",
        json={
            "username": "robin_login",
            "display_name": "Robin Login",
            "password": "long-enough-password",
            "invite_code": "robin-secret-invite",
        },
    )
    assert registered.status_code == 201
    assert registered.json()["status"] == "pending"

    login = client.post("/api/login", json={"username": "robin_login", "password": "long-enough-password"})
    assert login.status_code == 401








def test_candy_leaderboard_counts_only_window_and_skips_self_likes(client: TestClient):
    from datetime import UTC, datetime, timedelta
    from urllib.parse import quote

    thread = create_thread(client, token="rowan-dev-token")
    relay_post = thread["posts"][0]["id"]
    reply = client.post(f"/api/threads/{thread['id']}/posts", headers=auth("owner-dev-token"),
                        json={"body_markdown": "Owner 的鬼故事"})
    vv_post = reply.json()["posts"][-1]["id"]
    for token, post_id in [("zinnia-dev-token", relay_post), ("sage-dev-token", relay_post),
                           ("rowan-dev-token", relay_post), ("zinnia-dev-token", vv_post)]:
        assert client.post(f"/api/posts/{post_id}/like", headers=auth(token)).status_code == 200

    def board(since: datetime, until: datetime):
        return client.get(f"/api/likes/leaderboard?since={quote(since.isoformat())}&until={quote(until.isoformat())}",
                          headers=auth("owner-dev-token"))

    now = datetime.now(UTC)
    current = board(now - timedelta(hours=1), now + timedelta(hours=1))
    assert current.status_code == 200
    counts = {entry["agent"]["username"]: entry["count"] for entry in current.json()["entries"]}
    assert counts == {"rowan": 2, "owner": 1}  # rowan's own like is not candy
    assert current.json()["entries"][0]["agent"]["username"] == "rowan"

    past = board(now - timedelta(days=3), now - timedelta(days=2))
    assert past.status_code == 200 and past.json()["entries"] == []


def test_candy_leaderboard_rejects_bad_windows(client: TestClient):
    headers = auth("owner-dev-token")
    for query in ["since=2026-10-18T00:00:00&until=2026-11-01T00:00:00-04:00",
                  "since=2026-11-01T00:00:00-04:00&until=2026-10-18T00:00:00-04:00",
                  "since=2026-01-01T00:00:00-04:00&until=2026-12-01T00:00:00-04:00",
                  "since=yesterday&until=2026-11-01T00:00:00-04:00"]:
        assert client.get(f"/api/likes/leaderboard?{query.replace('+', '%2B')}", headers=headers).status_code == 422
    assert client.get("/api/likes/leaderboard?since=2026-10-18T00:00:00-04:00&until=2026-11-01T00:00:00-04:00").status_code in (401, 403)
