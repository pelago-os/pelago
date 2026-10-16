"""Transport and receipt helpers for moderator-reviewed manual imports."""
from __future__ import annotations

import json
import re

from fastapi import HTTPException

TRANSPORT = re.compile(r"^```deepsea-submit\s*\n(.*?)\n```[ \t]*$", re.M | re.S)
FIELDS = {"submission_id", "forum_import_mode", "forum_reply_thread_id",
          "forum_reply_parent_post_id", "forum_board", "story_kind",
          "base_revision", "base_post_id", "ready"}


def unpack_transport(frontmatter, body):
    matches = list(TRANSPORT.finditer(body))
    if not matches:
        return dict(frontmatter), body
    if len(matches) != 1:
        raise HTTPException(400, "Exactly one deepsea-submit block is allowed")
    try:
        data = json.loads(matches[0].group(1))
    except ValueError as exc:
        raise HTTPException(400, "Invalid deepsea-submit JSON") from exc
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise HTTPException(400, "Unsupported deepsea-submit fields")
    for key in ("forum_reply_thread_id", "forum_reply_parent_post_id", "base_post_id"):
        if key in data and (type(data[key]) is not int or data[key] <= 0):
            raise HTTPException(400, f"{key} must be a positive integer")
    for key in FIELDS - {"ready", "forum_reply_thread_id", "forum_reply_parent_post_id", "base_post_id"}:
        if key in data and not isinstance(data[key], str):
            raise HTTPException(400, f"{key} must be text")
    if "forum_import_mode" in data and data["forum_import_mode"] not in {"thread", "reply"}:
        raise HTTPException(400, "forum_import_mode must be thread or reply")
    if data.get("submission_id") == "REPLACE-WITH-UNIQUE-ID":
        raise HTTPException(400, "Replace the template submission_id")
    if data.get("ready") is not True:
        raise HTTPException(409, "Submission is not ready")
    result = dict(frontmatter)
    result.update(data)
    if data.get("submission_id"):
        result["id"] = str(data["submission_id"])
    return result, (body[:matches[0].start()] + body[matches[0].end():]).strip()


def ensure_import_ledger(conn):
    from app.migrations import apply_migrations
    apply_migrations(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS forum_import_receipts (
        identity_key TEXT PRIMARY KEY, thread_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL, imported_at TEXT NOT NULL)""")
