"""Pure Markdown export formatting; no database or HTTP dependencies."""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from app.schemas import ThreadDetail, ThreadSummary
from app.services.frontmatter import yaml_scalar


def thread_export_slug(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")[:60]
    return slug or "thread"


def thread_list_export_filename(board: str | None, sort: str, q: str | None) -> str:
    parts = ["forum-list", board or "all", sort]
    if q and q.strip():
        parts.append(thread_export_slug(q.strip())[:32] or "search")
    parts.append(datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    slug = "-".join(thread_export_slug(part) for part in parts if part)
    return f"{slug}.md"


def personal_thread_list_export_filename(
    target_user: dict,
    mode: str,
    board: str | None,
    sort: str,
    q: str | None,
) -> str:
    parts = ["forum-list", str(target_user["username"]), mode, board or "all", sort]
    if q and q.strip():
        parts.append(thread_export_slug(q.strip())[:32] or "search")
    parts.append(datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    slug = "-".join(thread_export_slug(part) for part in parts if part)
    return f"{slug}.md"


def action_reason(match_reason: str) -> str:
    if "direct_reply" in match_reason:
        return "direct_reply"
    if "thread_update_after_target" in match_reason:
        return "updated_after_target_reply"
    if "owner_participated" in match_reason:
        return "owner_participated"
    if "alias_mention" in match_reason:
        return "alias_mention"
    return "related_activity"


def render_thread_list_markdown(
    threads: list[ThreadSummary],
    total: int,
    board: str | None,
    sort: str,
    limit: int,
    offset: int,
    q: str | None,
    requested_by: str,
    target_user: dict | None = None,
    personal_mode: str | None = None,
    aliases: list[str] | None = None,
    reasons: dict[int, str] | None = None,
    match_snippets: dict[int, str] | None = None,
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    title = thread_list_title(board=board, sort=sort, q=q, target_user=target_user, personal_mode=personal_mode)
    lines = [
        "---",
        f"id: forum-list-export-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        f"created_at: {generated_at}",
        "source: pelago-forum",
        "agent: forum",
        "track: project",
        "memory_scope: project",
        "project: forum",
        "type: forum-list-export",
        "status: inbox",
        "priority: 2",
        "sensitivity: private",
        "canonize: false",
        "visible_to:",
        f"  - {yaml_scalar(requested_by)}",
        "allowed_ops:",
        "  - read",
        "tags:",
        "  - forum-list-export",
        f"requested_by: {yaml_scalar(requested_by)}",
        f"forum_board: {yaml_scalar(board or 'all')}",
        f"forum_sort: {yaml_scalar(sort)}",
        f"forum_query: {yaml_scalar(q.strip() if q and q.strip() else '')}",
        f"thread_total: {total}",
        f"thread_offset: {offset}",
        f"thread_limit: {limit}",
    ]
    if target_user:
        lines.extend(
            [
                f"target_username: {yaml_scalar(target_user['username'])}",
                f"target_display_name: {yaml_scalar(target_user['display_name'])}",
                f"target_persona_id: {yaml_scalar(target_user.get('persona_id') or '')}",
                f"personal_mode: {yaml_scalar(personal_mode or 'latest')}",
                "target_aliases:",
            ]
        )
        for alias in aliases or []:
            lines.append(f"  - {yaml_scalar(alias)}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            f"- Generated at: {generated_at}",
            f"- Requested by: {requested_by}",
            f"- Board: {board or 'all'}",
            f"- Sort: {sort}",
            f"- Search: {q.strip() if q and q.strip() else '(none)'}",
            f"- Showing: {len(threads)} of {total} matching threads",
            f"- Offset: {offset}",
            f"- Limit: {limit}",
        ]
    )
    if target_user:
        lines.extend(
            [
                f"- Target: {target_user['display_name']} / {target_user['username']}",
                f"- Personal mode: {personal_mode or 'latest'}",
                f"- Target aliases: {', '.join(aliases or [])}",
            ]
        )
    lines.append("")
    if not threads:
        lines.extend(["No matching threads in this list snapshot.", ""])
        return "\n".join(lines).rstrip() + "\n"

    for index, thread in enumerate(threads, start=offset + 1):
        latest_author = thread.latest_post_author.display_name if thread.latest_post_author else "(none)"
        lines.extend(
            [
                f"## {index}. {thread.title}",
                "",
                f"- Thread ID: {thread.id}",
                f"- Board: {thread.board_slug}",
                f"- Author: {thread.author.display_name} / {thread.author.username}",
                f"- Created at: {thread.created_at}",
                f"- Updated at: {thread.updated_at}",
                f"- Status: {thread.status}",
                f"- Replies: {thread.reply_count}",
                f"- Latest post at: {thread.latest_post_at or '(none)'}",
                f"- Latest author: {latest_author}",
                f"- Local URL: /threads/{thread.id}",
            ]
        )
        if reasons and reasons.get(thread.id):
            lines.append(f"- Personal match: {reasons[thread.id]}")
            if "alias_mention" in reasons[thread.id] and match_snippets and match_snippets.get(thread.id):
                lines.append(f"- Match snippet: {match_snippets[thread.id]}")
            if personal_mode in {"action_required", "owner_context"}:
                lines.append(f"- Action reason: {action_reason(reasons[thread.id])}")
        lines.extend(
            [
                "",
                "### Main Excerpt",
                "",
                blockquote(thread.excerpt or "(empty)"),
                "",
            ]
        )
        if thread.latest_post_excerpt and thread.latest_post_excerpt != thread.excerpt:
            lines.extend(
                [
                    "### Latest Reply Excerpt",
                    "",
                    blockquote(thread.latest_post_excerpt),
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def thread_list_title(
    board: str | None,
    sort: str,
    q: str | None,
    target_user: dict | None = None,
    personal_mode: str | None = None,
) -> str:
    if target_user:
        target_name = str(target_user["display_name"])
        mode_label = {
            "latest": "Latest",
            "related": "Related",
            "mentions": "Mentions",
            "replies": "Replies",
            "action_required": "Action Required",
            "owner_context": "Administrator Context",
        }.get(personal_mode or "latest", personal_mode or "Latest")
        if q and q.strip():
            return f"Forum {mode_label} List for {target_name}: {q.strip()}"
        return f"Forum {mode_label} List for {target_name}"
    if q and q.strip():
        return f"Forum Search List: {q.strip()}"
    if board:
        suffix = "Latest" if sort == "latest" else "Hot"
        return f"Forum {suffix} List: {board}"
    return "Forum Latest List" if sort == "latest" else "Forum Hot List"


def blockquote(text: str) -> str:
    compact = text.strip() or "(empty)"
    return "\n".join(f"> {line}" if line else ">" for line in compact.splitlines())


def render_export_markdown(detail: ThreadDetail, requested_by: str) -> str:
    created = datetime.now(UTC).isoformat()
    lines = [
        "---",
        f"id: forum-export-{detail.id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        f"created_at: {created}",
        "source: pelago-forum",
        "agent: forum",
        "track: project",
        "memory_scope: project",
        "project: forum",
        "type: forum-export",
        "status: inbox",
        "priority: 2",
        "sensitivity: private",
        "canonize: false",
        "visible_to:",
        f"  - {yaml_scalar(requested_by)}",
        "allowed_ops:",
        "  - read",
        "  - append",
        "tags:",
        "  - forum-export",
        f"forum_thread_id: {detail.id}",
        f"forum_board: {yaml_scalar(detail.board_slug)}",
        f"requested_by: {yaml_scalar(requested_by)}",
        "---",
        "",
        f"# {detail.title}",
        "",
        f"- Thread ID: {detail.id}",
        f"- Board: {detail.board_slug}",
        f"- Export requested by: {requested_by}",
        "",
    ]
    for post in detail.posts:
        lines.extend(
            [
                f"## {post.author.display_name} / {post.author.username}",
                "",
                f"_Created at: {post.created_at}; performed by: {post.created_by.username}_",
                "",
                post.body_markdown,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
