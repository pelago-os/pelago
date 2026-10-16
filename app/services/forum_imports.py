"""Transactional submission commands shared by HTTP and scheduled callers."""
from __future__ import annotations
import shutil
from datetime import UTC, datetime
from fastapi import HTTPException
from app.auth import Actor, resolve_acting_user_id
from app.database import get_connection
from app.schemas import ReviewImportRequest, ReviewImportResult, ReviewDeleteResult
from app.services.forum_review import (
    decode_submission_path,
    clean_review_body,
    import_body,
    parse_review_markdown,
    require_moderator,
    require_thread_open_for_reply,
    resolve_review_author_id,
    review_acting_as,
    review_board,
    review_identity_keys,
    review_import_receipt,
    review_import_mode,
    review_relative_path,
    review_reply_parent_post_id,
    review_reply_thread_id,
    review_source,
    review_source_message_id,
    review_title,
    unique_rejected_path,
    validate_parent_post
)
from app.services.audit import append_audit
from app.services.import_finalization import enqueue_import, finalize_import
from app.services.markdown import render_markdown
from app.services.forum_automation import ensure_import_ledger, unpack_transport


def import_review_submission(
    submission_id: str,
    payload: ReviewImportRequest,
    actor: Actor,
) -> ReviewImportResult:
    require_moderator(actor)
    path = decode_submission_path(submission_id)
    source_content = path.read_bytes()
    frontmatter, body = parse_review_markdown(path)
    if path.read_bytes() != source_content:
        raise HTTPException(status_code=409, detail="Submission changed during import")
    frontmatter, body = unpack_transport(frontmatter, body)
    title = (payload.title or review_title(frontmatter, body, path)).strip()
    board_slug = (payload.board_slug or review_board(frontmatter)).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Submission title is empty")
    if not body:
        raise HTTPException(status_code=400, detail="Submission body is empty")

    import_mode = payload.import_mode or review_import_mode(frontmatter)
    reply_thread_id = payload.reply_thread_id or review_reply_thread_id(frontmatter)
    reply_parent_post_id = payload.reply_parent_post_id
    if reply_parent_post_id is None:
        reply_parent_post_id = review_reply_parent_post_id(frontmatter)
    imported_body = import_body(clean_review_body(frontmatter, body), path)
    body_html = render_markdown(imported_body)
    now = datetime.now(UTC).isoformat()
    source = review_source(path, frontmatter)
    source_message_id = review_source_message_id(frontmatter)
    source_submission_id = str(frontmatter.get("id") or review_relative_path(path))[:300]

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_import_ledger(conn)
        identity_keys = review_identity_keys(frontmatter)
        if not identity_keys:
            identity_keys = {"path:" + review_relative_path(path)}
        if review_import_receipt(conn, frontmatter) is not None:
            raise HTTPException(status_code=409, detail="Submission already imported")
        board = None
        if import_mode == "thread":
            board = conn.execute("SELECT id FROM boards WHERE slug = ?", (board_slug,)).fetchone()
            if board is None:
                raise HTTPException(status_code=404, detail="Board not found")
        requested_author = payload.attribution_username or payload.acting_as
        acting_as = review_acting_as(actor, payload.acting_as)
        if payload.attribution_username:
            acting_user_id = resolve_review_author_id(conn, actor, frontmatter, payload.attribution_username)
        elif acting_as:
            acting_user_id = resolve_acting_user_id(actor, acting_as)
        else:
            acting_user_id = resolve_review_author_id(conn, actor, frontmatter, None)

        post_id: int | None = None
        if import_mode == "reply":
            if reply_thread_id is None:
                raise HTTPException(status_code=400, detail="Reply import requires reply_thread_id")
            require_thread_open_for_reply(conn, reply_thread_id)
            quoted_excerpt = validate_parent_post(conn, reply_thread_id, reply_parent_post_id)
            cur = conn.execute(
                """
                INSERT INTO posts (
                  thread_id, parent_post_id, author_user_id, created_by_user_id,
                  quoted_excerpt, body_markdown, body_html, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reply_thread_id,
                    reply_parent_post_id,
                    acting_user_id,
                    actor.id,
                    quoted_excerpt,
                    imported_body,
                    body_html,
                    now,
                    now,
                ),
            )
            post_id = int(cur.lastrowid)
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, reply_thread_id))
            thread_id = reply_thread_id
        else:
            cur = conn.execute(
                """
                INSERT INTO threads (
                  board_id, title, author_user_id, created_by_user_id, submitter_identity_id,
                  source_channel, source_message_id, source_submission_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    board["id"],  # type: ignore[index]
                    title[:160],
                    acting_user_id,
                    actor.id,
                    acting_user_id,
                    source,
                    source_message_id,
                    source_submission_id,
                    now,
                    now,
                ),
            )
            thread_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO posts (thread_id, author_user_id, created_by_user_id, body_markdown, body_html, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (thread_id, acting_user_id, actor.id, imported_body, body_html, now, now),
            )
            post_id = int(cur.lastrowid)
        for identity_key in identity_keys:
            conn.execute("INSERT INTO forum_import_receipts VALUES (?,?,?,?)",
                         (identity_key, thread_id, post_id, now))
        finalization_id = enqueue_import(conn, path, source_content, thread_id, post_id, {
            "performed_by": actor.username,
            "acting_as": requested_author or acting_as or actor.username,
            "source": "api",
            "import_mode": import_mode,
            "thread_id": thread_id,
            "post_id": post_id,
            "reply_parent_post_id": reply_parent_post_id,
            "board_slug": board_slug,
            "submission_path": str(path),
        }, now)
        conn.commit()

    result = finalize_import(finalization_id, audit_writer=append_audit)
    return ReviewImportResult(thread_id=thread_id, post_id=post_id, status="imported",
        imported_path=result['imported_path'], finalization_id=finalization_id,
        finalization_status=result['status'])


def retry_import_finalization(job_id: str, actor: Actor) -> ReviewImportResult:
    require_moderator(actor)
    result = finalize_import(job_id, audit_writer=append_audit)
    if result is None:
        raise HTTPException(status_code=404, detail="Import finalization not found")
    return ReviewImportResult(thread_id=result['thread_id'], post_id=result['post_id'],
        status='imported', imported_path=result['imported_path'], finalization_id=result['id'],
        finalization_status=result['status'])


def list_import_finalizations(actor: Actor):
    require_moderator(actor)
    with get_connection() as conn:
        return [dict(row) for row in conn.execute('''SELECT id,thread_id,post_id,status,
            attempts,last_error,created_at FROM forum_import_finalizations
            WHERE status='pending' ORDER BY created_at,id LIMIT 100''')]


def reject_review_submission(
    submission_id: str,
    actor: Actor,
) -> ReviewDeleteResult:
    require_moderator(actor)
    path = decode_submission_path(submission_id)
    rejected_path = unique_rejected_path(path)
    shutil.move(str(path), rejected_path)
    append_audit(
        "review.reject",
        {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "submission_path": str(path),
            "rejected_path": str(rejected_path),
        },
    )
    return ReviewDeleteResult(submission_id=submission_id, status="rejected", rejected_path=str(rejected_path))
