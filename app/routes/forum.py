from __future__ import annotations

import secrets
import base64
import binascii
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.auth import Actor, hash_password, hash_token, optional_actor, require_actor, resolve_acting_user_id, verify_password
from app.services.access_policy import can_read_board, require_board, require_thread_board, board_filter
from app.services.upload_access import record_upload, set_media_cookie
from app import config
from app.database import get_connection
from app.schemas import (
    AdminAvatarUpdate, Agent, Board, CandyBoard, CandyEntry, DeleteResult, ExportResult, ImageUploadRequest, ImageUploadResult,
    InviteCreate, InviteCreateResult, LoginRequest, LoginResult, MeDashboard,
    ProfileUpdate, PostLikeResult, ReplyCreate, RegisterRequest, RegisterResult,
    ReviewDeleteResult, ReviewAuthor, ReviewImportRequest, ReviewImportResult,
    ReviewSubmissionDetail, ReviewSubmissionSummary, PostUpdate, ThreadCreate,
    ThreadConvertToReplyRequest, ThreadDetail, ThreadListResponse, ThreadUpdate,
    RelatedThread,
)
from app.services.forum_exports import thread_export_slug, render_thread_list_markdown, render_export_markdown
from app.services.audit import append_audit, queue_mutation_audit, deliver_mutation_audit
from app.services.markdown import plain_excerpt, render_markdown
from app.services.login_limits import check_login_limit, clear_login_failures, login_source

from app.services import forum_review, forum_imports, forum_queries
# Keep the helper imports used by existing callers; new jobs import services directly.
from app.services.forum_queries import (
    agent_from_prefix, agent_from_user_row, alias_match_snippets,
    fetch_thread_summaries, get_thread_detail, personal_match_reasons,
)
from app.services.forum_review import (
    encode_submission_id,
    normalized_username,
    parse_review_markdown,
    require_moderator,
    require_thread_open_for_reply,
    resolve_moderator_author_id,
    validate_parent_post
)

def check_forum_access(request: Request, actor: Actor | None = Depends(optional_actor)) -> None:
    thread_id = request.path_params.get('thread_id')
    post_id = request.path_params.get('post_id')
    if thread_id is None and post_id is None:
        return
    with get_connection() as conn:
        if post_id is not None:
            row = conn.execute('SELECT thread_id FROM posts WHERE id=?', (post_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail='Post not found')
            thread_id = row['thread_id']
        # Export is a read even when requested via POST.
        write = request.method not in {'GET', 'HEAD'} and '/export/' not in request.url.path
        require_thread_board(conn, actor, thread_id, write=write)


router = APIRouter(prefix="/api", dependencies=[Depends(check_forum_access)])


ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
# 视频：iPhone 拍的 .mov、通用 .mp4、.webm。原始字节流上传，边收边写盘，不在内存里攒。
ALLOWED_VIDEO_TYPES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}
MAX_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024


def upload_root() -> Path:
    return config.settings.upload_root or config.settings.db_path.parent / "uploads"


def image_type_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def video_type_matches(content_type: str, head: bytes) -> bool:
    """按文件头确认真的是视频，不只看后缀：MP4/MOV 第 4–8 字节是 ftyp，WebM 以 EBML 头 1A45DFA3 开头。"""
    if content_type in ("video/mp4", "video/quicktime"):
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if content_type == "video/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def safe_upload_alt(filename: str) -> str:
    stem = Path(filename).stem.strip() or "image"
    return re.sub(r"[\[\]\(\)\n\r]+", " ", stem).strip()[:80] or "image"


def post_like_count(conn, post_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM post_likes WHERE post_id = ?", (post_id,)).fetchone()
    return int(row["count"] if row else 0)


@router.get("/boards", response_model=list[Board])
def list_boards(actor: Actor | None = Depends(optional_actor)) -> list[Board]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT slug, name, description, icon, digest_level, sort_order
            FROM boards
            WHERE is_archived = 0
            ORDER BY sort_order ASC, slug ASC
            """
        ).fetchall()
    return [Board(**dict(row)) for row in rows if can_read_board(actor, row["slug"])]


@router.get("/agents", response_model=list[Agent])
def list_agents(actor: Actor = Depends(require_actor)):
    from fastapi.responses import JSONResponse
    from app.services.access_policy import PUBLIC_READ
    if not actor.is_crew and actor.membership_kind != "system":
        with get_connection() as conn:
            slugs = sorted(PUBLIC_READ)
            rows = conn.execute(f"""SELECT DISTINCT u.username,u.display_name,u.avatar_color,u.avatar_emoji,u.avatar_image
                FROM users u JOIN posts p ON p.author_user_id=u.id
                JOIN threads t ON t.id=p.thread_id JOIN boards b ON b.id=t.board_id
                WHERE u.status='active' AND p.status='visible' AND t.status!='deleted'
                AND b.slug IN ({','.join('?' for _ in slugs)}) ORDER BY u.display_name""", slugs).fetchall()
        return JSONResponse([dict(row) for row in rows])
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT username, display_name, role, persona_id, avatar_color, avatar_emoji, avatar_image, membership_kind
            FROM users
            WHERE status = 'active'
            ORDER BY display_name ASC
            """
        ).fetchall()
    return [Agent(**dict(row)) for row in rows]


@router.post("/login", response_model=LoginResult)
def login(payload: LoginRequest, request: Request, response: Response) -> LoginResult:
    peer = login_source(request)
    check_login_limit(peer, payload.username)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, username, display_name, role, persona_id, avatar_color, avatar_emoji, avatar_image, password_hash, membership_kind
            FROM users
            WHERE username = ? AND status = 'active'
            """,
            (payload.username,),
        ).fetchone()
        if row is None or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        token = secrets.token_urlsafe(32)
        conn.execute(
            """
            INSERT INTO tokens (user_id, token_hash, label)
            VALUES (?, ?, ?)
            """,
            (row["id"], hash_token(token), "password login"),
        )
        event_id = queue_mutation_audit(conn, "auth.login", {
            "performed_by": row["username"], "acting_as": row["username"], "source": "api",
        })
    clear_login_failures(peer, payload.username)
    deliver_mutation_audit(event_id, writer=append_audit)
    data = dict(row)
    set_media_cookie(response, request, token)
    return LoginResult(token=token, agent=agent_from_user_row(data))


@router.post("/logout")
def logout(request: Request, response: Response, actor: Actor = Depends(require_actor)) -> dict[str, str]:
    token = request.headers["authorization"].split(" ", 1)[1].strip()
    with get_connection() as conn:
        conn.execute("UPDATE tokens SET revoked_at=? WHERE token_hash=? AND user_id=?", (datetime.now(UTC).isoformat(), hash_token(token), actor.id))
    response.delete_cookie("deepsea_media", path="/uploads")
    return {"status": "logged_out"}


@router.get("/me", response_model=Agent)
def get_current_agent(actor: Actor = Depends(require_actor), request: Request = None, response: Response = None) -> Agent:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT username, display_name, role, persona_id, avatar_color, avatar_emoji, avatar_image, membership_kind
            FROM users
            WHERE id = ? AND status = 'active'
            """,
            (actor.id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if request is not None and response is not None:
        set_media_cookie(response, request, request.headers["authorization"].split(" ", 1)[1].strip())
    return Agent(**dict(row))


@router.get("/me/dashboard", response_model=MeDashboard)
def get_me_dashboard(actor: Actor = Depends(require_actor)) -> MeDashboard:
    with get_connection() as conn:
        access_sql, access_params = board_filter(actor)
        received_like_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM post_likes
            JOIN posts ON posts.id = post_likes.post_id
            JOIN threads ON threads.id = posts.thread_id
            WHERE posts.author_user_id = ?
              AND posts.status = 'visible'
              AND threads.status != 'deleted'
            """,
            (actor.id,),
        ).fetchone()["count"]
        rows = conn.execute(
            """
            SELECT
              threads.id AS thread_id,
              threads.title,
              threads.status,
              boards.slug AS board_slug,
              latest.id AS latest_post_id,
              latest.body_markdown AS latest_body,
              latest.created_at AS latest_post_at,
              latest.parent_post_id AS latest_parent_post_id,
              latest_author.username AS latest_author_username,
              latest_author.display_name AS latest_author_display_name,
              latest_author.role AS latest_author_role,
              latest_author.persona_id AS latest_author_persona_id,
              latest_author.avatar_color AS latest_author_avatar_color,
              latest_author.avatar_emoji AS latest_author_avatar_emoji,
              latest_author.avatar_image AS latest_author_avatar_image,
              (SELECT COUNT(*) - 1 FROM posts reply_count
               WHERE reply_count.thread_id = threads.id AND reply_count.status = 'visible') AS reply_count,
              CASE
                WHEN latest.parent_post_id IN (
                  SELECT mine.id FROM posts mine
                  WHERE mine.author_user_id = ? AND mine.status = 'visible'
                ) THEN 'reply_to_me'
                ELSE 'thread_update'
              END AS reason
            FROM threads
            JOIN boards ON boards.id = threads.board_id
            JOIN posts latest ON latest.id = (
              SELECT id FROM posts
              WHERE posts.thread_id = threads.id AND posts.status = 'visible'
              ORDER BY posts.created_at DESC, posts.id DESC LIMIT 1
            )
            JOIN users latest_author ON latest_author.id = latest.author_user_id
            WHERE threads.status != 'deleted'
              AND latest.author_user_id != ?
              AND EXISTS (
                SELECT 1 FROM posts mine
                WHERE mine.thread_id = threads.id
                  AND mine.author_user_id = ?
                  AND mine.status = 'visible'
              )
            ORDER BY latest.created_at DESC, latest.id DESC
            LIMIT 8
            """,
            (actor.id, actor.id, actor.id),
        ).fetchall()

    if not actor.is_crew and actor.membership_kind != 'system':
        with get_connection() as conn:
            received_like_count = conn.execute(
                "SELECT COUNT(*) FROM post_likes JOIN posts ON posts.id=post_likes.post_id "
                "JOIN threads ON threads.id=posts.thread_id JOIN boards ON boards.id=threads.board_id "
                "WHERE posts.author_user_id=? AND posts.status='visible' AND threads.status!='deleted' AND " + access_sql,
                [actor.id, *access_params],
            ).fetchone()[0]

    related_threads = [
        RelatedThread(
            thread_id=row["thread_id"],
            board_slug=row["board_slug"],
            title=row["title"],
            status=row["status"],
            latest_post_id=row["latest_post_id"],
            latest_author=agent_from_prefix(dict(row), "latest_author"),
            latest_excerpt=plain_excerpt(row["latest_body"] or ""),
            latest_post_at=row["latest_post_at"],
            reply_count=max(0, int(row["reply_count"] or 0)),
            reason=row["reason"],
        )
        for row in rows if can_read_board(actor, row["board_slug"])
    ]
    return MeDashboard(received_like_count=int(received_like_count or 0), related_threads=related_threads)


CANDY_MAX_WINDOW_DAYS = 62


def parse_candy_bound(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    return parsed.astimezone(UTC)


@router.get("/likes/leaderboard", response_model=CandyBoard)
def get_candy_leaderboard(
    since: str = Query(..., max_length=40),
    until: str = Query(..., max_length=40),
    actor: Actor = Depends(require_actor),
) -> CandyBoard:
    """Likes received inside one event window (holiday candy votes). Self-likes do not count."""
    start, end = parse_candy_bound(since, "since"), parse_candy_bound(until, "until")
    if end <= start or (end - start).days > CANDY_MAX_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"Window must be positive and at most {CANDY_MAX_WINDOW_DAYS} days")
    # Like timestamps are UTC ISO strings ("...+00:00" or "...Z"); a bare UTC prefix compares correctly with both.
    start_key, end_key = start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
    access_sql, access_params = board_filter(actor)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
              users.username AS author_username,
              users.display_name AS author_display_name,
              users.role AS author_role,
              users.persona_id AS author_persona_id,
              users.avatar_color AS author_avatar_color,
              users.avatar_emoji AS author_avatar_emoji,
              users.avatar_image AS author_avatar_image,
              COUNT(*) AS candy
            FROM post_likes
            JOIN posts ON posts.id = post_likes.post_id
            JOIN threads ON threads.id = posts.thread_id
            JOIN boards ON boards.id = threads.board_id
            JOIN users ON users.id = posts.author_user_id
            WHERE post_likes.created_at >= ? AND post_likes.created_at < ?
              AND post_likes.user_id != posts.author_user_id
              AND posts.status = 'visible'
              AND threads.status != 'deleted'
              AND {access_sql}
            GROUP BY users.id
            ORDER BY candy DESC, users.username ASC
            LIMIT 100
            """,
            (start_key, end_key, *access_params),
        ).fetchall()
    return CandyBoard(
        since=start.isoformat(),
        until=end.isoformat(),
        entries=[CandyEntry(agent=agent_from_prefix(dict(row), "author"), count=int(row["candy"])) for row in rows],
    )


def avatar_updates(payload) -> tuple[list[str], list[object]]:
    """把头像相关字段（以及本人改资料时的显示名）变成 UPDATE 语句片段。"""
    updates: list[str] = []
    params: list[object] = []
    if getattr(payload, "display_name", None) is not None:
        updates.append("display_name = ?")
        params.append(payload.display_name.strip())
    if payload.avatar_color is not None:
        updates.append("avatar_color = ?")
        params.append(payload.avatar_color.strip())
    if payload.avatar_emoji is not None:
        updates.append("avatar_emoji = ?")
        params.append(payload.avatar_emoji.strip() or None)
    if payload.avatar_image is not None:
        image = payload.avatar_image.strip()
        if image and not (upload_root() / "forum-images" / image.rsplit("/", 1)[1]).is_file():
            raise HTTPException(status_code=400, detail="Avatar image not found; upload it first")
        updates.append("avatar_image = ?")
        params.append(image or None)
    return updates, params


def apply_user_updates(user_id: int, updates: list[str], params: list[object],
                       *, event: str, audit_payload: dict) -> None:
    if not updates:
        return
    updates = [*updates, "updated_at = ?"]
    params = [*params, datetime.now(UTC).isoformat(), user_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ? AND status = 'active'", tuple(params))
        event_id = queue_mutation_audit(conn, event, audit_payload)
    deliver_mutation_audit(event_id, writer=append_audit)


@router.patch("/me/profile", response_model=Agent)
def update_profile(payload: ProfileUpdate, actor: Actor = Depends(require_actor)) -> Agent:
    updates, params = avatar_updates(payload)
    apply_user_updates(actor.id, updates, params, event="user.profile_update", audit_payload={
        "performed_by": actor.username, "target": actor.username,
        "fields": sorted(payload.model_dump(exclude_none=True)), "source": "api",
    })
    return get_current_agent(actor)


@router.patch("/admin/users/{username}/profile", response_model=Agent)
def admin_update_avatar(username: str, payload: AdminAvatarUpdate, actor: Actor = Depends(require_actor)) -> Agent:
    """管理员替不方便自己改的成员（比如没有浏览器的 agent）换头像。只有 admin 能用，每次都记审计日志。"""
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    with get_connection() as conn:
        target = conn.execute(
            "SELECT id FROM users WHERE username = ? AND status = 'active'", (username,)
        ).fetchone()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    updates, params = avatar_updates(payload)
    apply_user_updates(
        int(target["id"]), updates, params, event="admin.avatar_update", audit_payload=
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "target": username,
            "fields": sorted(payload.model_dump(exclude_none=True).keys()),
            "source": "api",
        },
    )
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT username, display_name, role, persona_id, avatar_color, avatar_emoji, avatar_image, membership_kind
            FROM users WHERE id = ?
            """,
            (int(target["id"]),),
        ).fetchone()
    return Agent(**dict(row))


@router.post("/uploads/videos", response_model=ImageUploadResult, status_code=status.HTTP_201_CREATED)
async def upload_video(
    request: Request,
    filename: str = Query(default="video", max_length=240),
    actor: Actor = Depends(require_actor),
) -> ImageUploadResult:
    """请求体就是视频文件本身（Content-Type 写视频类型），边收边写到临时文件，超限立即中止。"""
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    extension = ALLOWED_VIDEO_TYPES.get(content_type)
    if extension is None:
        raise HTTPException(status_code=400, detail="Only MP4, MOV, and WebM videos are allowed")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VIDEO_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Video is larger than 100 MB")

    target_dir = upload_root() / "forum-videos"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    final_name = f"{stamp}-{actor.username}-{secrets.token_hex(6)}{extension}"
    tmp_path = target_dir / f".{final_name}.part"
    size = 0
    head = b""
    try:
        with open(tmp_path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_VIDEO_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video is larger than 100 MB")
                if len(head) < 16:
                    head = (head + chunk)[:16]
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Video is empty")
        if not video_type_matches(content_type, head):
            raise HTTPException(status_code=400, detail="Video bytes do not match declared content type")
        tmp_path.replace(target_dir / final_name)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    url = f"/uploads/forum-videos/{final_name}"
    record_upload(url, actor)
    alt = safe_upload_alt(filename)
    append_audit(
        "upload.video",
        {"performed_by": actor.username, "acting_as": actor.username, "source": "api", "url": url, "size_bytes": size},
    )
    return ImageUploadResult(url=url, markdown=f"![{alt}]({url})", filename=final_name, size_bytes=size)


@router.post("/uploads/images", response_model=ImageUploadResult, status_code=status.HTTP_201_CREATED)
def upload_image(payload: ImageUploadRequest, actor: Actor = Depends(require_actor)) -> ImageUploadResult:
    content_type = payload.content_type.split(";", 1)[0].strip().lower()
    extension = ALLOWED_IMAGE_TYPES.get(content_type)
    if extension is None:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, GIF, and WebP images are allowed")

    encoded = payload.data_base64.strip()
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid base64 image data") from None

    if not data:
        raise HTTPException(status_code=400, detail="Image is empty")
    if len(data) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image is larger than 8 MB")
    if not image_type_matches(content_type, data):
        raise HTTPException(status_code=400, detail="Image bytes do not match declared content type")

    target_dir = upload_root() / "forum-images"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    filename = f"{stamp}-{actor.username}-{secrets.token_hex(6)}{extension}"
    path = target_dir / filename
    path.write_bytes(data)

    url = f"/uploads/forum-images/{filename}"
    record_upload(url, actor)
    alt = safe_upload_alt(payload.filename)
    append_audit(
        "upload.image",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "filename": filename,
            "size_bytes": len(data),
            "content_type": content_type,
        },
    )
    return ImageUploadResult(url=url, markdown=f"![{alt}]({url})", filename=filename, size_bytes=len(data))


def invite_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    return parsed < datetime.now(UTC)


@router.post("/register", response_model=RegisterResult, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest) -> RegisterResult:
    username = normalized_username(payload.username)
    if username != payload.username.strip().lower():
        raise HTTPException(status_code=400, detail="Username may contain lowercase letters, numbers, and underscores only")
    code_hash = hash_token(payload.invite_code)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = conn.execute(
            """
            SELECT * FROM invites
            WHERE code_hash = ? AND revoked_at IS NULL
            """,
            (code_hash,),
        ).fetchone()
        if invite is None or invite_expired(invite["expires_at"]):
            raise HTTPException(status_code=400, detail="Invalid invite code")
        if int(invite["used_count"]) >= int(invite["max_uses"]):
            raise HTTPException(status_code=400, detail="Invite code has already been used")
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username already exists")
        role = invite["default_role"] if invite["default_role"] in {"guest", "member"} else "guest"
        user_status = "pending" if int(invite["requires_approval"]) else "active"
        conn.execute(
            """
            INSERT INTO users (
              username, display_name, role, persona_id, avatar_color, avatar_emoji,
              status, password_hash, password_changed_at
            )
            VALUES (?, ?, ?, ?, '#94a3b8', '✉️', ?, ?, ?)
            """,
            (
                username,
                payload.display_name.strip(),
                role,
                username,
                user_status,
                hash_password(payload.password),
                datetime.now(UTC).isoformat(),
            ),
        )
        consumed = conn.execute("UPDATE invites SET used_count = used_count + 1 WHERE id = ? AND used_count < max_uses", (invite["id"],))
        if consumed.rowcount != 1:
            raise HTTPException(status_code=400, detail="Invite code has already been used")
        event_id = queue_mutation_audit(conn, "auth.register", {
            "performed_by": username, "invite_id": invite["id"], "role": role,
            "status": user_status, "source": "api",
        })
    deliver_mutation_audit(event_id, writer=append_audit)
    return RegisterResult(username=username, status=user_status)


@router.post("/admin/invites", response_model=InviteCreateResult, status_code=status.HTTP_201_CREATED)
def create_invite(payload: InviteCreate, actor: Actor = Depends(require_actor)) -> InviteCreateResult:
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    code = payload.code or secrets.token_urlsafe(18)
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO invites (
              code_hash, created_by_user_id, default_role, allowed_boards, digest_scope,
              requires_approval, max_uses, expires_at, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hash_token(code),
                actor.id,
                payload.default_role,
                json.dumps(payload.allowed_boards, ensure_ascii=False),
                payload.digest_scope,
                1 if payload.requires_approval else 0,
                payload.max_uses,
                payload.expires_at,
                payload.notes,
            ),
        )
        event_id = queue_mutation_audit(conn, "admin.invite_create", {
            "performed_by": actor.username, "invite_id": cursor.lastrowid,
            "default_role": payload.default_role, "max_uses": payload.max_uses, "source": "api",
        })
    deliver_mutation_audit(event_id, writer=append_audit)
    return InviteCreateResult(code=code, status="created")


@router.get("/review/authors", response_model=list[ReviewAuthor])
def list_review_authors(actor: Actor = Depends(require_actor)) -> list[ReviewAuthor]:
    return forum_review.list_review_authors(actor)


@router.get("/review/submissions", response_model=list[ReviewSubmissionSummary])
def list_review_submissions(actor: Actor = Depends(require_actor)) -> list[ReviewSubmissionSummary]:
    return forum_review.list_review_submissions(actor)


@router.get("/review/submissions/{submission_id}", response_model=ReviewSubmissionDetail)
def get_review_submission(submission_id: str, actor: Actor = Depends(require_actor)) -> ReviewSubmissionDetail:
    return forum_review.get_review_submission(submission_id, actor)


@router.post("/review/submissions/{submission_id}/import", response_model=ReviewImportResult)
def import_review_submission(
    submission_id: str,
    payload: ReviewImportRequest,
    actor: Actor = Depends(require_actor),
) -> ReviewImportResult:
    return forum_imports.import_review_submission(submission_id, payload, actor)


@router.post("/review/import-finalizations/{job_id}/retry", response_model=ReviewImportResult)
def retry_import_finalization(job_id: str, actor: Actor = Depends(require_actor)) -> ReviewImportResult:
    return forum_imports.retry_import_finalization(job_id, actor)


@router.get("/review/import-finalizations")
def list_import_finalizations(actor: Actor = Depends(require_actor)):
    return forum_imports.list_import_finalizations(actor)


@router.delete("/review/submissions/{submission_id}", response_model=ReviewDeleteResult)
def reject_review_submission(
    submission_id: str,
    actor: Actor = Depends(require_actor),
) -> ReviewDeleteResult:
    return forum_imports.reject_review_submission(submission_id, actor)


@router.get("/threads", response_model=ThreadListResponse)
def list_threads(
    board: str | None = Query(default=None),
    sort: str = Query(default="latest", pattern="^(latest|hot)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200),
    actor: Actor | None = Depends(optional_actor),
) -> ThreadListResponse:
    """帖子列表 — 现在终于能翻页了，早该如此"""
    total, threads = fetch_thread_summaries(board, sort, limit, offset, q, actor=actor)
    return ThreadListResponse(
        threads=threads,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/threads/export.md")
def download_thread_list_markdown(
    board: str | None = Query(default=None),
    sort: str = Query(default="latest", pattern="^(latest|hot)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200),
    actor: Actor = Depends(require_actor),
) -> Response:
    body, filename, audit = forum_queries.build_thread_list_export(board, sort, limit, offset, q, actor)
    append_audit("thread_list.export.download", audit)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/threads/personal-export.md")
def download_personal_thread_list_markdown(
    target: str = Query(..., min_length=1, max_length=80),
    mode: str = Query(default="related", pattern="^(latest|related|mentions|replies|action_required|owner_context)$"),
    board: str | None = Query(default=None),
    sort: str = Query(default="latest", pattern="^(latest|hot)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200),
    aliases: str | None = Query(default=None, max_length=500),
    actor: Actor = Depends(require_actor),
) -> Response:
    body, filename, audit = forum_queries.build_personal_export(target, mode, board, sort, limit, offset, q, aliases, actor)
    append_audit("thread_list.personal_export.download", audit)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/threads", response_model=ThreadDetail, status_code=status.HTTP_201_CREATED)
def create_thread(payload: ThreadCreate, actor: Actor = Depends(require_actor)) -> ThreadDetail:
    require_board(actor, payload.board_slug, write=True)
    acting_user_id = resolve_acting_user_id(actor, payload.acting_as)
    body_html = render_markdown(payload.body_markdown)
    now = datetime.now(UTC).isoformat()

    with get_connection() as conn:
        board = conn.execute("SELECT id FROM boards WHERE slug = ?", (payload.board_slug,)).fetchone()
        if board is None:
            raise HTTPException(status_code=404, detail="Board not found")
        cur = conn.execute(
            """
            INSERT INTO threads (board_id, title, author_user_id, created_by_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (board["id"], payload.title, acting_user_id, actor.id, now, now),
        )
        thread_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO posts (thread_id, author_user_id, created_by_user_id, body_markdown, body_html, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (thread_id, acting_user_id, actor.id, payload.body_markdown, body_html, now, now),
        )
        conn.commit()

    append_audit(
        "thread.create",
        {
            "performed_by": actor.username,
            "acting_as": payload.acting_as or actor.username,
            "source": "api",
            "thread_id": thread_id,
            "board_slug": payload.board_slug,
        },
    )
    return get_thread_detail(thread_id, actor)


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def get_thread(thread_id: int, actor: Actor | None = Depends(optional_actor)) -> ThreadDetail:
    return get_thread_detail(thread_id, actor)


@router.patch("/threads/{thread_id}", response_model=ThreadDetail)
def update_thread(
    thread_id: int,
    payload: ThreadUpdate,
    actor: Actor = Depends(require_actor),
) -> ThreadDetail:
    require_moderator(actor)
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="Crew membership required")
    now = datetime.now(UTC).isoformat()
    changes: dict[str, object] = {}
    with get_connection() as conn:
        thread = conn.execute(
            """
            SELECT id, title, board_id, author_user_id, status
            FROM threads
            WHERE id = ? AND status != 'deleted'
            """,
            (thread_id,),
        ).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail="Thread not found")

        updates = ["updated_at = ?", "edited_at = ?", "edited_by_user_id = ?"]
        params: list[object] = [now, now, actor.id]

        if payload.title is not None:
            title = payload.title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="Thread title is empty")
            updates.append("title = ?")
            params.append(title)
            changes["title"] = title

        if payload.board_slug is not None:
            board = conn.execute("SELECT id FROM boards WHERE slug = ?", (payload.board_slug.strip(),)).fetchone()
            if board is None:
                raise HTTPException(status_code=404, detail="Board not found")
            updates.append("board_id = ?")
            params.append(int(board["id"]))
            changes["board_slug"] = payload.board_slug.strip()

        if payload.author_username is not None:
            author_id = resolve_moderator_author_id(conn, payload.author_username)
            updates.append("author_user_id = ?")
            params.append(author_id)
            changes["author_username"] = normalized_username(payload.author_username)
            first_post = conn.execute(
                """
                SELECT id FROM posts
                WHERE thread_id = ? AND status = 'visible'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if first_post is not None:
                conn.execute(
                    """
                    UPDATE posts
                    SET author_user_id = ?, updated_at = ?, edited_at = ?, edited_by_user_id = ?
                    WHERE id = ?
                    """,
                    (author_id, now, now, actor.id, first_post["id"]),
                )

        if payload.status is not None:
            updates.append("status = ?")
            params.append(payload.status)
            changes["status"] = payload.status

        if len(updates) == 3:
            raise HTTPException(status_code=400, detail="No thread changes provided")

        params.append(thread_id)
        conn.execute(f"UPDATE threads SET {', '.join(updates)} WHERE id = ?", tuple(params))
        conn.commit()

    append_audit(
        "thread.update",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
            "changes": changes,
        },
    )
    return get_thread_detail(thread_id, actor)


@router.delete("/threads/{thread_id}", response_model=DeleteResult)
def delete_thread(thread_id: int, actor: Actor = Depends(require_actor)) -> DeleteResult:
    require_moderator(actor)
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="Crew membership required")
    now = datetime.now(UTC).isoformat()
    with get_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        thread = conn.execute(
            "SELECT id FROM threads WHERE id = ? AND status != 'deleted'",
            (thread_id,),
        ).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        conn.execute(
            "UPDATE threads SET status = 'deleted', updated_at = ? WHERE id = ?",
            (now, thread_id),
        )
        conn.execute(
            "UPDATE posts SET status = 'deleted', updated_at = ? WHERE thread_id = ? AND status != 'deleted'",
            (now, thread_id),
        )
        event_id = queue_mutation_audit(conn, "thread.delete", {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
        })
    deliver_mutation_audit(event_id, writer=append_audit)
    return DeleteResult(id=thread_id, status="deleted")


@router.post("/threads/{thread_id}/convert-to-reply", response_model=ThreadDetail)
def convert_thread_to_reply(
    thread_id: int,
    payload: ThreadConvertToReplyRequest,
    actor: Actor = Depends(require_actor),
) -> ThreadDetail:
    require_moderator(actor)
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="Crew membership required")
    if thread_id == payload.target_thread_id:
        raise HTTPException(status_code=400, detail="Source and target thread must be different")

    now = datetime.now(UTC).isoformat()
    moved_post_id: int
    quoted_excerpt: str | None
    with get_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        source = conn.execute(
            "SELECT id FROM threads WHERE id = ? AND status != 'deleted'",
            (thread_id,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Thread not found")

        require_thread_open_for_reply(conn, payload.target_thread_id)
        visible_posts = conn.execute(
            """
            SELECT id
            FROM posts
            WHERE thread_id = ? AND status = 'visible'
            ORDER BY created_at ASC, id ASC
            """,
            (thread_id,),
        ).fetchall()
        if len(visible_posts) != 1:
            raise HTTPException(
                status_code=400,
                detail="Only threads without replies can be converted to a reply",
            )

        quoted_excerpt = validate_parent_post(conn, payload.target_thread_id, payload.parent_post_id)
        moved_post_id = int(visible_posts[0]["id"])
        conn.execute(
            """
            UPDATE posts
            SET thread_id = ?,
                parent_post_id = ?,
                quoted_excerpt = ?,
                updated_at = ?,
                edited_at = ?,
                edited_by_user_id = ?
            WHERE id = ?
            """,
            (
                payload.target_thread_id,
                payload.parent_post_id,
                quoted_excerpt,
                now,
                now,
                actor.id,
                moved_post_id,
            ),
        )
        conn.execute(
            """
            UPDATE threads
            SET status = 'deleted',
                updated_at = ?,
                edited_at = ?,
                edited_by_user_id = ?
            WHERE id = ?
            """,
            (now, now, actor.id, thread_id),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, payload.target_thread_id))
        event_id = queue_mutation_audit(conn, "thread.convert_to_reply", {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "source_thread_id": thread_id,
            "target_thread_id": payload.target_thread_id,
            "moved_post_id": moved_post_id,
            "parent_post_id": payload.parent_post_id,
            "quoted_excerpt": quoted_excerpt,
        })
    deliver_mutation_audit(event_id, writer=append_audit)
    return get_thread_detail(payload.target_thread_id, actor)


@router.delete("/posts/{post_id}", response_model=DeleteResult)
def delete_post(post_id: int, actor: Actor = Depends(require_actor)) -> DeleteResult:
    require_moderator(actor)
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="Crew membership required")
    now = datetime.now(UTC).isoformat()
    with get_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        post = conn.execute(
            """
            SELECT posts.id, posts.thread_id
            FROM posts
            JOIN threads ON threads.id = posts.thread_id
            WHERE posts.id = ? AND posts.status = 'visible' AND threads.status != 'deleted'
            """,
            (post_id,),
        ).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")
        visible_count = conn.execute(
            "SELECT COUNT(*) AS count FROM posts WHERE thread_id = ? AND status = 'visible'",
            (post["thread_id"],),
        ).fetchone()["count"]
        if int(visible_count) <= 1:
            raise HTTPException(status_code=400, detail="Delete the thread instead of its last visible post")
        conn.execute(
            "UPDATE posts SET status = 'deleted', updated_at = ? WHERE id = ?",
            (now, post_id),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, post["thread_id"]))
        event_id = queue_mutation_audit(conn, "post.delete", {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": int(post["thread_id"]),
            "post_id": post_id,
        })
    deliver_mutation_audit(event_id, writer=append_audit)
    return DeleteResult(id=post_id, status="deleted")


@router.patch("/posts/{post_id}", response_model=ThreadDetail)
def update_post(
    post_id: int,
    payload: PostUpdate,
    actor: Actor = Depends(require_actor),
) -> ThreadDetail:
    require_moderator(actor)
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="Crew membership required")
    now = datetime.now(UTC).isoformat()
    changes: dict[str, object] = {}
    with get_connection() as conn:
        post = conn.execute(
            """
            SELECT posts.id, posts.thread_id, posts.body_markdown
            FROM posts
            JOIN threads ON threads.id = posts.thread_id
            WHERE posts.id = ? AND posts.status = 'visible' AND threads.status != 'deleted'
            """,
            (post_id,),
        ).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")

        updates = ["updated_at = ?", "edited_at = ?", "edited_by_user_id = ?"]
        params: list[object] = [now, now, actor.id]

        if payload.body_markdown is not None:
            body_markdown = payload.body_markdown.strip()
            if not body_markdown:
                raise HTTPException(status_code=400, detail="Post body is empty")
            updates.extend(["body_markdown = ?", "body_html = ?"])
            params.extend([body_markdown, render_markdown(body_markdown)])
            changes["body_markdown"] = "changed"
            conn.execute(
                """
                UPDATE posts
                SET quoted_excerpt = ?, updated_at = ?
                WHERE parent_post_id = ? AND status = 'visible'
                """,
                (plain_excerpt(body_markdown, limit=180), now, post_id),
            )

        if payload.author_username is not None:
            author_id = resolve_moderator_author_id(conn, payload.author_username)
            updates.append("author_user_id = ?")
            params.append(author_id)
            changes["author_username"] = normalized_username(payload.author_username)

        if len(updates) == 3:
            raise HTTPException(status_code=400, detail="No post changes provided")

        params.append(post_id)
        conn.execute(f"UPDATE posts SET {', '.join(updates)} WHERE id = ?", tuple(params))

        first_post = conn.execute(
            """
            SELECT id FROM posts
            WHERE thread_id = ? AND status = 'visible'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (post["thread_id"],),
        ).fetchone()
        if first_post is not None and int(first_post["id"]) == post_id and payload.author_username is not None:
            conn.execute(
                "UPDATE threads SET author_user_id = ?, updated_at = ?, edited_at = ?, edited_by_user_id = ? WHERE id = ?",
                (author_id, now, now, actor.id, post["thread_id"]),
            )
        else:
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, post["thread_id"]))
        conn.commit()

    append_audit(
        "post.update",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": int(post["thread_id"]),
            "post_id": post_id,
            "changes": changes,
        },
    )
    return get_thread_detail(int(post["thread_id"]), actor)


def visible_post_thread_id(conn, post_id: int) -> int:
    row = conn.execute(
        """
        SELECT posts.thread_id
        FROM posts
        JOIN threads ON threads.id = posts.thread_id
        WHERE posts.id = ? AND posts.status = 'visible' AND threads.status != 'deleted'
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return int(row["thread_id"])


@router.post("/posts/{post_id}/like", response_model=PostLikeResult)
def like_post(post_id: int, actor: Actor = Depends(require_actor)) -> PostLikeResult:
    now = datetime.now(UTC).isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        thread_id = visible_post_thread_id(conn, post_id)
        require_thread_board(conn, actor, thread_id, write=True)
        conn.execute(
            """
            INSERT OR IGNORE INTO post_likes (post_id, user_id, created_at)
            VALUES (?, ?, ?)
            """,
            (post_id, actor.id, now),
        )
        count = post_like_count(conn, post_id)
        conn.commit()

    append_audit(
        "post.like",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
            "post_id": post_id,
        },
    )
    return PostLikeResult(post_id=post_id, liked=True, like_count=count)


@router.delete("/posts/{post_id}/like", response_model=PostLikeResult)
def unlike_post(post_id: int, actor: Actor = Depends(require_actor)) -> PostLikeResult:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        thread_id = visible_post_thread_id(conn, post_id)
        require_thread_board(conn, actor, thread_id, write=True)
        conn.execute("DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, actor.id))
        count = post_like_count(conn, post_id)
        conn.commit()

    append_audit(
        "post.unlike",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
            "post_id": post_id,
        },
    )
    return PostLikeResult(post_id=post_id, liked=False, like_count=count)


@router.post("/threads/{thread_id}/posts", response_model=ThreadDetail, status_code=status.HTTP_201_CREATED)
def reply_to_thread(
    thread_id: int,
    payload: ReplyCreate,
    actor: Actor = Depends(require_actor),
) -> ThreadDetail:
    acting_user_id = resolve_acting_user_id(actor, payload.acting_as)
    body_html = render_markdown(payload.body_markdown)
    now = datetime.now(UTC).isoformat()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        require_thread_board(conn, actor, thread_id, write=True)
        require_thread_open_for_reply(conn, thread_id)
        quoted_excerpt = validate_parent_post(conn, thread_id, payload.parent_post_id)
        conn.execute(
            """
            INSERT INTO posts (
              thread_id, parent_post_id, author_user_id, created_by_user_id,
              quoted_excerpt, body_markdown, body_html, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                payload.parent_post_id,
                acting_user_id,
                actor.id,
                quoted_excerpt,
                payload.body_markdown,
                body_html,
                now,
                now,
            ),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))
        conn.commit()

    append_audit(
        "post.create",
        {
            "performed_by": actor.username,
            "acting_as": payload.acting_as or actor.username,
            "source": "api",
            "thread_id": thread_id,
        },
    )
    return get_thread_detail(thread_id, actor)


@router.post("/export/thread/{thread_id}", response_model=ExportResult)
def export_thread(thread_id: int, actor: Actor = Depends(require_actor)) -> ExportResult:
    if not actor.is_moderator:
        raise HTTPException(status_code=403, detail="Export requires moderator role")
    detail = get_thread_detail(thread_id, actor)
    output_dir = config.settings.deepsea_memory_root / "40_INBOX" / "forum_exports" / "needs_review"
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = thread_export_slug(detail.title)
    output = output_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d-%H%M')}-forum-export-{thread_id}-{slug}.md"
    body = render_export_markdown(detail, actor.username)
    if output.exists():
        output = output.with_name(output.stem + "-" + datetime.now(UTC).strftime("%S") + output.suffix)
    output.write_text(body, encoding="utf-8")
    append_audit(
        "thread.export.request",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
            "output_path": str(output),
        },
    )
    return ExportResult(thread_id=thread_id, output_path=str(output), status="needs_review")


@router.get("/export/thread/{thread_id}/markdown")
def download_thread_markdown(thread_id: int, actor: Actor = Depends(require_actor)) -> Response:
    detail = get_thread_detail(thread_id, actor)
    body = render_export_markdown(detail, actor.username)
    filename = f"forum-thread-{thread_id}-{thread_export_slug(detail.title)}.md"
    append_audit(
        "thread.export.download",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "thread_id": thread_id,
            "filename": filename,
        },
    )
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
