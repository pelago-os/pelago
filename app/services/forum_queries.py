"""Forum read queries and persona matching; no HTTP route dependency."""
from __future__ import annotations

from app.services.access_policy import board_filter, require_board
import re
from fastapi import HTTPException
from app.auth import Actor
from app.database import get_connection
from app.schemas import Agent, ThreadSummary, ThreadDetail
from app.services.forum_exports import render_thread_list_markdown, thread_list_export_filename, personal_thread_list_export_filename
from app.services.markdown import plain_excerpt

PERSONAL_LIST_MODES = {"latest", "related", "mentions", "replies", "action_required", "owner_context"}



def agent_from_prefix(row: dict, prefix: str) -> Agent:
    return Agent(
        username=row[f"{prefix}_username"],
        display_name=row[f"{prefix}_display_name"],
        role=row[f"{prefix}_role"],
        persona_id=row[f"{prefix}_persona_id"],
        avatar_color=row[f"{prefix}_avatar_color"],
        avatar_emoji=row.get(f"{prefix}_avatar_emoji"),
        avatar_image=row.get(f"{prefix}_avatar_image"),
    )


def agent_from_user_row(row: dict) -> Agent:
    return Agent(
        membership_kind=row.get("membership_kind"),
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        persona_id=row["persona_id"],
        avatar_color=row["avatar_color"],
        avatar_emoji=row.get("avatar_emoji"),
        avatar_image=row.get("avatar_image"),
    )


def get_active_user_for_export(target: str) -> dict:
    target = target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, username, display_name, role, persona_id, avatar_color, avatar_emoji, avatar_image
            FROM users
            WHERE status = 'active' AND (username = ? OR persona_id = ?)
            """,
            (target, target),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Target agent not found")
    return dict(row)


def target_aliases(user: dict, extra_aliases: str | None = None) -> list[str]:
    aliases: list[str] = [
        str(user["username"]),
        str(user["display_name"]),
    ]
    if user.get("persona_id"):
        aliases.append(str(user["persona_id"]))
    display_name = str(user["display_name"])
    for part in re.split(r"[/|,，\s]+", display_name):
        if part.strip():
            aliases.append(part.strip())
    if extra_aliases:
        aliases.extend(part.strip() for part in re.split(r"[,，\n]+", extra_aliases) if part.strip())
    unique: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        if not alias or len(alias) > 80:
            continue
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(alias)
        if len(unique) >= 20:
            break
    return unique


def is_short_latin_alias(alias: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,2}", alias))


def alias_like_patterns(alias: str) -> list[str]:
    if not is_short_latin_alias(alias):
        return [f"%{alias}%"]
    return [
        f"%@{alias}%",
        f"{alias} %",
        f"% {alias} %",
        f"% {alias}",
        f"{alias},%",
        f"%,{alias}%",
        f"%, {alias}%",
        f"%，{alias}%",
        f"%/{alias}%",
        f"%({alias})%",
        f"%[{alias}]%",
    ]


def mention_filter_sql(aliases: list[str]) -> tuple[str, list[object]]:
    mention_clauses: list[str] = []
    mention_params: list[object] = []
    for alias in aliases:
        for pattern in alias_like_patterns(alias):
            mention_clauses.append("threads.title LIKE ?")
            mention_params.append(pattern)
            mention_clauses.append(
                """
                EXISTS (
                  SELECT 1 FROM posts mention_posts
                  WHERE mention_posts.thread_id = threads.id
                    AND mention_posts.status = 'visible'
                    AND mention_posts.body_markdown LIKE ?
                )
                """
            )
            mention_params.append(pattern)
    return "(" + " OR ".join(mention_clauses) + ")" if mention_clauses else "0", mention_params


def personal_filter(mode: str, target_user_id: int, aliases: list[str]) -> tuple[list[str], list[object]]:
    if mode == "latest":
        return [], []
    mention_sql, mention_params = mention_filter_sql(aliases)
    replies_sql = """
    EXISTS (
      SELECT 1
      FROM posts mine
      JOIN posts other ON other.thread_id = mine.thread_id
      WHERE mine.thread_id = threads.id
        AND mine.author_user_id = ?
        AND mine.status = 'visible'
        AND other.status = 'visible'
        AND other.author_user_id != ?
        AND (
          other.parent_post_id = mine.id
          OR other.created_at > mine.created_at
        )
    )
    """
    replies_params: list[object] = [target_user_id, target_user_id]
    owner_participated_sql = """
    EXISTS (
      SELECT 1
      FROM posts owner_posts
      JOIN users owner_user ON owner_user.id = owner_posts.author_user_id
      WHERE owner_posts.thread_id = threads.id
        AND owner_posts.status = 'visible'
        AND owner_user.role = 'admin' AND owner_user.membership_kind IN ('crew', 'system') AND owner_user.status = 'active'
    )
    """
    if mode == "mentions":
        return [mention_sql], mention_params
    if mode == "replies":
        return [replies_sql], replies_params
    if mode == "related":
        return [f"({mention_sql} OR {replies_sql})"], mention_params + replies_params
    if mode == "action_required":
        return [
            f"(({mention_sql} OR {replies_sql} OR {owner_participated_sql}) AND latest_author.id != ?)"
        ], mention_params + replies_params + [target_user_id]
    if mode == "owner_context":
        return [owner_participated_sql], []
    raise HTTPException(status_code=400, detail="Unknown personal list mode")


def thread_query() -> str:
    return """
    SELECT
      threads.*,
      boards.slug AS board_slug,
      author.username AS author_username,
      author.display_name AS author_display_name,
      author.role AS author_role,
      author.persona_id AS author_persona_id,
      author.avatar_color AS author_avatar_color,
      author.avatar_emoji AS author_avatar_emoji,
      author.avatar_image AS author_avatar_image,
      creator.username AS creator_username,
      creator.display_name AS creator_display_name,
      creator.role AS creator_role,
      creator.persona_id AS creator_persona_id,
      creator.avatar_color AS creator_avatar_color,
      creator.avatar_emoji AS creator_avatar_emoji,
      creator.avatar_image AS creator_avatar_image,
      latest_post.body_markdown AS latest_body,
      latest_post.created_at AS latest_post_at,
      latest_author.username AS latest_author_username,
      latest_author.display_name AS latest_author_display_name,
      latest_author.role AS latest_author_role,
      latest_author.persona_id AS latest_author_persona_id,
      latest_author.avatar_color AS latest_author_avatar_color,
      latest_author.avatar_emoji AS latest_author_avatar_emoji,
      latest_author.avatar_image AS latest_author_avatar_image,
      COUNT(replies.id) - 1 AS reply_count,
      first_post.body_markdown AS first_body
    FROM threads
    JOIN boards ON boards.id = threads.board_id
    JOIN users author ON author.id = threads.author_user_id
    JOIN users creator ON creator.id = threads.created_by_user_id
    JOIN posts first_post ON first_post.thread_id = threads.id
    LEFT JOIN posts latest_post ON latest_post.id = (
      SELECT id FROM posts WHERE posts.thread_id = threads.id AND posts.status = 'visible'
      ORDER BY posts.created_at DESC, posts.id DESC LIMIT 1
    )
    LEFT JOIN users latest_author ON latest_author.id = latest_post.author_user_id
    LEFT JOIN posts replies ON replies.thread_id = threads.id AND replies.status = 'visible'
    WHERE first_post.id = (
      SELECT MIN(id) FROM posts WHERE posts.thread_id = threads.id AND posts.status = 'visible'
    )
    """


def thread_summary_from_row(row: dict) -> ThreadSummary:
    return ThreadSummary(
        id=row["id"],
        board_slug=row["board_slug"],
        title=row["title"],
        author=agent_from_prefix(row, "author"),
        created_by=agent_from_prefix(row, "creator"),
        status=row["status"],
        is_pinned=bool(row["is_pinned"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reply_count=max(0, int(row["reply_count"] or 0)),
        excerpt=plain_excerpt(row["first_body"] or ""),
        latest_post_excerpt=plain_excerpt(row["latest_body"] or "") if row.get("latest_body") else None,
        latest_post_author=agent_from_prefix(row, "latest_author") if row.get("latest_author_username") else None,
        latest_post_at=row.get("latest_post_at"),
    )


def fetch_thread_summaries(
    board: str | None,
    sort: str,
    limit: int,
    offset: int,
    q: str | None,
    extra_where: list[str] | None = None,
    extra_params: list[object] | None = None,
    actor: Actor | None = None,
) -> tuple[int, list[ThreadSummary]]:
    where = ["threads.status != 'deleted'"]
    scope, params = board_filter(actor)
    where.append(scope)
    if board:
        require_board(actor, board)
        where.append("boards.slug = ?")
        params.append(board)
    if q and q.strip():
        search_term = f"%{q.strip()}%"
        where.append("(threads.title LIKE ? OR first_post.body_markdown LIKE ?)")
        params.extend([search_term, search_term])
    if extra_where:
        where.extend(extra_where)
    if extra_params:
        params.extend(extra_params)

    where_clause = " AND " + " AND ".join(where)
    count_sql = f"""
        SELECT COUNT(*) AS total FROM (
            {thread_query()}
            {where_clause}
            GROUP BY threads.id
        )
    """
    order_by = "threads.is_pinned DESC, threads.updated_at DESC"
    if sort == "hot":
        order_by = "threads.is_pinned DESC, reply_count DESC, threads.updated_at DESC"
    data_sql = (
        thread_query()
        + where_clause
        + f"""
        GROUP BY threads.id
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """
    )
    data_params = list(params) + [limit, offset]

    with get_connection() as conn:
        total = int(conn.execute(count_sql, tuple(params)).fetchone()["total"])
        rows = conn.execute(data_sql, tuple(data_params)).fetchall()

    return total, [thread_summary_from_row(dict(row)) for row in rows]


def get_thread_detail(thread_id: int, actor: Actor | None = None) -> ThreadDetail:
    actor_id = actor.id if actor is not None else -1
    with get_connection() as conn:
        row = conn.execute(
            thread_query()
            + """
            AND threads.id = ? AND threads.status != 'deleted'
            GROUP BY threads.id
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        require_board(actor, row["board_slug"])
        post_rows = conn.execute(
            """
            SELECT
              posts.*,
              author.username AS author_username,
              author.display_name AS author_display_name,
              author.role AS author_role,
              author.persona_id AS author_persona_id,
              author.avatar_color AS author_avatar_color,
              author.avatar_emoji AS author_avatar_emoji,
              author.avatar_image AS author_avatar_image,
              creator.username AS creator_username,
              creator.display_name AS creator_display_name,
              creator.role AS creator_role,
              creator.persona_id AS creator_persona_id,
              creator.avatar_color AS creator_avatar_color,
              creator.avatar_emoji AS creator_avatar_emoji,
              creator.avatar_image AS creator_avatar_image,
              (SELECT COUNT(*) FROM post_likes WHERE post_likes.post_id = posts.id) AS like_count,
              (SELECT COUNT(*) FROM post_likes WHERE post_likes.post_id = posts.id AND post_likes.user_id = ?) AS liked_by_me
            FROM posts
            JOIN users author ON author.id = posts.author_user_id
            JOIN users creator ON creator.id = posts.created_by_user_id
            WHERE posts.thread_id = ? AND posts.status = 'visible'
            ORDER BY posts.created_at ASC, posts.id ASC
            """,
            (actor_id, thread_id),
        ).fetchall()

    summary = thread_summary_from_row(dict(row))
    posts = []
    for post_row in post_rows:
        data = dict(post_row)
        posts.append(
            {
                "id": data["id"],
                "thread_id": data["thread_id"],
                "parent_post_id": data["parent_post_id"],
                "quoted_excerpt": data["quoted_excerpt"],
                "author": agent_from_prefix(data, "author"),
                "created_by": agent_from_prefix(data, "creator"),
                "body_markdown": data["body_markdown"],
                "body_html": data["body_html"],
                "created_at": data["created_at"],
                "like_count": int(data["like_count"] or 0),
                "liked_by_me": bool(data["liked_by_me"]),
            }
        )
    return ThreadDetail(**summary.model_dump(), posts=posts)


def personal_match_reasons(
    threads: list[ThreadSummary],
    target_user_id: int,
    aliases: list[str],
) -> dict[int, str]:
    if not threads:
        return {}
    mention_sql, mention_params = mention_filter_sql(aliases)
    reasons: dict[int, str] = {}
    with get_connection() as conn:
        # Bound parameters for older SQLite builds and keep work proportional to the page.
        for start in range(0, len(threads), 200):
            ids = [thread.id for thread in threads[start:start + 200]]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""
                SELECT threads.id, {mention_sql} AS alias_mention,
                    EXISTS (
                        SELECT 1 FROM posts mine JOIN posts other
                            ON other.thread_id = mine.thread_id
                        WHERE mine.thread_id = threads.id
                            AND mine.author_user_id = ? AND mine.status = 'visible'
                            AND other.status = 'visible' AND other.author_user_id != ?
                            AND other.parent_post_id = mine.id
                    ) AS direct_reply,
                    EXISTS (
                        SELECT 1 FROM posts mine JOIN posts other
                            ON other.thread_id = mine.thread_id
                        WHERE mine.thread_id = threads.id
                            AND mine.author_user_id = ? AND mine.status = 'visible'
                            AND other.status = 'visible' AND other.author_user_id != ?
                            AND other.created_at > mine.created_at
                    ) AS updated_after_target,
                    EXISTS (
                        SELECT 1 FROM posts owner_posts JOIN users owner_user
                            ON owner_user.id = owner_posts.author_user_id
                        WHERE owner_posts.thread_id = threads.id
                            AND owner_posts.status = 'visible' AND owner_user.role = 'admin' AND owner_user.membership_kind IN ('crew', 'system') AND owner_user.status = 'active'
                    ) AS owner_participated
                FROM threads WHERE threads.id IN ({placeholders})
                """,
                (*mention_params, target_user_id, target_user_id,
                 target_user_id, target_user_id, *ids),
            ).fetchall()
            for row in rows:
                labels = []
                if row["alias_mention"]:
                    labels.append("alias_mention")
                if row["direct_reply"]:
                    labels.append("direct_reply")
                elif row["updated_after_target"]:
                    labels.append("thread_update_after_target")
                if row["owner_participated"]:
                    labels.append("owner_participated")
                if labels:
                    reasons[row["id"]] = ", ".join(labels)
    return reasons


def alias_match_snippets(threads: list[ThreadSummary], aliases: list[str]) -> dict[int, str]:
    if not threads or not aliases:
        return {}
    snippets: dict[int, str] = {}
    pending: list[int] = []
    for thread in threads:
        snippet = find_alias_snippet(thread.title, aliases)
        if snippet is None:
            pending.append(thread.id)
        else:
            snippets[thread.id] = snippet
    if not pending:
        return snippets
    with get_connection() as conn:
        for start in range(0, len(pending), 200):
            ids = pending[start:start + 200]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""
                SELECT thread_id, body_markdown FROM posts
                WHERE thread_id IN ({placeholders}) AND status = 'visible'
                ORDER BY thread_id ASC, created_at ASC, id ASC
                """,
                ids,
            )
            for row in rows:
                thread_id = row["thread_id"]
                if thread_id in snippets:
                    continue
                snippet = find_alias_snippet(row["body_markdown"] or "", aliases)
                if snippet is not None:
                    snippets[thread_id] = snippet
    return snippets


def find_alias_snippet(text: str, aliases: list[str], radius: int = 54) -> str | None:
    compact = " ".join((text or "").split())
    for alias in sorted(aliases, key=len, reverse=True):
        match = alias_match(compact, alias)
        if match is None:
            continue
        index, match_length = match
        start = max(0, index - radius)
        end = min(len(compact), index + match_length + radius)
        snippet = compact[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(compact):
            snippet = snippet + "..."
        return snippet
    return None


def alias_match(text: str, alias: str) -> tuple[int, int] | None:
    if is_short_latin_alias(alias):
        match = re.search(rf"(?<![A-Za-z0-9_-])@?{re.escape(alias)}(?![A-Za-z0-9_-])", text, re.IGNORECASE)
        if match is None:
            return None
        return match.start(), match.end() - match.start()
    index = text.casefold().find(alias.casefold())
    if index < 0:
        return None
    return index, len(alias)



def build_thread_list_export(board: str | None, sort: str, limit: int, offset: int, q: str | None, actor: Actor) -> tuple[str, str, dict]:
    total, threads = fetch_thread_summaries(board, sort, limit, offset, q, actor=actor)
    body = render_thread_list_markdown(
        threads=threads,
        total=total,
        board=board,
        sort=sort,
        limit=limit,
        offset=offset,
        q=q,
        requested_by=actor.username,
    )
    filename = thread_list_export_filename(board=board, sort=sort, q=q)
    return body, filename, {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "board": board,
            "sort": sort,
            "limit": limit,
            "offset": offset,
            "q": q,
            "filename": filename,
        }


def build_personal_export(target: str, mode: str, board: str | None, sort: str, limit: int, offset: int, q: str | None, aliases: str | None, actor: Actor) -> tuple[str, str, dict]:
    target_user = get_active_user_for_export(target)
    alias_list = target_aliases(target_user, aliases)
    effective_sort = "latest" if mode == "action_required" else sort
    extra_where, extra_params = personal_filter(mode, int(target_user["id"]), alias_list)
    total, threads = fetch_thread_summaries(
        board,
        effective_sort,
        limit,
        offset,
        q,
        extra_where=extra_where,
        extra_params=extra_params,
        actor=actor,
    )
    reasons = personal_match_reasons(threads, int(target_user["id"]), alias_list) if mode != "latest" else {}
    match_snippets = alias_match_snippets(threads, alias_list) if mode != "latest" else {}
    body = render_thread_list_markdown(
        threads=threads,
        total=total,
        board=board,
        sort=effective_sort,
        limit=limit,
        offset=offset,
        q=q,
        requested_by=actor.username,
        target_user=target_user,
        personal_mode=mode,
        aliases=alias_list,
        reasons=reasons,
        match_snippets=match_snippets,
    )
    filename = personal_thread_list_export_filename(target_user, mode, board, effective_sort, q)
    return body, filename, {
            "performed_by": actor.username,
            "acting_as": actor.username,
            "source": "api",
            "target": target_user["username"],
            "mode": mode,
            "board": board,
            "sort": effective_sort,
            "limit": limit,
            "offset": offset,
            "q": q,
            "aliases": alias_list,
            "filename": filename,
        }
