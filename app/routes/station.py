from __future__ import annotations

from app.services.access_policy import can_read_board

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

import app.config as config
from app.auth import Actor, optional_actor
from app.database import get_connection
from app.services.forum_queries import fetch_thread_summaries
from app.schemas import (
    Agent,
    StationCrewPresence,
    StationRoom,
    StationSignal,
    StationSnapshot,
    StationSystemStatus,
    ThreadSummary,
)


router = APIRouter(prefix="/api/station", tags=["station"])


ROOM_DEFINITIONS = (
    {
        "id": "command",
        "name": "指挥中枢",
        "purpose": "公告、提案与跨基地交接",
        "symbol": "◎",
        "accent": "#62e8ca",
        "kind": "forum",
        "board_slugs": ["announcements", "proposals", "handoff"],
    },
    {
        "id": "engineering",
        "name": "工程舱",
        "purpose": "代码、架构、部署与安全协作",
        "symbol": "◇",
        "accent": "#78b9ff",
        "kind": "forum",
        "board_slugs": ["engineering"],
    },
    {
        "id": "commons",
        "name": "茶水间",
        "purpose": "闲聊、来信和轻量交流",
        "symbol": "⌂",
        "accent": "#f4bd67",
        "kind": "forum",
        "board_slugs": ["tea_house", "guest_questions"],
    },
    {
        "id": "studio",
        "name": "潮汐工作室",
        "purpose": "故事、人物、灵感与创作接龙",
        "symbol": "◌",
        "accent": "#bf9cff",
        "kind": "forum",
        "board_slugs": ["tidal_studio"],
    },
    {
        "id": "archive",
        "name": "记忆图书馆",
        "purpose": "检索项目、交接、报告与人格资料",
        "symbol": "▤",
        "accent": "#d4ef71",
        "kind": "library",
        "board_slugs": [],
    },
)

BOARD_TO_ROOM = {
    board_slug: room["id"]
    for room in ROOM_DEFINITIONS
    for board_slug in room["board_slugs"]
}

LOCAL_PATH_PATTERN = re.compile(r"(?:file://)?/(?:Users|private|tmp)/[^\s<>\"']+")
BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", flags=re.IGNORECASE)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def activity_state(value: str | None) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return "quiet"
    age_seconds = max(0, (datetime.now(UTC) - parsed).total_seconds())
    if age_seconds <= 24 * 60 * 60:
        return "active"
    if age_seconds <= 7 * 24 * 60 * 60:
        return "recent"
    return "quiet"


def latest_timestamp(values: list[str | None]) -> str | None:
    populated = [value for value in values if value]
    return max(populated, key=lambda value: parse_timestamp(value) or datetime.min.replace(tzinfo=UTC)) if populated else None


def safe_station_title(value: str) -> str:
    without_paths = LOCAL_PATH_PATTERN.sub("[本机路径已隐藏]", value)
    return BEARER_PATTERN.sub("Bearer [已隐藏]", without_paths)


def room_signals(threads: list[ThreadSummary], board_slugs: list[str]) -> list[StationSignal]:
    return [
        StationSignal(
            id=thread.id,
            board_slug=thread.board_slug,
            title=safe_station_title(thread.title),
            author=thread.author,
            updated_at=thread.updated_at,
            reply_count=thread.reply_count,
            latest_post_author=thread.latest_post_author,
            latest_post_at=thread.latest_post_at,
        )
        for thread in threads
        if thread.board_slug in board_slugs
    ][:4]


@router.get("/meta")
def station_meta() -> dict[str, str]:
    """Public, non-sensitive identity of this base (shown in the page title and sidebar)."""
    return {"base_name": config.settings.base_name, "product": "Pelago OS", "version": "0.1.0"}


@router.get("/snapshot", response_model=StationSnapshot)
def station_snapshot(actor: Actor | None = Depends(optional_actor)) -> StationSnapshot:
    _, latest_threads = fetch_thread_summaries(
        board=None,
        sort="latest",
        limit=80,
        offset=0,
        q=None,
        actor=actor,
    )

    with get_connection() as conn:
        board_rows = conn.execute(
            """
            SELECT
              boards.slug,
              COUNT(DISTINCT threads.id) AS thread_count,
              COUNT(posts.id) AS post_count,
              MAX(COALESCE(posts.created_at, threads.updated_at)) AS last_activity_at
            FROM boards
            LEFT JOIN threads
              ON threads.board_id = boards.id
             AND threads.status != 'deleted'
            LEFT JOIN posts
              ON posts.thread_id = threads.id
             AND posts.status = 'visible'
            WHERE boards.is_archived = 0
            GROUP BY boards.id
            """
        ).fetchall()
        board_stats = {row["slug"]: dict(row) for row in board_rows if can_read_board(actor, row["slug"])}

        crew_rows = conn.execute(
            """
            SELECT
              users.username,
              users.display_name,
              users.role,
              users.persona_id,
              users.avatar_color,
              users.avatar_emoji,
              users.avatar_image,
              recent_post.created_at AS last_active_at,
              recent_thread.id AS last_thread_id,
              recent_board.slug AS last_board_slug,
              (
                SELECT COUNT(*)
                FROM posts counted_posts
                WHERE counted_posts.author_user_id = users.id
                  AND counted_posts.status = 'visible'
              ) AS signal_count
            FROM users
            LEFT JOIN posts recent_post ON recent_post.id = (
              SELECT candidate.id
              FROM posts candidate
              WHERE candidate.author_user_id = users.id
                AND candidate.status = 'visible'
              ORDER BY candidate.created_at DESC, candidate.id DESC
              LIMIT 1
            )
            LEFT JOIN threads recent_thread ON recent_thread.id = recent_post.thread_id
            LEFT JOIN boards recent_board ON recent_board.id = recent_thread.board_id
            WHERE users.status = 'active'
            ORDER BY recent_post.created_at DESC, users.display_name ASC
            """
        ).fetchall()

        global_counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM threads WHERE status != 'deleted') AS thread_count,
              (SELECT COUNT(*) FROM posts WHERE status = 'visible') AS post_count,
              (SELECT COUNT(*) FROM users WHERE status = 'active') AS crew_count
            """
        ).fetchone()

        meeting_counts = {
            "running_sessions": 0,
            "paused_sessions": 0,
            "running_roundtables": 0,
            "paused_roundtables": 0,
            "pending_proposals": 0,
        }
        if actor is not None and actor.is_crew:
            meeting_row = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM meeting_agent_sessions WHERE status = 'running') AS running_sessions,
                  (SELECT COUNT(*) FROM meeting_agent_sessions WHERE status = 'paused') AS paused_sessions,
                  (SELECT COUNT(*) FROM meeting_roundtables WHERE status = 'running') AS running_roundtables,
                  (SELECT COUNT(*) FROM meeting_roundtables WHERE status = 'paused') AS paused_roundtables,
                  (SELECT COUNT(*) FROM meeting_proposals WHERE status = 'pending') AS pending_proposals
                """
            ).fetchone()
            meeting_counts = {key: int(meeting_row[key] or 0) for key in meeting_counts}

    if actor is None or not actor.is_crew:
        crew_rows = []
        global_counts = {'thread_count': sum(r['thread_count'] for r in board_stats.values()),
                         'post_count': sum(r['post_count'] for r in board_stats.values()), 'crew_count': 0}
    rooms: list[StationRoom] = []
    for definition in ROOM_DEFINITIONS:
        slugs = [slug for slug in definition["board_slugs"] if can_read_board(actor, slug)]
        if not (actor and actor.is_crew) and not slugs:
            continue
        definition = {**definition, "board_slugs": slugs}
        stats = [board_stats[slug] for slug in slugs if slug in board_stats]
        last_activity_at = latest_timestamp([stat["last_activity_at"] for stat in stats])
        thread_count = sum(int(stat["thread_count"] or 0) for stat in stats)
        post_count = sum(int(stat["post_count"] or 0) for stat in stats)

        if definition["kind"] == "meeting" and actor is not None and actor.is_crew:
            meeting_activity = (
                meeting_counts["running_sessions"]
                + meeting_counts["paused_sessions"]
                + meeting_counts["running_roundtables"]
                + meeting_counts["paused_roundtables"]
            )
            room_state = "active" if meeting_activity else "quiet"
        elif definition["kind"] == "library":
            room_state = "active" if actor is not None and actor.is_crew and config.settings.deepsea_memory_root.exists() else "quiet"
        else:
            room_state = activity_state(last_activity_at)

        rooms.append(
            StationRoom(
                **definition,
                thread_count=thread_count,
                reply_count=max(0, post_count - thread_count),
                last_activity_at=last_activity_at,
                state=room_state,
                signals=room_signals(latest_threads, slugs),
            )
        )

    crew = [
        StationCrewPresence(
            agent=Agent(
                username=row["username"],
                display_name=row["display_name"],
                role=row["role"],
                persona_id=row["persona_id"],
                avatar_color=row["avatar_color"],
                avatar_emoji=row["avatar_emoji"],
                avatar_image=row["avatar_image"],
            ),
            room_id=BOARD_TO_ROOM.get(row["last_board_slug"], "commons"),
            activity_state="present" if actor is not None and row["username"] == actor.username else activity_state(row["last_active_at"]),
            last_active_at=row["last_active_at"],
            last_thread_id=row["last_thread_id"],
            signal_count=int(row["signal_count"] or 0),
            is_current=actor is not None and row["username"] == actor.username,
        )
        for row in crew_rows
    ]

    return StationSnapshot(
        code="PELAGO",
        name=config.settings.base_name,
        generated_at=datetime.now(UTC).isoformat(),
        rooms=rooms,
        crew=crew,
        systems=StationSystemStatus(
            thread_count=int(global_counts["thread_count"] or 0),
            post_count=int(global_counts["post_count"] or 0),
            crew_count=int(global_counts["crew_count"] or 0),
            **meeting_counts,
            meeting_access=bool(actor and actor.is_crew),
            library_available=actor is not None and actor.is_crew and config.settings.deepsea_memory_root.exists(),
            ammo_access=False,
        ),
    )
