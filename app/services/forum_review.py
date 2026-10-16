"""Review inbox paths, parsing and attribution rules shared by API and automation."""
from __future__ import annotations
import base64
import os
from email.utils import parseaddr
import re
from datetime import UTC, datetime
from pathlib import Path
from fastapi import HTTPException
from app import config
from app.auth import Actor
from app.database import get_connection
from app.schemas import ReviewAuthor, ReviewSubmissionSummary, ReviewSubmissionDetail
from app.services.frontmatter import split_markdown
from app.services.markdown import plain_excerpt

REVIEW_LANES = {
    "gmail": Path("40_INBOX/forum_drafts_mail/needs_review"),
    "drive": Path("40_INBOX/forum_drafts_drive/needs_review"),
}


def require_moderator(actor: Actor) -> None:
    if not actor.is_moderator:
        raise HTTPException(status_code=403, detail="Moderator role required")


def review_lane_roots() -> list[tuple[int, Path, Path]]:
    roots = []
    for index, memory_root in enumerate(config.settings.review_memory_roots):
        resolved_memory_root = memory_root.resolve()
        for relative in REVIEW_LANES.values():
            roots.append((index, resolved_memory_root, (resolved_memory_root / relative).resolve()))
    return roots


def review_roots() -> list[Path]:
    return [lane_root for _, _, lane_root in review_lane_roots()]


def encode_submission_id(path: Path) -> str:
    resolved = path.resolve()
    for index, memory_root in enumerate(config.settings.review_memory_roots):
        root = memory_root.resolve()
        if resolved.is_relative_to(root):
            relative = resolved.relative_to(root).as_posix()
            payload = f"{index}:{relative}"
            return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    raise HTTPException(status_code=403, detail="Review path not allowed")


def decode_submission_path(submission_id: str) -> Path:
    try:
        padding = "=" * (-len(submission_id) % 4)
        relative = base64.urlsafe_b64decode((submission_id + padding).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc

    root = config.settings.deepsea_memory_root.resolve()
    if re.match(r"^\d+:", relative):
        index_text, relative = relative.split(":", 1)
        try:
            root = config.settings.review_memory_roots[int(index_text)].resolve()
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Submission not found") from exc

    path = (root / relative).resolve()
    if path.suffix.lower() != ".md" or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Submission not found")
    if not any(path.is_relative_to(root) for root in review_roots()):
        raise HTTPException(status_code=403, detail="Review path not allowed")
    return path


def parse_review_markdown(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    parsed = split_markdown(text)
    if parsed is None:
        return {}, text.strip()
    metadata, body = parsed
    return metadata, body.strip()


def first_markdown_heading(body: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def strip_forum_marker(title: str) -> str:
    clean = re.sub(r"^\[forum[:\s]+[a-z_]+\]\s*", "", title.strip(), flags=re.IGNORECASE)
    clean = re.sub(
        r"^\[(?:forum-)?reply(?::#?\d+)?(?:\s+post:#?\d+)?\]\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"^forum:?[a-z_]+\s*[-:：]?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(论坛投稿|投稿到)\s+[a-z_]+\s*[-:：]?\s*", "", clean, flags=re.IGNORECASE)
    return clean.strip() or title.strip()


def clean_review_body(frontmatter: dict[str, object], body: str) -> str:
    """Remove only a recognizable undelimited Doc envelope before its H1."""
    if not frontmatter.get("source_doc_id"):
        return body
    lines = body.splitlines(keepends=True)
    fields: dict[str, str] = {}
    for index, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        if text.startswith("# "):
            title = fields.get("title", "").strip()
            if len(title) > 1 and title[0] == title[-1] and title[0] in "\"'":
                title = title[1:-1].strip()
            if {"id", "agent", "type", "title"} <= fields.keys() and title == text[2:].strip():
                return "".join(lines[index:])
            return body
        field = re.fullmatch(r"([a-z_]+)\s*:\s*(.*)", text, re.IGNORECASE)
        if field:
            if not fields and field[1].lower() != "id":
                return body
            fields[field[1].lower()] = field[2]
        elif not (fields and re.match(r"^[-*+]\s+", text)):
            return body
    return body


def review_title(frontmatter: dict[str, object], body: str, path: Path) -> str:
    # Older Google Doc harvests mistook undelimited metadata for the title.
    # Recover the display title only; never promote body metadata to routing fields.
    if frontmatter.get("source_doc_id"):
        title = str(frontmatter.get("title") or "").strip()
        if not title or re.match(
            r"^(?:[a-z_]+\s*:|[-*+]\s+|(?:[a-z0-9]+[\s_-]+){1,2}(?:forum[\s_-]+)?drop\b)",
            title, re.IGNORECASE,
        ):
            for line in body.splitlines():
                line = line.strip()
                if not line or line == "---" or re.match(r"^[-*+]\s+", line):
                    continue
                field = re.match(r"^([a-z_]+)\s*:\s*(.*)$", line, re.IGNORECASE)
                if not field:
                    break
                if field[1].lower() == "title" and field[2].strip():
                    value = field[2].strip()
                    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1].strip()
                    return strip_forum_marker(value)[:160]
            heading = first_markdown_heading(body)
            if heading:
                return strip_forum_marker(heading)[:160]
            source_title = str(frontmatter.get("source_doc_title") or "").strip()
            if source_title:
                return re.sub(
                    r"^(?:[a-z0-9]+[\s_-]+){1,2}(?:forum[\s_-]+)?drop\s*[-\u2013\u2014:\uff1a]\s*"
                    r"20\d{2}-\d{2}-\d{2}\s*[-\u2013\u2014:\uff1a]\s*[a-z_]+\s*[-\u2013\u2014:\uff1a]\s*",
                    "", source_title, flags=re.IGNORECASE,
                )[:160]
    for key in ("gmail_subject", "title"):
        value = str(frontmatter.get(key) or "").strip()
        if value:
            return strip_forum_marker(value)[:160]
    heading = first_markdown_heading(body)
    if heading:
        return strip_forum_marker(heading)[:160]
    value = str(frontmatter.get("source_doc_title") or "").strip()
    if value:
        return strip_forum_marker(value)[:160]
    return path.stem[:160]


def review_board(frontmatter: dict[str, object]) -> str:
    board = str(frontmatter.get("forum_board") or "").strip()
    if board.casefold() in {"brainstorm", "脑洞", "脑洞版", "脑洞与提案"}:
        return "proposals"
    return board or "guest_questions"


def review_int(frontmatter: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        value = str(frontmatter.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"#?(\d+)", value)
        if match:
            return int(match.group(1))
    return None


def review_import_mode(frontmatter: dict[str, object]) -> str:
    mode = str(frontmatter.get("forum_import_mode") or "").strip().lower()
    if mode == "reply":
        return "reply"
    if review_int(frontmatter, "forum_reply_thread_id", "reply_thread_id", "thread_id") is not None:
        return "reply"
    return "thread"


def review_reply_thread_id(frontmatter: dict[str, object]) -> int | None:
    return review_int(frontmatter, "forum_reply_thread_id", "reply_thread_id")


def review_reply_parent_post_id(frontmatter: dict[str, object]) -> int | None:
    return review_int(frontmatter, "forum_reply_parent_post_id", "reply_parent_post_id", "parent_post_id")


def review_excerpt(body: str) -> str:
    return plain_excerpt(body.replace("#", " ").strip())


def review_source(path: Path, frontmatter: dict[str, object]) -> str:
    source = str(frontmatter.get("gmail_source") or frontmatter.get("source") or "").strip()
    if source:
        return source
    relative = path.as_posix()
    if "forum_drafts_mail" in relative:
        return "gmail"
    if "forum_drafts_drive" in relative:
        return "drive"
    return "unknown"


def normalized_username(value: str, fallback: str = "guest") -> str:
    clean = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = fallback
    if not clean[0].isalpha():
        clean = f"guest_{clean}"
    return clean[:80]


def body_author_hint(body: str) -> str | None:
    for line in body.splitlines()[:40]:
        match = re.match(r"^\s*(Author\s*/\s*display name|Author|From-Agent)\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if not match:
            continue
        value = match.group(2).strip()
        if value:
            return value.split("/", 1)[0].strip()
    return None


def display_name_from_identity(username: str, frontmatter: dict[str, object] | None = None) -> str:
    frontmatter = frontmatter or {}
    for key in ("display_name", "from_display_name", "agent_display_name"):
        value = str(frontmatter.get(key) or "").strip()
        if value:
            return value[:120]
    return username.replace("_", " ").title()[:120]


def user_by_username(conn, username: str, statuses: tuple[str, ...] = ("active", "attribution_only")):
    placeholders = ",".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM users WHERE username = ? AND status IN ({placeholders})",
        (username, *statuses),
    ).fetchone()


def suggested_author_username(frontmatter: dict[str, object], body: str = "") -> str | None:
    agent = normalized_username(str(frontmatter.get("agent") or "").strip())
    if agent and agent not in {"guest", "unknown", "unknown_guest"}:
        return agent
    hinted_author = body_author_hint(body)
    if hinted_author:
        return normalized_username(hinted_author, "guest")
    if agent == "guest":
        return "guest"
    sender = parseaddr(str(frontmatter.get("gmail_from") or frontmatter.get("from") or ""))[1].strip().lower()
    # The base's own intake mailbox is not an author; set it so it is never mistaken for one.
    own_mailbox = parseaddr(os.environ.get("PELAGO_INTAKE_MAILBOX", ""))[1].strip().lower()
    if sender and "@" in sender and sender != own_mailbox:
        return normalized_username(sender.split("@", 1)[0], "guest")
    return None


def ensure_attribution_user(conn, username: str, frontmatter: dict[str, object]) -> int:
    username = normalized_username(username)
    existing = user_by_username(conn, username)
    if existing is not None:
        return int(existing["id"])

    display_name = display_name_from_identity(username, frontmatter)
    conn.execute(
        """
        INSERT INTO users (username, display_name, role, persona_id, avatar_color, avatar_emoji, status)
        VALUES (?, ?, 'guest', ?, '#94a3b8', '✉️', 'attribution_only')
        """,
        (username, display_name, username),
    )
    return int(conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"])


def resolve_review_author_id(conn, actor: Actor, frontmatter: dict[str, object], requested_username: str | None) -> int:
    raw_username = (requested_username or suggested_author_username(frontmatter) or "").strip()
    if not raw_username:
        return actor.id
    username = normalized_username(raw_username)

    existing = user_by_username(conn, username)
    if existing is not None:
        if existing["status"] == "active":
            if not actor.is_admin:
                return actor.id
            return int(existing["id"])
        return int(existing["id"])

    return ensure_attribution_user(conn, username, frontmatter)


def resolve_moderator_author_id(conn, username: str) -> int:
    normalized = normalized_username(username)
    row = user_by_username(conn, normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="Author user not found")
    return int(row["id"])


def validate_parent_post(conn, thread_id: int, parent_post_id: int | None) -> str | None:
    if parent_post_id is None:
        return None
    parent = conn.execute(
        """
        SELECT id, body_markdown
        FROM posts
        WHERE id = ? AND thread_id = ? AND status = 'visible'
        """,
        (parent_post_id, thread_id),
    ).fetchone()
    if parent is None:
        raise HTTPException(status_code=400, detail="Parent post is not visible in this thread")
    return plain_excerpt(parent["body_markdown"], limit=180)


def require_thread_open_for_reply(conn, thread_id: int) -> None:
    thread = conn.execute(
        "SELECT id, status FROM threads WHERE id = ? AND status != 'deleted'",
        (thread_id,),
    ).fetchone()
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["status"] == "locked":
        raise HTTPException(status_code=423, detail="Thread is locked")


def review_source_message_id(frontmatter: dict[str, object]) -> str | None:
    for key in ("gmail_message_id", "gmail_thread_id", "source_doc_id", "id"):
        value = str(frontmatter.get(key) or "").strip()
        if value:
            return value[:200]
    return None


def review_relative_path(path: Path) -> str:
    resolved = path.resolve()
    for memory_root in config.settings.review_memory_roots:
        root = memory_root.resolve()
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    return resolved.as_posix()


def review_identity_keys(frontmatter: dict[str, object]) -> set[str]:
    # Harvester supplies the actual Drive file ID, shared across accounts/mirrors.
    # Agent-written submission IDs are labels, not globally unique document identities.
    source_doc_id = str(frontmatter.get("source_doc_id") or "").strip()
    if source_doc_id:
        return {f"source_doc_id:{source_doc_id}"}
    keys = set()
    for key in ("id", "gmail_message_id"):
        value = str(frontmatter.get(key) or "").strip()
        if value:
            keys.add(f"{key}:{value}")
    return keys


def review_import_receipt(conn, frontmatter):
    for key in sorted(review_identity_keys(frontmatter)):
        row = conn.execute("SELECT * FROM forum_import_receipts WHERE identity_key=?", (key,)).fetchone()
        if row is not None:
            return row
    if frontmatter.get("source_doc_id") and frontmatter.get("id"):
        legacy = conn.execute("SELECT * FROM forum_import_receipts WHERE identity_key=?",
                              ("id:" + str(frontmatter["id"]).strip(),)).fetchone()
        if legacy is not None:
            sources = conn.execute("""SELECT identity_key FROM forum_import_receipts
                WHERE post_id=? AND identity_key LIKE 'source_doc_id:%'""", (legacy["post_id"],)).fetchall()
            if not sources:
                # No provenance to establish whether this is the old deleted/imported post.
                raise HTTPException(409, "Legacy submission ID has no source document provenance; review required")
            # A legacy ID associated with another document is a collision, not a duplicate.
    return None


def imported_review_identity_keys() -> set[str]:
    keys = set()
    for _, memory_root, lane_root in review_lane_roots():
        imported_dir = memory_root / lane_root.relative_to(memory_root).parent / "imported"
        if not imported_dir.exists():
            continue
        for path in imported_dir.glob("*.md"):
            frontmatter, _ = parse_review_markdown(path)
            keys.update(review_identity_keys(frontmatter))
    return keys


def review_summary(
    path: Path,
    frontmatter: dict[str, object] | None = None,
    body: str | None = None,
) -> ReviewSubmissionSummary:
    if frontmatter is None or body is None:
        frontmatter, body = parse_review_markdown(path)
    suggested_username = suggested_author_username(frontmatter, body)
    return ReviewSubmissionSummary(
        id=encode_submission_id(path),
        source=review_source(path, frontmatter),
        relative_path=review_relative_path(path),
        title=review_title(frontmatter, body, path),
        board_slug=review_board(frontmatter),
        import_mode=review_import_mode(frontmatter),
        reply_thread_id=review_reply_thread_id(frontmatter),
        reply_parent_post_id=review_reply_parent_post_id(frontmatter),
        agent=str(frontmatter.get("agent") or "unknown"),
        suggested_author_username=suggested_username,
        suggested_author_display_name=display_name_from_identity(suggested_username, frontmatter)
        if suggested_username
        else None,
        created_at=str(frontmatter.get("created_at") or "") or None,
        excerpt=review_excerpt(clean_review_body(frontmatter, body)),
    )


def unique_import_path(source_path: Path) -> Path:
    imported_dir = source_path.parent.parent / "imported"
    imported_dir.mkdir(parents=True, exist_ok=True)
    desired = imported_dir / source_path.name
    if not desired.exists():
        return desired
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return desired.with_name(f"{desired.stem}-{stamp}{desired.suffix}")


def unique_rejected_path(source_path: Path) -> Path:
    rejected_dir = source_path.parent.parent / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    desired = rejected_dir / source_path.name
    if not desired.exists():
        return desired
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return desired.with_name(f"{desired.stem}-{stamp}{desired.suffix}")


def import_body(body: str, source_path: Path) -> str:
    relative = review_relative_path(source_path)
    return body.rstrip() + "\n\n---\n\n_imported from review inbox: `" + relative + "`_\n"


def active_username_exists(username: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? AND status = 'active'",
            (username,),
        ).fetchone()
    return row is not None


def review_acting_as(actor: Actor, requested_author: str | None) -> str | None:
    if not actor.is_admin or not requested_author:
        return None
    return requested_author if active_username_exists(requested_author) else None


def list_review_authors(actor: Actor) -> list[ReviewAuthor]:
    require_moderator(actor)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT username, display_name, role, status
            FROM users
            WHERE status IN ('active', 'attribution_only')
            ORDER BY status ASC, display_name ASC
            """
        ).fetchall()
    authors = []
    for row in rows:
        status_value = row["status"]
        authors.append(
            ReviewAuthor(
                username=row["username"],
                display_name=row["display_name"],
                role=row["role"],
                status=status_value,
                source="attribution" if status_value == "attribution_only" else "active",
            )
        )
    return authors


def list_review_submissions(actor: Actor) -> list[ReviewSubmissionSummary]:
    require_moderator(actor)
    submissions: list[ReviewSubmissionSummary] = []
    seen_keys = imported_review_identity_keys()
    candidates: list[Path] = []
    for _, _, lane_root in review_lane_roots():
        if not lane_root.exists():
            continue
        candidates.extend(lane_root.glob("*.md"))

    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        frontmatter, body = parse_review_markdown(path)
        keys = review_identity_keys(frontmatter)
        if keys and keys & seen_keys:
            continue
        seen_keys.update(keys)
        submissions.append(review_summary(path, frontmatter, body))
    return submissions


def get_review_submission(submission_id: str, actor: Actor) -> ReviewSubmissionDetail:
    require_moderator(actor)
    path = decode_submission_path(submission_id)
    frontmatter, body = parse_review_markdown(path)
    return ReviewSubmissionDetail(**review_summary(path).model_dump(), body_markdown=clean_review_body(frontmatter, body), frontmatter=frontmatter)
