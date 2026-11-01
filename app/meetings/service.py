"""Meeting service: owns meeting, participant, slot, attempt, request and artifact rows.

The runner never writes those rows except through the calls in section 12 of
docs/meetings-contract.md, and this module never starts or signals processes.
Every public function here expects the caller to hold a `BEGIN IMMEDIATE` transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from app import config
from app.auth import Actor
from app.meetings import modes as M
from app.meetings import states as S
from app.meetings.adapter_config import load_agent_meeting_policy, load_members
from app.meetings.minutes import build_minutes, content_hash
from app.services.access_policy import PUBLIC_READ
from app.services.audit import queue_mutation_audit

clock: Callable[[], float] = time.time
_wake_hooks: list[Callable[[], None]] = []


class MeetingError(Exception):
    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


def on_wake(hook: Callable[[], None]) -> None:
    _wake_hooks.append(hook)


def wake() -> None:
    for hook in list(_wake_hooks):
        try:
            hook()
        except Exception:  # a hint only; the runner also polls
            pass


# ---------------------------------------------------------------- helpers

def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _meeting(conn, meeting_id: str) -> dict:
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        raise MeetingError(404, "not_found")
    return dict(row)


def _participants(conn, meeting_id: str) -> list[dict]:
    return _rows(conn.execute(
        """SELECT p.*, u.username, u.display_name FROM meeting_participants p
           JOIN users u ON u.id = p.user_id WHERE p.meeting_id = ? ORDER BY p.seat""", (meeting_id,)))


def _participant_for(conn, meeting_id: str, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM meeting_participants WHERE meeting_id = ? AND user_id = ?",
                       (meeting_id, user_id)).fetchone()
    return dict(row) if row else None


def _actor_for_user(conn, user_id: int) -> Actor:
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return Actor(id=u["id"], username=u["username"], display_name=u["display_name"], role=u["role"],
                 persona_id=u["persona_id"], membership_kind=u["membership_kind"])


def _event(conn, meeting_id: str, actor_id: int | None, kind: str, **payload) -> None:
    conn.execute("INSERT INTO meeting_events(meeting_id, at, actor_user_id, kind, payload_json) VALUES (?,?,?,?,?)",
                 (meeting_id, clock(), actor_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    queue_mutation_audit(conn, f"meeting.{kind}", dict(payload, meeting_id=meeting_id, actor_user_id=actor_id))


def _bump(conn, meeting_id: str) -> None:
    conn.execute("UPDATE meetings SET version = version + 1, updated_at = ? WHERE id = ?", (clock(), meeting_id))


def _require_crew(actor: Actor) -> None:
    if not actor.is_crew and actor.membership_kind != "system":
        raise MeetingError(403, "forbidden", "meetings are for crew members")


def _is_organizer_or_admin(actor: Actor, m: dict) -> bool:
    return actor.is_admin or actor.id == m["organizer_user_id"]


def _require_organizer(actor: Actor, m: dict) -> None:
    if not _is_organizer_or_admin(actor, m):
        raise MeetingError(403, "forbidden", "organizer or administrator only")


def _bounded(name: str, value: Any) -> int:
    default, low, high = S.BUDGETS[name]
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise MeetingError(422, "out_of_range", {name: value}) from None
    if not low <= value <= high:
        raise MeetingError(422, "out_of_range", {name: value, "range": [low, high]})
    return value


def _apply_dormancy(conn, m: dict) -> dict:
    """Section 4: dormant is written lazily on read or sweep."""
    if m["state"] == S.PAUSED and m["paused_at"] and m["paused_at"] + m["stale_after_s"] < clock():
        conn.execute("UPDATE meetings SET state = 'dormant', dormant_at = ? WHERE id = ?", (clock(), m["id"]))
        _bump(conn, m["id"])
        _event(conn, m["id"], None, "dormant")
        _rebuild_minutes(conn, m["id"])
        m = _meeting(conn, m["id"])
    return m


# ---------------------------------------------------------------- materials (section 8)

def _read_material(path_text: str) -> tuple[Path, dict, str] | None:
    from app.services.library import parse_frontmatter, resolve_relative_path, resolve_root
    try:
        root = resolve_root(config.settings.deepsea_memory_root)
        full = resolve_relative_path(root, path_text)
        text = full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    parsed = parse_frontmatter(text)
    return full.relative_to(root), parsed.frontmatter, parsed.body


def _unreadable(paths: Iterable[str], actors: Iterable[Actor]) -> list[str]:
    from app.services.library import actor_can_read
    actors = list(actors)
    bad = []
    for path in paths:
        item = _read_material(path)
        if item is None:
            bad.append(path)
            continue
        rel, meta, _ = item
        if not all(actor_can_read(a, rel, meta) for a in actors):
            bad.append(path)
    return bad


def _material_paths(conn, meeting_id: str) -> list[str]:
    return [r["path"] for r in conn.execute(
        "SELECT path FROM meeting_materials WHERE meeting_id = ? ORDER BY path", (meeting_id,))]


def _active_actors(conn, meeting_id: str) -> list[Actor]:
    return [_actor_for_user(conn, p["user_id"]) for p in _participants(conn, meeting_id) if p["status"] == "active"]


def _material_error(actor: Actor, m: dict, bad: list[str]) -> MeetingError:
    detail = {"paths": bad} if _is_organizer_or_admin(actor, m) else None
    return MeetingError(409, "material_not_readable", detail)


# ---------------------------------------------------------------- minutes (section 9)

def _rebuild_minutes(conn, meeting_id: str) -> None:
    m = _meeting(conn, meeting_id)
    org = conn.execute("SELECT display_name FROM users WHERE id = ?", (m["organizer_user_id"],)).fetchone()
    m["organizer_name"] = org["display_name"] if org else None
    body = build_minutes(
        m, _participants(conn, meeting_id),
        _rows(conn.execute("SELECT * FROM meeting_slots WHERE meeting_id = ?", (meeting_id,))),
        _rows(conn.execute("SELECT * FROM meeting_attempts WHERE meeting_id = ?", (meeting_id,))),
        _interjections(conn, meeting_id))
    digest = content_hash(body)
    if conn.execute("SELECT 1 FROM meeting_artifacts WHERE meeting_id = ? AND kind = 'minutes' AND content_hash = ?",
                    (meeting_id, digest)).fetchone():
        return
    rev = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM meeting_artifacts WHERE meeting_id = ? AND kind = 'minutes'",
                       (meeting_id,)).fetchone()[0]
    conn.execute("""INSERT INTO meeting_artifacts(meeting_id, kind, revision, content_hash, body_md, state, exit_reason, created_at)
                    VALUES (?, 'minutes', ?, ?, ?, ?, ?, ?)""",
                 (meeting_id, rev, digest, body, m["state"], m["exit_reason"], clock()))


# ---------------------------------------------------------------- state changes

def _set_state(conn, m: dict, target: str, exit_reason: str | None, actor_id: int | None) -> None:
    if not S.can_transition(m["state"], target):
        raise MeetingError(409, "invalid_transition", {"from": m["state"], "to": target})
    now = clock()
    fields = {"state": target, "exit_reason": exit_reason}
    if target == S.RUNNING:
        fields.update(exit_reason=None, paused_at=None, dormant_at=None)
        if not m["started_at"]:
            fields["started_at"] = now
    elif target == S.PAUSED:
        fields["paused_at"] = now
    elif target in S.TERMINAL_MEETING:
        fields["ended_at"] = now
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE meetings SET {sets} WHERE id = ?", (*fields.values(), m["id"]))
    _bump(conn, m["id"])
    _event(conn, m["id"], actor_id, f"state.{target}", exit_reason=exit_reason)
    m.update(fields)
    if target != S.RUNNING:
        _rebuild_minutes(conn, m["id"])


def _cancel_attempts(conn, meeting_id: str, reason: str, seat: int | None = None) -> bool:
    """Stop this meeting's live attempts. Returns True if any running process must drain."""
    q = "SELECT * FROM meeting_attempts WHERE meeting_id = ? AND state IN ('queued','claimed','running')"
    args: list[Any] = [meeting_id]
    if seat is not None:
        q += " AND seat = ?"
        args.append(seat)
    draining = False
    for a in _rows(conn.execute(q, args)):
        if a["state"] == S.QUEUED:
            conn.execute("UPDATE meeting_attempts SET state = 'cancelled', cancel_reason = ?, ended_at = ? WHERE id = ?",
                         (reason, clock(), a["id"]))
        else:
            conn.execute("""UPDATE meeting_attempts SET state = 'cancel_requested', cancel_reason = ?,
                            cancel_requested_at = ? WHERE id = ?""", (reason, clock(), a["id"]))
            draining = True
    if draining:
        wake()
    return draining


def _next_open_slot(conn, meeting_id: str) -> dict | None:
    row = conn.execute("""SELECT * FROM meeting_slots WHERE meeting_id = ? AND state = 'open'
                          ORDER BY round, seat LIMIT 1""", (meeting_id,)).fetchone()
    return dict(row) if row else None


def _create_slots(conn, meeting_id: str, seat: int, first_round: int, last_round: int) -> None:
    for r in range(first_round, last_round + 1):
        conn.execute("""INSERT INTO meeting_slots(meeting_id, round, seat, state, updated_at) VALUES (?,?,?,'open',?)
                        ON CONFLICT(meeting_id, round, seat) DO NOTHING""", (meeting_id, r, seat, clock()))


def enqueue_next(conn, meeting_id: str) -> str | None:
    """Section 12: only for running meetings; idempotent through the per-slot index."""
    m = _meeting(conn, meeting_id)
    if m["state"] != S.RUNNING:
        return None
    live = conn.execute(f"SELECT id FROM meeting_attempts WHERE meeting_id = ? AND state IN {S.NON_TERMINAL}",
                        (meeting_id,)).fetchone()
    if live:
        return live["id"]
    slot = _next_open_slot(conn, meeting_id)
    if slot is None:
        _set_state(conn, m, S.COMPLETED, "completed", None)
        return None
    if not m["approved_by"] and _agent_organized(conn, m) and _small_meeting_violations(conn, m, count_today=False):
        _pause(conn, m, "agent_policy_changed", "shutdown", None)  # the policy was closed or tightened mid-meeting
        return None
    p = conn.execute("SELECT * FROM meeting_participants WHERE meeting_id = ? AND seat = ?",
                     (meeting_id, slot["seat"])).fetchone()
    n = conn.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM meeting_attempts WHERE meeting_id = ? AND round = ? AND seat = ?",
                     (meeting_id, slot["round"], slot["seat"])).fetchone()[0]
    attempt_id = "att_" + uuid.uuid4().hex[:16]
    conn.execute("""INSERT INTO meeting_attempts(id, meeting_id, round, seat, participant_id, agent_user_id, attempt, state, queued_at)
                    VALUES (?,?,?,?,?,?,?, 'queued', ?)""",
                 (attempt_id, meeting_id, slot["round"], slot["seat"], p["id"], p["user_id"], n, clock()))
    wake()
    return attempt_id


# ---------------------------------------------------------------- requests (section 11)

_ORGANIZER_ACTIONS = frozenset({"patch", "materials", "start", "pause", "resume", "skip", "delete", "export"})


def authorize(conn, actor: Actor, meeting_id: str | None, action: str) -> None:
    """Section 3 and 11: visibility and permission come before replay and version checks,
    so neither a cached response nor a version error can reveal a meeting to an outsider."""
    _require_crew(actor)
    if meeting_id is None:
        return
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        raise MeetingError(404, "not_found")
    m = dict(row)
    view = _view(conn, actor, m)
    if view is None:
        raise MeetingError(404, "not_found")
    if action in _ORGANIZER_ACTIONS and not _is_organizer_or_admin(actor, m):
        raise MeetingError(403, "forbidden", "organizer or administrator only")
    if action in ("approve", "reject") and not actor.is_admin:
        raise MeetingError(403, "forbidden", "administrators approve or reject meetings")
    if action == "interject":
        p = _participant_for(conn, meeting_id, actor.id)
        if view[0] != "full" or not _is_person(conn, actor.id) or (p is not None and p["status"] != "active"):
            raise MeetingError(403, "forbidden", "only people interject; agents speak in their turns")
    if action == "close" and not _is_organizer_or_admin(actor, m) and view[0] != "full":
        raise MeetingError(403, "forbidden", "only the organizer closes a meeting that is not dormant")


def handle_request(conn, actor: Actor, meeting_id: str | None, request_id: str | None, action: str,
                   payload: dict, expected_version: int | None, fn: Callable[[], dict]) -> dict:
    if not request_id or len(request_id) > 100:
        raise MeetingError(400, "invalid_request", "request_id is required")
    key = meeting_id or f"create:{actor.id}"
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    row = conn.execute("SELECT * FROM meeting_requests WHERE meeting_id = ? AND request_id = ?", (key, request_id)).fetchone()
    if row:
        if (row["actor_user_id"], row["action"], row["payload_hash"]) != (actor.id, action, digest):
            raise MeetingError(409, "request_id_reused")
        return json.loads(row["response_json"])
    if meeting_id is not None and expected_version is not None:
        m = _meeting(conn, meeting_id)
        if m["version"] != expected_version:
            raise MeetingError(409, "version_conflict", {"version": m["version"], "state": m["state"]})
    response = fn()
    conn.execute("""INSERT INTO meeting_requests(meeting_id, request_id, actor_user_id, action, payload_hash, response_json, created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (key, request_id, actor.id, action, digest, json.dumps(response, ensure_ascii=False, default=str), clock()))
    return response


# ---------------------------------------------------------------- views (section 3)

def _view(conn, actor: Actor, m: dict) -> tuple[str, float | None] | None:
    if m.get("deleted_at") is not None:
        return None  # deleted meetings are hidden from everyone; the rows stay in the database
    if actor.is_admin or actor.id == m["organizer_user_id"]:
        return ("full", None)
    p = _participant_for(conn, m["id"], actor.id)
    if p is None:
        return None
    if p["status"] == "active":
        return ("full", None)
    return ("snapshot", p["left_at"])


def get_meeting(conn, actor: Actor, meeting_id: str) -> dict:
    _require_crew(actor)
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    view = _view(conn, actor, m)
    if view is None:
        raise MeetingError(404, "not_found")
    if view[0] == "snapshot":
        snap = _snapshot(conn, meeting_id, actor.id)
        if snap is not None:
            return snap
    out = _serialize(conn, actor, m, view)
    p = _participant_for(conn, meeting_id, actor.id)
    out["can_interject"] = (m["state"] in (S.RUNNING, S.PAUSED, S.DORMANT) and _is_person(conn, actor.id)
                            and (p is None or p["status"] == "active"))
    return out


def _snapshot(conn, meeting_id: str, user_id: int) -> dict | None:
    row = conn.execute("SELECT view_json FROM meeting_snapshots WHERE meeting_id = ? AND user_id = ?",
                       (meeting_id, user_id)).fetchone()
    return json.loads(row["view_json"]) if row else None


def _take_snapshot(conn, meeting_id: str, user_id: int) -> None:
    """Freeze everything a leaving member could see: state, settings, members, slots, turns, materials."""
    m = _meeting(conn, meeting_id)
    view = _serialize(conn, None, m, ("full", None))
    view["view"] = "snapshot"
    conn.execute("""INSERT INTO meeting_snapshots(meeting_id, user_id, taken_at, view_json) VALUES (?,?,?,?)
                    ON CONFLICT(meeting_id, user_id) DO UPDATE SET taken_at = excluded.taken_at, view_json = excluded.view_json""",
                 (meeting_id, user_id, clock(), json.dumps(view, ensure_ascii=False, default=str)))


def _serialize(conn, actor: Actor, m: dict, view: tuple[str, float | None]) -> dict:
    mode, cutoff = view
    parts = _participants(conn, m["id"])
    attempts = _rows(conn.execute("SELECT * FROM meeting_attempts WHERE meeting_id = ? ORDER BY round, seat, attempt", (m["id"],)))
    slots = _rows(conn.execute("SELECT * FROM meeting_slots WHERE meeting_id = ? ORDER BY round, seat", (m["id"],)))
    said = _interjections(conn, m["id"])
    if mode == "snapshot":
        attempts = [a for a in attempts if a["ended_at"] is not None and a["ended_at"] <= cutoff]
        said = [i for i in said if i["created_at"] <= cutoff]
        accepted_ids = {a["id"] for a in attempts if a["state"] == S.ACCEPTED}
        slots = [dict(s, state="filled") if s["filled_attempt_id"] in accepted_ids else
                 dict(s, state="open", filled_attempt_id=None) for s in slots]
        parts = [p for p in parts if p["joined_at"] <= cutoff]
    keep = ("id", "round", "seat", "attempt", "state", "cancel_reason", "text", "raw_text", "truncated",
            "char_count", "provider", "model", "requested_model", "fallback_used", "tool_surface", "adapter_rev",
            "error_code", "queued_at", "ended_at")
    return {
        "id": m["id"], "title": m["title"], "topic": m["topic"], "state": m["state"],
        "exit_reason": m["exit_reason"], "version": m["version"], "view": mode,
        "organizer_user_id": m["organizer_user_id"], "approved_by": m["approved_by"],
        "settings": {k: m[k] for k in ("rounds", "max_chars", "moderator_seat", "turn_timeout_s",
                                       "meeting_timeout_s", "stale_after_s", "wrap_up", "wrap_up_seat", "mode")},
        "decision": _decision(conn, m),
        "run_seconds": round(m["run_seconds"], 3),
        "participants": [{k: p[k] for k in ("user_id", "username", "display_name", "kind", "seat", "status", "model",
                                            "joined_from_round", "role")} for p in parts],
        "slots": [{k: s[k] for k in ("round", "seat", "state", "filled_attempt_id", "skip_reason")} for s in slots],
        "attempts": [dict({k: a[k] for k in keep}, support_tier="mock" if a["provider"] == "mock" else "experimental" if a["provider"] in ("claude_code", "hermes") else "unknown") for a in attempts],
        "materials": _material_paths(conn, m["id"]),
        "approval": _approval_view(conn, m),
        "interjections": [{k: i[k] for k in ("id", "round", "author_user_id", "username", "display_name", "text",
                                             "created_at")} for i in said],
    }


def list_meetings(conn, actor: Actor) -> list[dict]:
    _require_crew(actor)
    if actor.is_admin:
        rows = conn.execute("SELECT * FROM meetings WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute("""SELECT m.* FROM meetings m WHERE m.deleted_at IS NULL AND (m.organizer_user_id = ? OR EXISTS (
                               SELECT 1 FROM meeting_participants p WHERE p.meeting_id = m.id AND p.user_id = ?))
                               ORDER BY m.updated_at DESC LIMIT 200""", (actor.id, actor.id)).fetchall()
    out = []
    for r in rows:
        item = {k: r[k] for k in ("id", "title", "state", "exit_reason", "version", "updated_at", "mode")}
        item["decision"] = _decision(conn, dict(r))
        approval = _approval_view(conn, dict(r))
        item["agent_organized"] = approval is not None
        item["awaiting_approval"] = bool(approval and approval["needed"])
        item["can_delete"] = r["state"] in DELETABLE and _is_organizer_or_admin(actor, dict(r))
        if not actor.is_admin and r["organizer_user_id"] != actor.id:
            p = _participant_for(conn, r["id"], actor.id)
            snap = _snapshot(conn, r["id"], actor.id) if p and p["status"] == "left" else None
            if snap is not None:
                item = {"id": snap["id"], "title": snap["title"], "state": snap["state"],
                        "exit_reason": snap["exit_reason"], "version": snap["version"], "view": "snapshot"}
        out.append(item)
    return out


def get_minutes(conn, actor: Actor, meeting_id: str) -> dict:
    _require_crew(actor)
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    view = _view(conn, actor, m)
    if view is None:
        raise MeetingError(404, "not_found")
    q = "SELECT revision, state, exit_reason, created_at, body_md FROM meeting_artifacts WHERE meeting_id = ? AND kind = 'minutes'"
    args: list[Any] = [meeting_id]
    if view[0] == "snapshot":
        snap_row = conn.execute("SELECT taken_at FROM meeting_snapshots WHERE meeting_id = ? AND user_id = ?",
                                (meeting_id, actor.id)).fetchone()
        q += " AND created_at <= ?"
        args.append(snap_row["taken_at"] if snap_row else view[1])
    revs = _rows(conn.execute(q + " ORDER BY revision", args))
    return {"latest": revs[-1] if revs else None,
            "revisions": [{k: r[k] for k in ("revision", "state", "exit_reason", "created_at")} for r in revs]}


# ---------------------------------------------------------------- create / edit

def create_meeting(conn, actor: Actor, payload: dict) -> dict:
    _require_crew(actor)
    members = load_members()
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 200:
        raise MeetingError(400, "invalid_request", "title is required (200 characters at most)")
    usernames = [actor.username] + [u for u in payload.get("participants") or [] if u != actor.username]
    if len(set(usernames)) != len(usernames):
        raise MeetingError(400, "invalid_request", "participants must be unique")
    users = []
    for name in usernames:
        u = conn.execute("SELECT * FROM users WHERE username = ? AND status = 'active'", (name,)).fetchone()
        if u is None or u["membership_kind"] not in ("crew", "system"):
            raise MeetingError(422, "out_of_range", {"participant": name, "reason": "not an active crew member"})
        users.append(u)
    agents = [u for u in users if u["username"] in members]
    low, high = S.AGENT_PARTICIPANTS
    if not low <= len(agents) <= high:
        raise MeetingError(422, "out_of_range", {"agent_participants": len(agents), "range": [low, high]})
    max_chars = payload.get("max_chars", S.default_max_chars())
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        raise MeetingError(422, "out_of_range", {"max_chars": max_chars}) from None
    if not S.MAX_CHARS_RANGE[0] <= max_chars <= S.MAX_CHARS_RANGE[1]:
        raise MeetingError(422, "out_of_range", {"max_chars": max_chars, "range": list(S.MAX_CHARS_RANGE)})
    mode = _mode_choice(payload.get("mode"))
    rounds = _mode_rounds(mode, payload.get("rounds"))
    if M.mode_rounds(mode) and payload.get("wrap_up") is False:
        raise MeetingError(422, "out_of_range", {"wrap_up": False, "reason": "this mode ends with the chair's wrap-up"})
    turn_timeout = _bounded("turn_timeout_s", payload.get("turn_timeout_s"))
    meeting_timeout = _bounded("meeting_timeout_s", payload.get("meeting_timeout_s"))
    materials = list(dict.fromkeys(payload.get("materials") or []))
    participant_actors = [_actor_for_user(conn, u["id"]) for u in users]
    bad = _unreadable(materials, participant_actors)
    if bad:
        raise MeetingError(409, "material_not_readable", {"paths": bad})
    moderator = payload.get("moderator")
    moderator_seat = None
    if moderator is not None:
        if moderator not in usernames or moderator not in members:
            raise MeetingError(422, "out_of_range", {"moderator": moderator, "reason": "must be an agent participant"})
        moderator_seat = usernames.index(moderator)
    chosen = _choose_models(payload.get("models") or {}, usernames, members)
    wrap_up, wrap_seat = _wrap_up_choice(payload, actor.username, usernames, members)
    now = clock()
    meeting_id = "mtg_" + uuid.uuid4().hex[:16]
    conn.execute("""INSERT INTO meetings(id, title, topic, organizer_user_id, state, rounds, max_chars, moderator_seat,
                    turn_timeout_s, meeting_timeout_s, stale_after_s, wrap_up, wrap_up_seat, mode, created_at, updated_at)
                    VALUES (?,?,?,?, 'draft', ?,?,?,?,?,?,?,?,?,?,?)""",
                 (meeting_id, title, str(payload.get("topic") or "")[:4000], actor.id, rounds, max_chars, moderator_seat,
                  turn_timeout, meeting_timeout, S.STALE_AFTER_S, int(wrap_up), wrap_seat, mode, now, now))
    for seat, u in enumerate(users):
        conn.execute("""INSERT INTO meeting_participants(meeting_id, user_id, kind, seat, status, joined_from_round, model, joined_at)
                        VALUES (?,?,?,?, 'active', 1, ?, ?)""",
                     (meeting_id, u["id"], "agent" if u["username"] in members else "human", seat,
                      chosen.get(u["username"]), now))
    for path in materials:
        conn.execute("INSERT INTO meeting_materials(meeting_id, path, added_by, added_at) VALUES (?,?,?,?)",
                     (meeting_id, path, actor.id, now))
    _event(conn, meeting_id, actor.id, "created", participants=usernames)
    return get_meeting(conn, actor, meeting_id)


def _wrap_up_choice(payload: dict, organizer: str, usernames: list[str], members: dict,
                    current: tuple[bool, int | None] = (True, None)) -> tuple[bool, int | None]:
    """Section 5: who sums the meeting up. Only an agent who speaks; the organizer by default only when the
    organizer is one; never picked silently for a person."""
    wrap_up = bool(payload.get("wrap_up", current[0]))
    if not wrap_up:
        return False, None
    name = payload.get("wrap_up_by")
    if name is None and current[1] is not None and current[1] < len(usernames):
        name = usernames[current[1]]
    if name is None and organizer in members:
        name = organizer
    if name is None:
        return True, None  # 待指定: start asks for a choice instead of guessing one
    if name not in usernames or name not in members:
        raise MeetingError(422, "out_of_range", {"wrap_up_by": name, "reason": "choose an agent participant to wrap up"})
    return True, usernames.index(name)


def _mode_choice(value: Any) -> str:
    mode = M.DISCUSSION if value is None else value
    if mode not in M.MODES:
        raise MeetingError(422, "out_of_range", {"mode": value, "allowed": list(M.MODES)})
    return mode


def _mode_rounds(mode: str, requested: Any) -> int:
    """Section 5.1: a mode brings its own rounds; asking for others is refused rather than silently changed."""
    fixed = M.mode_rounds(mode)
    if fixed is None:
        return _bounded("rounds", requested)
    if requested is not None and requested != fixed:
        raise MeetingError(422, "out_of_range", {"rounds": requested, "reason": f"{mode} has {fixed} rounds"})
    return fixed


def _assign_roles(conn, m: dict) -> None:
    """Section 5.1: in `initiation` each speaking agent gets the next public perspective, in seat order."""
    if not M.MODES[m["mode"]].get("perspectives"):
        return
    parts = [p for p in _participants(conn, m["id"]) if p["kind"] == "agent"]
    given = sum(1 for p in parts if p["role"])
    for p in parts:
        if p["status"] == "active" and not p["role"]:
            role = M.PERSPECTIVES[given % len(M.PERSPECTIVES)][0]
            conn.execute("UPDATE meeting_participants SET role = ? WHERE id = ?", (role, p["id"]))
            given += 1


def _choose_models(requested: dict, usernames: list[str], members: dict) -> dict[str, str]:
    """Each agent speaks with a model the administrator allowed for that member, never another."""
    if not isinstance(requested, dict):
        raise MeetingError(400, "invalid_request", "models must map usernames to model names")
    chosen = {}
    for name in usernames:
        member = members.get(name)
        if member is None:
            continue
        model = requested.get(name) or member.default_model
        if member.models and model not in member.models:
            raise MeetingError(422, "out_of_range", {"model": model, "member": name, "allowed": list(member.models)})
        chosen[name] = model or None
    unknown = set(requested) - set(chosen)
    if unknown:
        raise MeetingError(422, "out_of_range", {"models_for_non_agents": sorted(unknown)})
    return chosen


def attach_materials(conn, actor: Actor, meeting_id: str, paths: list[str]) -> dict:
    m = _meeting(conn, meeting_id)
    _require_organizer(actor, m)
    if m["state"] != S.DRAFT:
        raise MeetingError(409, "invalid_transition", "materials can only be attached in draft")
    paths = [p for p in dict.fromkeys(paths) if p not in _material_paths(conn, meeting_id)]
    bad = _unreadable(paths, _active_actors(conn, meeting_id))
    if bad:
        raise _material_error(actor, m, bad)
    for path in paths:
        conn.execute("INSERT INTO meeting_materials(meeting_id, path, added_by, added_at) VALUES (?,?,?,?)",
                     (meeting_id, path, actor.id, clock()))
    _bump(conn, meeting_id)
    _event(conn, meeting_id, actor.id, "materials", paths=paths)
    return get_meeting(conn, actor, meeting_id)


def patch_meeting(conn, actor: Actor, meeting_id: str, payload: dict) -> dict:
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    _require_organizer(actor, m)
    if m["state"] not in (S.DRAFT, S.PAUSED, S.DORMANT):
        raise MeetingError(409, "invalid_transition", "pause the meeting before editing it")
    members = load_members()
    if "mode" in payload:
        if m["state"] != S.DRAFT:
            raise MeetingError(409, "invalid_transition", "the mode is chosen before the meeting starts")
        mode = _mode_choice(payload["mode"])
        rounds = _mode_rounds(mode, payload.get("rounds")) if M.mode_rounds(mode) else m["rounds"]
        conn.execute("UPDATE meetings SET mode = ?, rounds = ? WHERE id = ?", (mode, rounds, meeting_id))
        if M.mode_rounds(mode) and not m["wrap_up"]:
            conn.execute("UPDATE meetings SET wrap_up = 1 WHERE id = ?", (meeting_id,))
        m = _meeting(conn, meeting_id)
    fixed = M.mode_rounds(m["mode"])
    if fixed and "rounds" in payload and payload["rounds"] != fixed:
        raise MeetingError(422, "out_of_range", {"rounds": payload["rounds"], "reason": f"{m['mode']} has {fixed} rounds"})
    if fixed and payload.get("wrap_up") is False:
        raise MeetingError(422, "out_of_range", {"wrap_up": False, "reason": "this mode ends with the chair's wrap-up"})
    for name in ("turn_timeout_s", "meeting_timeout_s"):
        if name in payload:
            conn.execute(f"UPDATE meetings SET {name} = ? WHERE id = ?", (_bounded(name, payload[name]), meeting_id))
    if "max_chars" in payload:
        value = int(payload["max_chars"])
        if not S.MAX_CHARS_RANGE[0] <= value <= S.MAX_CHARS_RANGE[1]:
            raise MeetingError(422, "out_of_range", {"max_chars": value})
        conn.execute("UPDATE meetings SET max_chars = ? WHERE id = ?", (value, meeting_id))
    if "models" in payload:
        names = [p["username"] for p in _participants(conn, meeting_id)]
        for name, model in _choose_models(payload["models"], names, members).items():
            if name in payload["models"]:
                conn.execute("""UPDATE meeting_participants SET model = ? WHERE meeting_id = ?
                                AND user_id = (SELECT id FROM users WHERE username = ?)""", (model, meeting_id, name))
    if "wrap_up" in payload or "wrap_up_by" in payload:
        if m["state"] != S.DRAFT:
            raise MeetingError(409, "invalid_transition", "who wraps up is decided before the meeting starts")
        names = [p["username"] for p in _participants(conn, meeting_id)]
        wrap_up, seat = _wrap_up_choice(payload, _username(conn, m["organizer_user_id"]), names, members,
                                        (bool(m["wrap_up"]), m["wrap_up_seat"]))
        conn.execute("UPDATE meetings SET wrap_up = ?, wrap_up_seat = ? WHERE id = ?", (int(wrap_up), seat, meeting_id))
    for name in payload.get("remove") or []:
        _leave(conn, actor, m, name, reason="removed")
    for name in payload.get("add") or []:
        _add_participant(conn, actor, m, name, members)
    if "rounds" in payload and not fixed:
        _set_rounds(conn, m, _bounded("rounds", payload["rounds"]))
    m = _meeting(conn, meeting_id)
    if m["state"] != S.DRAFT and not m["approved_by"] and not actor.is_admin and _agent_organized(conn, m):
        reasons = _small_meeting_violations(conn, m, count_today=False)
        if reasons:  # the whole edit is rolled back with the transaction
            raise MeetingError(409, "approval_required", {"reasons": reasons})
    _bump(conn, meeting_id)
    _event(conn, meeting_id, actor.id, "edited", fields=sorted(payload))
    return get_meeting(conn, actor, meeting_id)


def _next_round(conn, m: dict) -> int:
    slot = _next_open_slot(conn, m["id"])
    return (slot["round"] + 1) if slot and slot["round"] != S.WRAP_ROUND else m["rounds"] + 1


def _username(conn, user_id: int) -> str | None:
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else None


def _add_participant(conn, actor: Actor, m: dict, username: str, members: dict) -> None:
    u = conn.execute("SELECT * FROM users WHERE username = ? AND status = 'active'", (username,)).fetchone()
    if u is None or u["membership_kind"] not in ("crew", "system"):
        raise MeetingError(422, "out_of_range", {"participant": username, "reason": "not an active crew member"})
    bad = _unreadable(_material_paths(conn, m["id"]), [_actor_for_user(conn, u["id"])])
    if bad:
        raise _material_error(actor, m, bad)
    existing = _participant_for(conn, m["id"], u["id"])
    first = 1 if m["state"] == S.DRAFT else _next_round(conn, m)
    if existing and existing["status"] == "active":
        return
    if existing:  # re-add keeps the seat and reopens slots from the next round
        conn.execute("""UPDATE meeting_participants SET status = 'active', left_at = NULL, left_by = NULL,
                        joined_from_round = ? WHERE id = ?""", (first, existing["id"]))
        conn.execute("""UPDATE meeting_slots SET state = 'open', skip_reason = NULL, updated_at = ?
                        WHERE meeting_id = ? AND seat = ? AND state = 'skipped' AND round >= ?""",
                     (clock(), m["id"], existing["seat"], first))
        if existing["kind"] == "agent" and m["state"] != S.DRAFT:
            # engineer R2: someone removed in draft never got slots at start; earlier rounds and filled turns stay as they are
            _create_slots(conn, m["id"], existing["seat"], first, m["rounds"])
            _assign_roles(conn, m)
        conn.execute("DELETE FROM meeting_snapshots WHERE meeting_id = ? AND user_id = ?", (m["id"], u["id"]))
        _event(conn, m["id"], actor.id, "participant.readded", username=username, from_round=first)
        return
    agents = conn.execute("SELECT COUNT(*) FROM meeting_participants WHERE meeting_id = ? AND kind = 'agent' AND status = 'active'",
                          (m["id"],)).fetchone()[0]
    kind = "agent" if username in members else "human"
    if kind == "agent" and agents + 1 > S.AGENT_PARTICIPANTS[1]:
        raise MeetingError(422, "out_of_range", {"agent_participants": agents + 1})
    seat = conn.execute("SELECT COALESCE(MAX(seat), -1) + 1 FROM meeting_participants WHERE meeting_id = ?",
                        (m["id"],)).fetchone()[0]
    conn.execute("""INSERT INTO meeting_participants(meeting_id, user_id, kind, seat, status, joined_from_round, joined_at)
                    VALUES (?,?,?,?, 'active', ?, ?)""", (m["id"], u["id"], kind, seat, first, clock()))
    if kind == "agent" and m["state"] != S.DRAFT:
        _create_slots(conn, m["id"], seat, first, m["rounds"])
        _assign_roles(conn, m)
    _event(conn, m["id"], actor.id, "participant.added", username=username, from_round=first)


def _set_rounds(conn, m: dict, rounds: int) -> None:
    if rounds < m["rounds"]:
        filled = conn.execute("""SELECT 1 FROM meeting_slots WHERE meeting_id = ? AND round > ? AND round != ?
                                 AND state = 'filled'""", (m["id"], rounds, S.WRAP_ROUND)).fetchone()
        if filled:
            raise MeetingError(409, "invalid_transition", "cannot drop rounds that already have turns")
        conn.execute("DELETE FROM meeting_slots WHERE meeting_id = ? AND round > ? AND round != ?",
                     (m["id"], rounds, S.WRAP_ROUND))
    elif rounds > m["rounds"] and m["state"] != S.DRAFT:
        for p in _participants(conn, m["id"]):
            if p["kind"] == "agent" and p["status"] == "active":
                _create_slots(conn, m["id"], p["seat"], max(m["rounds"] + 1, p["joined_from_round"]), rounds)
    conn.execute("UPDATE meetings SET rounds = ? WHERE id = ?", (rounds, m["id"]))
    m["rounds"] = rounds


# ---------------------------------------------------------------- meetings an agent organizes (section 4)

def _is_person(conn, user_id: int) -> bool:
    """People come from the administrator's explicit list, never from who happens to lack an adapter
    (R1, engineer review): an unknown account counts as an agent. Without a list only administrators are people."""
    u = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if u is None:
        return False
    people = load_agent_meeting_policy().people
    return u["username"] in people if people else u["role"] == "admin"


def _agent_organized(conn, m: dict) -> bool:
    return not _is_person(conn, m["organizer_user_id"])


def _local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _small_meeting_violations(conn, m: dict, count_today: bool = True) -> list[str]:
    """Why an unapproved agent-organized meeting may not run on its own; empty means it may."""
    members, policy = load_members(), load_agent_meeting_policy()
    if not policy.auto_start_models:
        return ["agent_meetings_not_enabled"]
    reasons = []
    present = [p for p in _participants(conn, m["id"]) if p["status"] == "active"]
    # R1b: every agent present counts, speaking or not (an agent with no adapter only listens);
    # the model check is for the seats that will actually call a model
    if sum(1 for p in present if not _is_person(conn, p["user_id"])) > policy.max_agents:
        reasons.append(f"agents>{policy.max_agents}")
    agents = [p for p in present if p["kind"] == "agent"]
    if m["rounds"] > policy.max_rounds:
        reasons.append(f"rounds>{policy.max_rounds}")
    if m["meeting_timeout_s"] > policy.max_meeting_timeout_s:
        reasons.append(f"meeting_timeout_s>{policy.max_meeting_timeout_s}")
    for p in agents:
        member = members.get(p["username"])
        model = p["model"] or (member.default_model if member else None)
        if model not in policy.auto_start_models:
            reasons.append(f"model:{p['username']}={model}")
    if count_today:
        started = conn.execute("""SELECT organizer_user_id FROM meetings
                                  WHERE approved_by IS NULL AND started_at >= ? AND id != ?""",
                               (_local_midnight(clock()), m["id"])).fetchall()
        if sum(1 for r in started if not _is_person(conn, r["organizer_user_id"])) >= policy.per_day:
            reasons.append(f"per_day>{policy.per_day}")
    return reasons


def _approval_view(conn, m: dict) -> dict | None:
    if not _agent_organized(conn, m):
        return None
    if m["approved_by"] or m["state"] != S.DRAFT:
        return {"needed": False, "reasons": []}
    reasons = _small_meeting_violations(conn, m)
    return {"needed": bool(reasons), "reasons": reasons}


def _auto_models(conn, m: dict) -> list[str] | None:
    """Models an unapproved agent-organized meeting may use, fallbacks included; None means no such limit."""
    if m["approved_by"] or not _agent_organized(conn, m):
        return None
    return list(load_agent_meeting_policy().auto_start_models)


# ---------------------------------------------------------------- lifecycle

def approve(conn, actor: Actor, meeting_id: str) -> dict:
    m = _meeting(conn, meeting_id)
    if not actor.is_admin:
        raise MeetingError(403, "forbidden", "administrators approve meetings")
    conn.execute("UPDATE meetings SET approved_by = ? WHERE id = ?", (actor.id, meeting_id))
    _bump(conn, meeting_id)
    _event(conn, meeting_id, actor.id, "approved")
    return get_meeting(conn, actor, meeting_id)


REJECT_REASON_MAX = 500


def reject(conn, actor: Actor, meeting_id: str, reason: Any) -> dict:
    """Section 4: an administrator refuses a draft that waits for approval, and says why."""
    m = _meeting(conn, meeting_id)
    if not actor.is_admin:
        raise MeetingError(403, "forbidden", "administrators approve or reject meetings")
    text = S.normalize(reason) if isinstance(reason, str) else ""
    if not text:
        raise MeetingError(400, "invalid_request", "reason is required")
    if len(text) > REJECT_REASON_MAX:
        raise MeetingError(422, "out_of_range", {"reason": len(text), "range": [1, REJECT_REASON_MAX]})
    if m["state"] != S.DRAFT or m["approved_by"] or not _agent_organized(conn, m):
        raise MeetingError(409, "invalid_transition", "only a draft that waits for approval can be rejected")
    conn.execute("UPDATE meetings SET decision_reason = ? WHERE id = ?", (text, meeting_id))
    _event(conn, meeting_id, actor.id, "rejected", reason=text)
    _set_state(conn, m, S.CLOSED, "rejected", actor.id)
    return get_meeting(conn, actor, meeting_id)


def _decision(conn, m: dict) -> dict | None:
    if m["exit_reason"] != "rejected":
        return None
    row = conn.execute("""SELECT u.display_name, u.username FROM meeting_events e JOIN users u ON u.id = e.actor_user_id
                          WHERE e.meeting_id = ? AND e.kind = 'rejected' ORDER BY e.id DESC LIMIT 1""", (m["id"],)).fetchone()
    return {"rejected": True, "reason": m["decision_reason"], "by": (row["display_name"] or row["username"]) if row else None}


def start(conn, actor: Actor, meeting_id: str) -> dict:
    m = _meeting(conn, meeting_id)
    _require_organizer(actor, m)
    if m["state"] != S.DRAFT:
        raise MeetingError(409, "invalid_transition", {"from": m["state"], "to": S.RUNNING})
    if S.approval_required() and not m["approved_by"]:
        raise MeetingError(409, "approval_required")
    if not m["approved_by"] and _agent_organized(conn, m):
        reasons = _small_meeting_violations(conn, m)
        if reasons:
            raise MeetingError(409, "approval_required", {"reasons": reasons})
    agents = [p for p in _participants(conn, meeting_id) if p["kind"] == "agent" and p["status"] == "active"]
    if len(agents) < S.AGENT_PARTICIPANTS[0]:
        raise MeetingError(422, "out_of_range", {"agent_participants": len(agents)})
    for p in agents:
        _create_slots(conn, meeting_id, p["seat"], p["joined_from_round"], m["rounds"])
    _assign_roles(conn, m)
    if M.mode_rounds(m["mode"]) and not m["wrap_up"]:
        raise MeetingError(422, "out_of_range", {"wrap_up": False, "reason": "this mode ends with the chair's wrap-up"})
    if m["wrap_up"]:
        if m["wrap_up_seat"] is None:
            raise MeetingError(422, "out_of_range", {"wrap_up_by": None, "reason": "choose who wraps up, or turn wrap-up off"})
        if m["wrap_up_seat"] not in {p["seat"] for p in agents}:
            raise MeetingError(422, "out_of_range", {"wrap_up_by": None, "reason": "the wrap-up speaker is not here"})
        _create_slots(conn, meeting_id, m["wrap_up_seat"], S.WRAP_ROUND, S.WRAP_ROUND)
    _set_state(conn, m, S.RUNNING, None, actor.id)
    enqueue_next(conn, meeting_id)
    return get_meeting(conn, actor, meeting_id)


def pause(conn, actor: Actor, meeting_id: str) -> dict:
    m = _meeting(conn, meeting_id)
    _require_organizer(actor, m)
    _pause(conn, m, "user_pause", "user_pause", actor.id)
    return get_meeting(conn, actor, meeting_id)


def _pause(conn, m: dict, exit_reason: str, cancel_reason: str, actor_id: int | None) -> None:
    _cancel_attempts(conn, m["id"], cancel_reason)
    _set_state(conn, m, S.PAUSED, exit_reason, actor_id)


def resume(conn, actor: Actor, meeting_id: str) -> dict:
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    _require_organizer(actor, m)
    if m["state"] not in S.STOPPED:
        raise MeetingError(409, "invalid_transition", {"from": m["state"], "to": S.RUNNING})
    states = {r["state"] for r in conn.execute(
        "SELECT state FROM meeting_attempts WHERE meeting_id = ? AND state IN ('cancel_requested','quarantined')", (meeting_id,))}
    if S.QUARANTINED in states:
        raise MeetingError(409, "quarantined")
    if S.CANCEL_REQUESTED in states:
        raise MeetingError(409, "draining")
    if m["run_seconds"] >= m["meeting_timeout_s"]:
        raise MeetingError(409, "invalid_transition", "time budget used up; raise meeting_timeout_s first")
    if not m["approved_by"] and _agent_organized(conn, m):  # R2: today's policy, not the one it started under
        reasons = _small_meeting_violations(conn, m, count_today=False)  # already counted on the day it started
        if reasons:
            raise MeetingError(409, "approval_required", {"reasons": reasons})
    _set_state(conn, m, S.RUNNING, None, actor.id)
    enqueue_next(conn, meeting_id)
    return get_meeting(conn, actor, meeting_id)


def skip_turn(conn, actor: Actor, meeting_id: str, username: Any) -> dict:
    """Skip only the turn that would run next; its speaker keeps the later rounds (product requirement).
    For a seat whose adapter failed: the meeting goes on without removing anyone. The organizer names
    whose turn they mean, so a stale page cannot skip somebody else's."""
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    _require_organizer(actor, m)
    if m["state"] not in S.STOPPED:
        raise MeetingError(409, "invalid_transition", "pause the meeting before skipping a turn")
    slot = _next_open_slot(conn, meeting_id)
    if slot is None:
        raise MeetingError(409, "invalid_transition", "no turn left to skip")
    p = conn.execute("""SELECT u.username FROM meeting_participants p JOIN users u ON u.id = p.user_id
                        WHERE p.meeting_id = ? AND p.seat = ?""", (meeting_id, slot["seat"])).fetchone()
    if p["username"] != username:
        raise MeetingError(409, "not_next", {"next": p["username"], "round": slot["round"]})
    if conn.execute(f"""SELECT 1 FROM meeting_attempts WHERE meeting_id = ? AND round = ? AND seat = ?
                        AND state IN {S.NON_TERMINAL}""", (meeting_id, slot["round"], slot["seat"])).fetchone():
        raise MeetingError(409, "draining")
    conn.execute("""UPDATE meeting_slots SET state = 'skipped', skip_reason = 'skipped_by_organizer', updated_at = ?
                    WHERE meeting_id = ? AND round = ? AND seat = ? AND state = 'open'""",
                 (clock(), meeting_id, slot["round"], slot["seat"]))
    _event(conn, meeting_id, actor.id, "turn.skipped", username=username, round=slot["round"])
    _rebuild_minutes(conn, meeting_id)
    _bump(conn, meeting_id)
    return get_meeting(conn, actor, meeting_id)


DELETABLE = frozenset({S.DRAFT, S.COMPLETED, S.CLOSED})


def delete_meeting(conn, actor: Actor, meeting_id: str) -> dict:
    """Hide a draft or finished meeting from every list and view (product requirement: failed trial meetings pile up).
    Nothing is erased: the meeting, its turns and minutes stay in the database and can be restored there.
    A meeting that may still run is closed first, so deleting never stops anyone mid-turn."""
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    _require_organizer(actor, m)
    if m["state"] not in DELETABLE:
        raise MeetingError(409, "invalid_transition", "close the meeting before deleting it")
    conn.execute("UPDATE meetings SET deleted_at = ?, deleted_by = ? WHERE id = ?", (clock(), actor.id, meeting_id))
    _event(conn, meeting_id, actor.id, "deleted")
    _bump(conn, meeting_id)
    return {"meeting_id": meeting_id, "status": "deleted"}


def close(conn, actor: Actor, meeting_id: str) -> dict:
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    if m["state"] in S.TERMINAL_MEETING:
        raise MeetingError(409, "invalid_transition", {"from": m["state"], "to": S.CLOSED})
    if _is_organizer_or_admin(actor, m):
        reason = "user_close"
    else:
        p = _participant_for(conn, meeting_id, actor.id)
        if not (m["state"] == S.DORMANT and p and p["status"] == "active"):
            raise MeetingError(403, "forbidden", "only the organizer closes a meeting that is not dormant")
        reason = "participant_close_dormant"
    _cancel_attempts(conn, meeting_id, "user_close")
    _set_state(conn, m, S.CLOSED, reason, actor.id)
    return get_meeting(conn, actor, meeting_id)


def leave(conn, actor: Actor, meeting_id: str) -> dict:
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    _require_crew(actor)
    p = _participant_for(conn, meeting_id, actor.id)
    if p is None:
        raise MeetingError(404, "not_found")
    if actor.id == m["organizer_user_id"]:
        raise MeetingError(409, "invalid_transition", "the organizer closes the meeting instead of leaving")
    if p["status"] == "active":
        _leave(conn, actor, m, actor.username, reason="left")
        _bump(conn, meeting_id)
    return {"meeting_id": meeting_id, "status": "left"}


def _leave(conn, actor: Actor, m: dict, username: str, reason: str) -> None:
    u = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    p = _participant_for(conn, m["id"], u["id"]) if u else None
    if p is None:
        raise MeetingError(404, "not_found", {"participant": username})
    if p["status"] == "left":
        return
    if p["user_id"] == m["organizer_user_id"]:
        raise MeetingError(409, "invalid_transition", "the organizer cannot be removed")
    now = clock()
    conn.execute("UPDATE meeting_participants SET status = 'left', left_at = ?, left_by = ? WHERE id = ?",
                 (now, actor.id, p["id"]))
    conn.execute("""UPDATE meeting_slots SET state = 'skipped', skip_reason = ?, updated_at = ?
                    WHERE meeting_id = ? AND seat = ? AND state = 'open'""", (reason, now, m["id"], p["seat"]))
    _event(conn, m["id"], actor.id, f"participant.{reason}", username=username)
    _cancel_attempts(conn, m["id"], reason, seat=p["seat"])
    m = _meeting(conn, m["id"])
    if m["state"] == S.RUNNING:
        active_agents = conn.execute("""SELECT COUNT(*) FROM meeting_participants
                                        WHERE meeting_id = ? AND kind = 'agent' AND status = 'active'""",
                                     (m["id"],)).fetchone()[0]
        if active_agents == 0:
            _pause(conn, m, "no_active_agents", "shutdown", None)
        else:
            enqueue_next(conn, m["id"])
    _take_snapshot(conn, m["id"], p["user_id"])


# ---------------------------------------------------------------- interjections (section 6.1)

INTERJECTION_MAX = 2000


def _interjections(conn, meeting_id: str) -> list[dict]:
    return _rows(conn.execute("""SELECT i.*, u.username, u.display_name FROM meeting_interjections i
                                 JOIN users u ON u.id = i.author_user_id
                                 WHERE i.meeting_id = ? ORDER BY i.created_at, i.id""", (meeting_id,)))


def _transcript(turns: list[dict], parts: dict, said: list[dict]) -> list[dict]:
    """Accepted turns in slot order with interjections placed by time: a turn counts from its ended_at."""
    out, pending = [], list(said)
    for t in turns:
        while pending and pending[0]["created_at"] < (t["ended_at"] or 0):
            i = pending.pop(0)
            out.append({"round": i["round"], "speaker": i["display_name"] or i["username"], "text": i["text"], "human": True})
        out.append({"round": t["round"], "speaker": parts[t["seat"]]["display_name"], "text": t["text"]})
    out += [{"round": i["round"], "speaker": i["display_name"] or i["username"], "text": i["text"], "human": True}
            for i in pending]
    return out


def interject(conn, actor: Actor, meeting_id: str, text: Any) -> dict:
    m = _apply_dormancy(conn, _meeting(conn, meeting_id))
    view = _view(conn, actor, m)
    if view is None:
        raise MeetingError(404, "not_found")
    p = _participant_for(conn, meeting_id, actor.id)
    if view[0] != "full" or not _is_person(conn, actor.id) or (p is not None and p["status"] != "active"):
        raise MeetingError(403, "forbidden", "only people interject; agents speak in their turns")
    if m["state"] not in (S.RUNNING, S.PAUSED, S.DORMANT):
        raise MeetingError(409, "invalid_transition", {"from": m["state"], "action": "interject"})
    body = S.normalize(text) if isinstance(text, str) else ""
    if not body:
        raise MeetingError(400, "invalid_request", "text is required")
    if len(body) > INTERJECTION_MAX:
        raise MeetingError(422, "out_of_range", {"text": len(body), "range": [1, INTERJECTION_MAX]})
    slot = _next_open_slot(conn, meeting_id)
    interjection_id = "int_" + uuid.uuid4().hex[:16]
    conn.execute("""INSERT INTO meeting_interjections(id, meeting_id, author_user_id, round, text, created_at)
                    VALUES (?,?,?,?,?,?)""",
                 (interjection_id, meeting_id, actor.id,
                  slot["round"] if slot and slot["round"] != S.WRAP_ROUND else m["rounds"], body, clock()))
    _bump(conn, meeting_id)
    _event(conn, meeting_id, actor.id, "interjection", interjection_id=interjection_id, chars=len(body))
    if m["state"] != S.RUNNING:
        _rebuild_minutes(conn, meeting_id)
    return get_meeting(conn, actor, meeting_id)


# ---------------------------------------------------------------- export (section 9)

def export_minutes(conn, actor: Actor, meeting_id: str, revision: int, board_slug: str) -> dict:
    from app.services.markdown import render_markdown
    m = _meeting(conn, meeting_id)
    _require_organizer(actor, m)
    art = conn.execute("SELECT * FROM meeting_artifacts WHERE meeting_id = ? AND kind = 'minutes' AND revision = ?",
                       (meeting_id, revision)).fetchone()
    if art is None:
        raise MeetingError(404, "not_found", {"revision": revision})
    done = conn.execute("SELECT * FROM meeting_exports WHERE artifact_id = ?", (art["id"],)).fetchone()
    if done:
        return {"thread_id": done["thread_id"], "board": done["board"], "revision": revision, "existing": True}
    board = conn.execute("SELECT * FROM boards WHERE slug = ? AND is_archived = 0", (board_slug,)).fetchone()
    if board is None:
        raise MeetingError(404, "not_found", {"board": board_slug})
    if board_slug in PUBLIC_READ:
        raise MeetingError(409, "export_scope", "public boards are not allowed in v0.2")
    readers = [_actor_for_user(conn, r["id"]) for r in conn.execute(
        "SELECT id FROM users WHERE status = 'active' AND membership_kind IN ('crew','system')")]
    bad = _unreadable(_material_paths(conn, meeting_id), readers)
    if bad:
        raise MeetingError(409, "export_scope", {"paths": bad})
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    cur = conn.execute("""INSERT INTO threads(board_id, title, author_user_id, created_by_user_id, created_at, updated_at)
                          VALUES (?,?,?,?,?,?)""", (board["id"], f"会议纪要：{m['title']}", actor.id, actor.id, now_iso, now_iso))
    thread_id = int(cur.lastrowid)
    pcur = conn.execute("""INSERT INTO posts(thread_id, author_user_id, created_by_user_id, body_markdown, body_html, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (thread_id, actor.id, actor.id, art["body_md"], render_markdown(art["body_md"], allow_media=False), now_iso, now_iso))
    conn.execute("""INSERT INTO meeting_exports(artifact_id, meeting_id, board, thread_id, post_id, created_by, created_at)
                    VALUES (?,?,?,?,?,?,?)""", (art["id"], meeting_id, board_slug, thread_id, int(pcur.lastrowid), actor.id, clock()))
    _event(conn, meeting_id, actor.id, "exported", revision=revision, board=board_slug, thread_id=thread_id)
    return {"thread_id": thread_id, "board": board_slug, "revision": revision, "existing": False}


def release_quarantine(conn, actor: Actor, attempt_id: str) -> dict:
    if not actor.is_admin:
        raise MeetingError(403, "forbidden")
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if a is None or a["state"] != S.QUARANTINED:
        raise MeetingError(409, "invalid_transition", "only quarantined attempts can be released")
    conn.execute("UPDATE meeting_attempts SET state = 'interrupted', ended_at = ?, error_code = COALESCE(error_code, 'quarantine_released') WHERE id = ?",
                 (clock(), attempt_id))
    _bump(conn, a["meeting_id"])
    _event(conn, a["meeting_id"], actor.id, "quarantine.released", attempt_id=attempt_id)
    _rebuild_minutes(conn, a["meeting_id"])
    return {"attempt_id": attempt_id, "state": "interrupted"}


# ---------------------------------------------------------------- runner calls (section 12)

def owner_valid(conn, owner: str, generation: int) -> bool:
    row = conn.execute("SELECT owner_id, generation, lease_until FROM meeting_scheduler WHERE id = 1").fetchone()
    return bool(row and row["owner_id"] == owner and row["generation"] == generation and row["lease_until"] > clock())


def acquire_lease(conn, owner: str, lease_s: float) -> int | None:
    row = conn.execute("SELECT * FROM meeting_scheduler WHERE id = 1").fetchone()
    now = clock()
    if row is None:
        conn.execute("INSERT INTO meeting_scheduler(id, owner_id, generation, lease_until, renewed_at) VALUES (1,?,1,?,?)",
                     (owner, now + lease_s, now))
        return 1
    if row["owner_id"] == owner and row["lease_until"] > now:
        return row["generation"]
    if row["lease_until"] > now:
        return None
    gen = row["generation"] + 1
    conn.execute("UPDATE meeting_scheduler SET owner_id = ?, generation = ?, lease_until = ?, renewed_at = ? WHERE id = 1",
                 (owner, gen, now + lease_s, now))
    return gen


def renew_lease(conn, owner: str, generation: int, lease_s: float) -> bool:
    now = clock()
    cur = conn.execute("""UPDATE meeting_scheduler SET lease_until = ?, renewed_at = ?
                          WHERE id = 1 AND owner_id = ? AND generation = ? AND lease_until > ?""",
                       (now + lease_s, now, owner, generation, now))
    return cur.rowcount == 1


def claim(conn, owner: str, generation: int) -> dict | None:
    if not owner_valid(conn, owner, generation):
        return None
    holding = conn.execute(f"SELECT COUNT(*) FROM meeting_attempts WHERE state IN {S.SLOT_HOLDING}").fetchone()[0]
    if holding >= S.global_slots():
        return None
    candidates = _rows(conn.execute("""SELECT a.* FROM meeting_attempts a JOIN meetings m ON m.id = a.meeting_id
                                       WHERE a.state = 'queued' AND m.state = 'running' ORDER BY a.queued_at"""))
    for a in candidates:
        if not check_materials(conn, a["id"], owner, generation):
            continue
        try:
            conn.execute("SAVEPOINT claim_one")
            conn.execute("UPDATE meeting_attempts SET state = 'claimed', generation = ?, claimed_at = ? WHERE id = ? AND state = 'queued'",
                         (generation, clock(), a["id"]))
            conn.execute("RELEASE claim_one")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO claim_one")
            conn.execute("RELEASE claim_one")
            continue
        return _attempt_context(conn, a["id"])
    return None


def _attempt_context(conn, attempt_id: str) -> dict:
    a = dict(conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone())
    m = _meeting(conn, a["meeting_id"])
    parts = {p["seat"]: p for p in _participants(conn, m["id"])}
    turns = _rows(conn.execute("""SELECT a.round, a.seat, a.text, a.ended_at FROM meeting_slots s
                                  JOIN meeting_attempts a ON a.id = s.filled_attempt_id
                                  WHERE s.meeting_id = ? AND s.state = 'filled' ORDER BY s.round, s.seat""", (m["id"],)))
    speaker = parts[a["seat"]]
    stage = M.stage(m["mode"], a["round"])
    if stage and stage["blind"]:  # section 5.1: nobody sees another agent's turn of a blind round
        turns = [t for t in turns if t["round"] < a["round"]]
    materials = []
    for path in _material_paths(conn, m["id"]):
        item = _read_material(path)
        if item:
            materials.append({"path": path, "body": item[2]})
    return {
        "attempt": a,
        "meeting": {k: m[k] for k in ("id", "title", "topic", "rounds", "max_chars", "turn_timeout_s", "mode")},
        "stage": stage,
        "perspective": M.perspective(speaker.get("role")),
        "auto_models": _auto_models(conn, m),
        "speaker": {"username": speaker["username"], "display_name": speaker["display_name"], "seat": a["seat"]},
        "exempt": m["moderator_seat"] == a["seat"] and a["round"] == 1,
        "wrap_up": a["round"] == S.WRAP_ROUND,
        "model": speaker.get("model"),
        "turns": _transcript(turns, parts, _interjections(conn, m["id"])),
        "materials": materials,
    }


def mark_running(conn, attempt_id: str, owner: str, generation: int, identity: dict) -> bool:
    if not owner_valid(conn, owner, generation):
        return False
    external = identity.get("external")
    cur = conn.execute("""UPDATE meeting_attempts SET state = 'running', started_at = ?, exec_host = ?, exec_pid = ?,
                          exec_pgid = ?, exec_started_at = ?, exec_external = ?
                          WHERE id = ? AND state = 'claimed' AND generation = ?""",
                       (clock(), identity.get("host"), identity.get("pid"), identity.get("pgid"),
                        identity.get("started"), json.dumps(external, sort_keys=True) if external is not None else None,
                        attempt_id, generation))
    return cur.rowcount == 1


def fail_unlaunched(conn, attempt_id: str, owner: str, generation: int, error_code: str, detail: str = "") -> bool:
    """A claimed attempt whose launcher never reached the barrier (its group is already gone)."""
    if not owner_valid(conn, owner, generation):
        return False
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if a is None or a["state"] != S.CLAIMED or a["generation"] != generation:
        return False
    conn.execute("UPDATE meeting_attempts SET state = 'failed', ended_at = ?, error_code = ?, error_detail = ? WHERE id = ?",
                 (clock(), error_code, detail[:2000], attempt_id))
    _add_run_time(conn, dict(a))
    _event(conn, a["meeting_id"], None, "turn.failed", attempt_id=attempt_id, error_code=error_code)
    m = _meeting(conn, a["meeting_id"])
    if m["state"] == S.RUNNING:
        _pause(conn, m, "adapter_unavailable", "shutdown", None)
    else:
        _rebuild_minutes(conn, m["id"])
    return True


def _route_fields(outcome: dict) -> tuple:
    requested, served = outcome.get("requested_model"), outcome.get("model")
    fallback = None if not (requested and served) else int(requested != served)
    if isinstance(outcome.get("fallback_used"), bool) and fallback is not None:
        fallback = int(outcome["fallback_used"])  # the adapter matched a reported alias for this exact request
    surface = outcome.get("tool_surface")
    rev = outcome.get("adapter_rev")
    return (requested, fallback, json.dumps(surface, ensure_ascii=False) if surface is not None else None,
            json.dumps(rev, sort_keys=True) if isinstance(rev, dict) else rev)


def _group_ref(a: dict) -> dict | None:
    """Which process group an event is about, so a termination can be traced afterwards."""
    if not a.get("exec_pgid"):
        return None
    return {"host": a.get("exec_host"), "pgid": a["exec_pgid"], "pid": a.get("exec_pid"),
            "started": a.get("exec_started_at")}


def _add_run_time(conn, a: dict) -> None:
    started = a["claimed_at"] or a["queued_at"]
    conn.execute("UPDATE meetings SET run_seconds = run_seconds + ? WHERE id = ?",
                 (max(0.0, clock() - started), a["meeting_id"]))


def record_turn_result(conn, attempt_id: str, owner: str, generation: int, outcome: dict) -> str:
    """Section 7.4. The runner calls this only after the whole process group is gone."""
    if not owner_valid(conn, owner, generation):
        return "discarded"  # a stale owner writes nothing at all
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    m = _meeting(conn, a["meeting_id"]) if a else None
    if a is None or a["state"] != S.ATTEMPT_RUNNING or a["generation"] != generation or m["state"] != S.RUNNING:
        if a is not None:
            _event(conn, a["meeting_id"], None, "late_result_discarded", attempt_id=attempt_id)
        return "discarded"
    a = dict(a)
    now = clock()
    _add_run_time(conn, a)
    slot = conn.execute("SELECT * FROM meeting_slots WHERE meeting_id = ? AND round = ? AND seat = ?",
                        (a["meeting_id"], a["round"], a["seat"])).fetchone()
    if outcome.get("status") == "accepted":
        wrap = a["round"] == S.WRAP_ROUND
        limited = S.apply_char_limit(outcome.get("text") or "", S.WRAP_MAX_CHARS if wrap else m["max_chars"],
                                     exempt=(not wrap and m["moderator_seat"] == a["seat"] and a["round"] == 1))
        if not limited["text"]:
            outcome = {"status": "failed", "error_code": "empty"}
        else:
            conn.execute("""UPDATE meeting_attempts SET state = 'accepted', ended_at = ?, raw_text = ?, text = ?,
                            truncated = ?, char_count = ?, provider = ?, model = ?, provider_session_id = ?,
                            requested_model = ?, fallback_used = ?, tool_surface = ?, adapter_rev = ? WHERE id = ?""",
                         (now, limited["raw_text"], limited["text"], int(limited["truncated"]), limited["char_count"],
                          outcome.get("provider"), outcome.get("model"), outcome.get("provider_session_id"),
                          *_route_fields(outcome), attempt_id))
            if slot and slot["state"] == S.SLOT_OPEN:
                conn.execute("""UPDATE meeting_slots SET state = 'filled', filled_attempt_id = ?, updated_at = ?
                                WHERE meeting_id = ? AND round = ? AND seat = ?""",
                             (attempt_id, now, a["meeting_id"], a["round"], a["seat"]))
            _bump(conn, a["meeting_id"])
            _event(conn, a["meeting_id"], None, "turn.accepted", attempt_id=attempt_id, truncated=limited["truncated"])
            m = _meeting(conn, a["meeting_id"])
            if m["run_seconds"] >= m["meeting_timeout_s"]:
                _pause(conn, m, "time_budget", "time_budget", None)
            else:
                enqueue_next(conn, a["meeting_id"])
            return "applied"
    conn.execute("""UPDATE meeting_attempts SET state = 'failed', ended_at = ?, error_code = ?, error_detail = ?,
                    raw_text = ?, provider = ?, model = ?, requested_model = ?, fallback_used = ?, tool_surface = ?,
                    adapter_rev = ? WHERE id = ?""",
                 (now, outcome.get("error_code") or "failed", (outcome.get("detail") or "")[:2000],
                  outcome.get("text"), outcome.get("provider"), outcome.get("model"), *_route_fields(outcome), attempt_id))
    _event(conn, a["meeting_id"], None, "turn.failed", attempt_id=attempt_id, error_code=outcome.get("error_code"))
    _pause(conn, m, "turn_failed", "shutdown", None)  # never auto-enqueue a retry (7.4, 7.5)
    return "applied"


def confirm_terminated(conn, attempt_id: str, owner: str, generation: int) -> str:
    if not owner_valid(conn, owner, generation):
        return "discarded"
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if a is None or a["generation"] != generation:
        return "discarded"
    if a["state"] == S.CANCELLED:
        return "noop"
    if a["state"] != S.CANCEL_REQUESTED:
        return "discarded"
    a = dict(a)
    conn.execute("UPDATE meeting_attempts SET state = 'cancelled', ended_at = ?, error_code = COALESCE(error_code, ?) WHERE id = ?",
                 (clock(), "timeout" if a["cancel_reason"] == "timeout" else None, attempt_id))
    _add_run_time(conn, a)
    _event(conn, a["meeting_id"], None, "attempt.cancelled", attempt_id=attempt_id, reason=a["cancel_reason"],
           process_group_ref=_group_ref(a), termination="group_gone_confirmed")
    m = _meeting(conn, a["meeting_id"])
    if m["state"] == S.RUNNING:
        enqueue_next(conn, m["id"])
    else:
        _rebuild_minutes(conn, m["id"])
    return "applied"


def quarantine(conn, attempt_id: str, owner: str, generation: int, detail: str) -> bool:
    if not owner_valid(conn, owner, generation):
        return False
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if a is None or a["state"] not in (S.CLAIMED, S.ATTEMPT_RUNNING, S.CANCEL_REQUESTED):
        return False
    conn.execute("UPDATE meeting_attempts SET state = 'quarantined', error_code = 'quarantined', error_detail = ? WHERE id = ?",
                 (detail[:2000], attempt_id))
    _event(conn, a["meeting_id"], None, "attempt.quarantined", attempt_id=attempt_id, detail=detail[:200],
           process_group_ref=_group_ref(dict(a)), termination="unconfirmed")
    m = _meeting(conn, a["meeting_id"])
    if m["state"] == S.RUNNING:
        _set_state(conn, m, S.PAUSED, "interrupted_restart", None)
    else:
        _rebuild_minutes(conn, m["id"])
    return True


def pending_cancels(conn, owner: str, generation: int) -> list[str]:
    if not owner_valid(conn, owner, generation):
        return []
    return [r["id"] for r in conn.execute(
        "SELECT id FROM meeting_attempts WHERE state = 'cancel_requested' AND generation = ?", (generation,))]


def check_budgets(conn, owner: str, generation: int) -> None:
    if not owner_valid(conn, owner, generation):
        return
    now = clock()
    for a in _rows(conn.execute("""SELECT a.*, m.turn_timeout_s, m.meeting_timeout_s, m.run_seconds FROM meeting_attempts a
                                   JOIN meetings m ON m.id = a.meeting_id
                                   WHERE a.state IN ('claimed','running') AND m.state = 'running'""")):
        elapsed = now - (a["claimed_at"] or now)
        m = _meeting(conn, a["meeting_id"])
        if elapsed > a["turn_timeout_s"]:
            _pause(conn, m, "turn_timeout", "timeout", None)
        elif a["run_seconds"] + elapsed > a["meeting_timeout_s"]:
            _pause(conn, m, "time_budget", "time_budget", None)


def check_materials(conn, attempt_id: str, owner: str, generation: int) -> bool:
    if not owner_valid(conn, owner, generation):
        return False
    a = conn.execute("SELECT * FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
    m = _meeting(conn, a["meeting_id"])
    revoked = conn.execute("""
        SELECT 1 FROM meeting_participants p JOIN users u ON u.id = p.user_id
        WHERE p.meeting_id = ? AND p.status = 'active'
          AND (u.status != 'active' OR COALESCE(u.membership_kind, '') NOT IN ('crew', 'system'))
        LIMIT 1""", (m["id"],)).fetchone()
    if revoked:
        _pause(conn, m, "participant_revoked", "participant_revoked", None)
        return False
    if _unreadable(_material_paths(conn, m["id"]), _active_actors(conn, m["id"])):
        _pause(conn, m, "material_acl_changed", "material_acl_changed", None)
        return False
    # R2b: the last check before a launch, so a turn queued under an older policy never calls a model
    if not m["approved_by"] and _agent_organized(conn, m) and _small_meeting_violations(conn, m, count_today=False):
        _pause(conn, m, "agent_policy_changed", "agent_policy_changed", None)
        return False
    return True


def recovery_candidates(conn, owner: str, generation: int) -> list[dict]:
    """Attempts an older generation left holding slots (7.1). Read in a short transaction."""
    if not owner_valid(conn, owner, generation):
        return []
    return _rows(conn.execute("""SELECT * FROM meeting_attempts WHERE state IN ('claimed','running','cancel_requested')
                                 AND (generation IS NULL OR generation < ?)""", (generation,)))


def apply_recovery(conn, owner: str, generation: int, attempt_id: str, verdict: str) -> bool:
    """Write one probe verdict. Re-checks the full owner condition: a probe can take long."""
    if not owner_valid(conn, owner, generation):
        return False
    a = conn.execute("""SELECT * FROM meeting_attempts WHERE id = ? AND state IN ('claimed','running','cancel_requested')
                        AND (generation IS NULL OR generation < ?)""", (attempt_id, generation)).fetchone()
    if a is None:
        return True  # already handled
    a = dict(a)
    if verdict in ("gone", "killed"):
        conn.execute("UPDATE meeting_attempts SET state = 'interrupted', ended_at = ?, error_code = 'interrupted_restart' WHERE id = ?",
                     (clock(), a["id"]))
        _add_run_time(conn, a)
        _event(conn, a["meeting_id"], None, "attempt.interrupted", attempt_id=a["id"], verdict=verdict,
               process_group_ref=_group_ref(a), termination=verdict)
    else:
        conn.execute("UPDATE meeting_attempts SET state = 'quarantined', error_code = 'quarantined', error_detail = ? WHERE id = ?",
                     ("recovery could not prove the process group gone", a["id"]))
        _event(conn, a["meeting_id"], None, "attempt.quarantined", attempt_id=a["id"],
               process_group_ref=_group_ref(a), termination="unconfirmed")
    m = _meeting(conn, a["meeting_id"])
    if m["state"] == S.RUNNING:
        _set_state(conn, m, S.PAUSED, "interrupted_restart", None)
    else:
        _rebuild_minutes(conn, m["id"])
    return True


def recover(conn, owner: str, generation: int, probe: Callable[[dict], str]) -> int:
    """Section 7.1, single-connection form. The runner uses the two calls above instead,
    so probes never run inside a write transaction.

    Launch barrier: the launcher only execs after the runner tells it to, and the runner
    does that only after mark_running committed the identity and its lease is still good.
    So an attempt with no recorded pgid can never have exec'd the adapter, and a waiting
    launcher exits when its parent's pipe closes.
    """
    count = 0
    for a in recovery_candidates(conn, owner, generation):
        verdict = "gone" if a["exec_pgid"] is None else probe(a)
        if not apply_recovery(conn, owner, generation, a["id"], verdict):
            break  # lease lost during the probe: leave the evidence for the next owner
        count += 1
    return count


def sweep(conn, owner: str, generation: int) -> None:
    if not owner_valid(conn, owner, generation):
        return
    for m in _rows(conn.execute("SELECT * FROM meetings WHERE state = 'paused'")):
        _apply_dormancy(conn, m)
    conn.execute("DELETE FROM meeting_requests WHERE created_at < ?", (clock() - 7 * 86_400,))
