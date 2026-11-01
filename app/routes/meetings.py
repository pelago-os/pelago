"""HTTP layer for meetings v0.2 (contract section 11). Every call goes through the service."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import Actor, require_actor
from app.database import get_connection
from app.meetings import service
from app.meetings.adapter_config import AdapterConfigError, load_agent_meeting_policy, load_members

def require_meetings_enabled():
    from app.meetings.settings import enabled
    if not enabled():
        raise HTTPException(404, "Meetings are disabled")


router = APIRouter(prefix="/api", dependencies=[Depends(require_meetings_enabled)])


def _run(fn: Callable[[Any], Any]):
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
    except service.MeetingError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})
    except AdapterConfigError as exc:
        return JSONResponse(status_code=503, content={"error": "adapter_config", "detail": str(exc)})
    from app.services.audit import recover_mutation_audits
    recover_mutation_audits(limit=50)
    return result


def _validate_body(body: dict, action: str) -> None:
    """Reject malformed JSON values before service conversions or SQLite bindings.

    Called after authorization so input errors cannot reveal a private meeting.
    Unknown fields retain the existing compatibility policy.
    """
    def invalid(field):
        raise service.MeetingError(400, "invalid_request", f"invalid type or value for {field}")

    for key in ("request_id", "expected_version"):
        if key not in body:
            continue
        value = body[key]
        if key == "request_id":
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                invalid(key)
        elif type(value) is not int:
            invalid(key)
    fields = {
        "create": {"participants": "list", "materials": "list", "models": "map",
                   "moderator": "optional_string", "title": "string", "topic": "optional_string",
                   "rounds": "int", "max_chars": "int", "turn_timeout_s": "int", "meeting_timeout_s": "int",
                   "wrap_up": "bool", "wrap_up_by": "optional_string", "mode": "string"},
        "patch": {"add": "list", "remove": "list", "models": "map", "rounds": "int", "mode": "string",
                  "wrap_up": "bool", "wrap_up_by": "optional_string",
                  "max_chars": "int", "turn_timeout_s": "int", "meeting_timeout_s": "int"},
        "materials": {"paths": "list"},
        "export": {"revision": "int", "board": "string"},
        "interject": {"text": "string"},
        "reject": {"reason": "string"},
        "skip": {"username": "string"},
    }.get(action, {})
    for key, kind in fields.items():
        if key not in body:
            continue
        value = body[key]
        if kind == "list":
            ok = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
        elif kind == "map":
            ok = isinstance(value, dict) and all(isinstance(v, str) and v.strip() for v in value.values())
        elif kind == "int":
            ok = type(value) is int
        elif kind == "bool":
            ok = type(value) is bool
        else:
            ok = isinstance(value, str) or (kind == "optional_string" and value is None)
        if not ok:
            invalid(key)


def _mutate(actor: Actor, meeting_id: str | None, action: str, body: dict, fn, versioned: bool = True):
    payload = {k: v for k, v in body.items() if k not in ("request_id", "expected_version")}
    expected = body.get("expected_version")
    if versioned and meeting_id is not None and expected is None:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "detail": "expected_version is required"})
    def run(conn):
        service.authorize(conn, actor, meeting_id, action)  # before replay or version (R1)
        _validate_body(body, action)
        return service.handle_request(conn, actor, meeting_id, body.get("request_id"), action, payload,
                                      expected if versioned else None, lambda: fn(conn, payload))
    return _run(run)


def _crew(actor: Actor) -> JSONResponse | None:
    if not actor.is_crew and actor.membership_kind != "system":
        return JSONResponse(status_code=403, content={"error": "forbidden", "detail": "meetings are for crew members"})
    return None


@router.post("/meetings", status_code=201)
def create(body: dict = Body(...), actor: Actor = Depends(require_actor)):
    return _crew(actor) or _mutate(actor, None, "create", body, lambda conn, p: service.create_meeting(conn, actor, p))


@router.get("/meetings")
def list_(actor: Actor = Depends(require_actor)):
    return _crew(actor) or _run(lambda conn: {"meetings": service.list_meetings(conn, actor)})


@router.get("/meetings/adapters")
def adapters(actor: Actor = Depends(require_actor)):
    def run(_conn):
        from pathlib import Path
        out = []
        for name, m in sorted(load_members().items()):
            installed = Path(m.command[0]).exists() and Path(m.cwd).is_dir()
            if m.adapter in ("claude_code", "hermes"):
                from app.meetings.adapters.claude_runtime import health
                status = health(m)
            else:
                status = {"ok": installed, "status": "installed" if installed else "missing", "live_verified": False}
            out.append({"username": name, "adapter": m.adapter,
                        "support_tier": "mock" if m.adapter == "mock" else "experimental",
                        "models": list(m.models), "health": status})
        policy = load_agent_meeting_policy()
        small = {"models": list(policy.auto_start_models), "max_agents": policy.max_agents,
                 "max_rounds": policy.max_rounds, "max_meeting_timeout_s": policy.max_meeting_timeout_s,
                 "per_day": policy.per_day}
        return {"members": out, "agent_small_meetings": small}  # never calls a model (A16)
    return _crew(actor) or _run(run)


@router.get("/meetings/{meeting_id}")
def detail(meeting_id: str, actor: Actor = Depends(require_actor)):
    return _crew(actor) or _run(lambda conn: service.get_meeting(conn, actor, meeting_id))


@router.get("/meetings/{meeting_id}/minutes")
def minutes(meeting_id: str, actor: Actor = Depends(require_actor)):
    return _crew(actor) or _run(lambda conn: service.get_minutes(conn, actor, meeting_id))


def _action(name: str, fn, versioned: bool = True):
    def handler(meeting_id: str, body: dict = Body(default={}), actor: Actor = Depends(require_actor)):
        return _crew(actor) or _mutate(actor, meeting_id, name, body, lambda conn, p: fn(conn, actor, meeting_id, p),
                                       versioned=versioned)
    handler.__name__ = f"meeting_{name}"
    return handler


router.patch("/meetings/{meeting_id}")(_action("patch", lambda c, a, i, p: service.patch_meeting(c, a, i, p)))
router.post("/meetings/{meeting_id}/materials")(_action("materials", lambda c, a, i, p: service.attach_materials(c, a, i, p.get("paths") or [])))
router.post("/meetings/{meeting_id}/approve")(_action("approve", lambda c, a, i, p: service.approve(c, a, i)))
router.post("/meetings/{meeting_id}/reject")(_action("reject", lambda c, a, i, p: service.reject(c, a, i, p.get("reason"))))
router.post("/meetings/{meeting_id}/start")(_action("start", lambda c, a, i, p: service.start(c, a, i)))
router.post("/meetings/{meeting_id}/pause")(_action("pause", lambda c, a, i, p: service.pause(c, a, i)))
router.post("/meetings/{meeting_id}/resume")(_action("resume", lambda c, a, i, p: service.resume(c, a, i)))
router.post("/meetings/{meeting_id}/skip")(_action("skip", lambda c, a, i, p: service.skip_turn(c, a, i, p.get("username"))))
router.post("/meetings/{meeting_id}/close")(_action("close", lambda c, a, i, p: service.close(c, a, i)))
router.post("/meetings/{meeting_id}/delete")(_action("delete", lambda c, a, i, p: service.delete_meeting(c, a, i)))
router.post("/meetings/{meeting_id}/leave")(_action("leave", lambda c, a, i, p: service.leave(c, a, i), versioned=False))
router.post("/meetings/{meeting_id}/interject")(_action(
    "interject", lambda c, a, i, p: service.interject(c, a, i, p.get("text")), versioned=False))
router.post("/meetings/{meeting_id}/export")(_action(
    "export", lambda c, a, i, p: service.export_minutes(c, a, i, int(p.get("revision") or 0), str(p.get("board") or "")),
    versioned=False))


@router.post("/meetings/attempts/{attempt_id}/release")
def release(attempt_id: str, body: dict = Body(default={}), actor: Actor = Depends(require_actor)):
    def run(conn):
        if not actor.is_admin:
            raise service.MeetingError(403, "forbidden")
        row = conn.execute("SELECT meeting_id FROM meeting_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise service.MeetingError(404, "not_found")
        _validate_body(body, "release")
        return service.handle_request(conn, actor, row["meeting_id"], body.get("request_id"), "release",
                                      {"attempt_id": attempt_id}, None,
                                      lambda: service.release_quarantine(conn, actor, attempt_id))
    return _run(run)
