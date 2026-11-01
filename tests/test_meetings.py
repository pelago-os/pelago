"""Meetings v0.2 acceptance (docs/meetings-contract.md section 14), with a real child-process mock."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.meetings.runner import group_alive

AGENTS = ("agent_alpha", "agent_beta", "agent_gamma")
MOCK = Path(__file__).resolve().parents[1] / "app" / "meetings" / "adapters" / "mock_agent.py"


def tok(user: str) -> dict:
    return {"Authorization": f"Bearer {user}-dev-token"}


def rid() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PELAGO_DB_PATH", str(tmp_path / "forum.db"))
    monkeypatch.setenv("PELAGO_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PELAGO_UPLOAD_ROOT", str(tmp_path / "uploads"))
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("PELAGO_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("PELAGO_MEETINGS_ENABLED", "1")
    controls = {}
    lines = []
    for name in AGENTS:
        work = tmp_path / "work" / name
        work.mkdir(parents=True)
        controls[name] = tmp_path / f"{name}.control.json"
        lines += [f"[members.{name}]", 'adapter = "mock"',
                  f'command = ["{sys.executable}", "{MOCK}", "--tag={tmp_path}"]', f'cwd = "{work}"',
                  'models = ["mock-1"]', f"[members.{name}.env]", f'MOCK_CONTROL = "{controls[name]}"', ""]
    # agents may start meetings on their own here; the small-meeting limits have their own tests
    lines += ["[agent_meetings]", 'people = ["owner", "moderator", "reviewer"]', 'auto_start_models = ["mock-1"]', "max_agents = 8", "max_rounds = 6",
              "max_meeting_timeout_s = 3600", "per_day = 1000"]
    adapters = tmp_path / "adapters.toml"
    adapters.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("PELAGO_MEETING_ADAPTERS", str(adapters))

    import app.config as config
    config.settings = config.Settings.from_env()
    import app.database as database
    with database.get_connection(config.settings.db_path) as conn:
        database.init_schema(conn)
    from app.auth import hash_token
    from scripts.init_db import BOARDS
    with database.get_connection() as conn:
        conn.executemany('INSERT INTO boards(slug,name,description,icon,digest_level,sort_order) VALUES (?,?,?,?,?,?)', BOARDS)
        for name in (*AGENTS, "owner", "moderator", "reviewer", "observer", "unconfigured_agent"):
            role = "admin" if name == "owner" else "moderator" if name == "moderator" else "member"
            cur = conn.execute("INSERT INTO users(username,display_name,role,membership_kind,status) VALUES (?,?,?,'crew','active')", (name,name,role))
            conn.execute("INSERT INTO tokens(user_id,token_hash,label) VALUES (?,?,?)", (cur.lastrowid,hash_token(name+"-dev-token"),"test"))
    from app.main import app
    from app.meetings import service
    from app.meetings.runner import Runner

    class Env:
        pass

    e = Env()
    e.client, e.tmp, e.library, e.controls, e.service = TestClient(app), tmp_path, library, controls, service
    e.offset = 0.0
    monkeypatch.setattr(service, "clock", lambda: time.time() + e.offset)
    e.new_runner = lambda: Runner(lease_s=30, renew_s=10, poll_s=0.05, term_grace=0.5, kill_wait=3,
                                  run_dir=tmp_path / "run")
    e.runner = e.new_runner()
    yield e
    e.runner.fence("test teardown")
    receipt = scan_and_reap(f"--tag={tmp_path}", tmp_path.name)
    assert receipt is None, f"processes outlived the test, receipt: {receipt}"


RECEIPTS = Path(os.environ.get("MEETING_TEST_RECEIPTS", Path(tempfile.gettempdir()) / "pelago-meeting-leaks"))


def scan_and_reap(tag: str, test_ref: str) -> str | None:
    """Find every process (mock, launcher, or descendant) whose command line carries this
    test's tag. Any live one is a leak: kill its whole group, prove the group gone, and keep
    a process_group_ref -> termination_receipt record. Returns the receipt path, or None."""
    out = subprocess.run(["ps", "-ww", "-A", "-o", "pid=,ppid=,pgid=,stat=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and tag in parts[4] and not parts[3].startswith("Z") and int(parts[0]) != os.getpid():
            found.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
                          "stat": parts[3], "command": parts[4][:300]})
    if not found:
        return None
    records = []
    for pgid in sorted({f["pgid"] for f in found}):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            signal_result = type(exc).__name__
        else:
            signal_result = "SIGKILL sent"
        deadline = time.time() + 5
        while time.time() < deadline and group_alive(pgid) is not False:
            time.sleep(0.05)
        records.append({"process_group_ref": {"pgid": pgid, "members": [f for f in found if f["pgid"] == pgid]},
                        "termination_receipt": {"signal": signal_result, "group_gone": group_alive(pgid) is False,
                                                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}})
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    path = RECEIPTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{test_ref}.json"
    path.write_text(json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST", test_ref), "leaks": records},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def control(e, name, **kw):
    e.controls[name].write_text(json.dumps(kw), encoding="utf-8")


def calls(e, name) -> list[dict]:
    p = Path(str(e.controls[name]) + ".calls")
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def create(e, organizer="moderator", participants=("agent_alpha", "agent_beta"), **settings):
    settings.setdefault("wrap_up", False)  # the wrap-up slot has its own tests; elsewhere it would add a turn
    body = {"request_id": rid(), "title": "Harbour planning", "topic": "Where the new pier goes",
            "participants": list(participants), **settings}
    r = e.client.post("/api/meetings", headers=tok(organizer), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def get(e, mid, user="moderator"):
    return e.client.get(f"/api/meetings/{mid}", headers=tok(user))


def act(e, mid, action, user="moderator", version=None, request_id=None, **extra):
    if version is None:
        version = get(e, mid, "owner").json()["version"]
    body = {"request_id": request_id or rid(), "expected_version": version, **extra}
    return e.client.post(f"/api/meetings/{mid}/{action}", headers=tok(user), json=body)


def drive(e, until, runner=None, timeout=20.0):
    runner = runner or e.runner
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner.tick()
        if until():
            return
        time.sleep(0.03)
    raise AssertionError("condition not reached")


def state(e, mid):
    return get(e, mid, "owner").json()


def attempts(e, mid):
    return state(e, mid)["attempts"]


# ------------------------------------------------------------------ the happy path

def test_two_round_meeting_completes_with_minutes(env):
    m = create(env)
    assert [p["username"] for p in m["participants"]] == ["moderator", "agent_alpha", "agent_beta"]
    assert m["participants"][0]["kind"] == "human"
    r = act(env, m["id"], "start")
    assert r.status_code == 200, r.text
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    s = state(env, m["id"])
    assert [a["state"] for a in s["attempts"]] == ["accepted"] * 4
    mins = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()
    assert "incomplete: false" in mins["latest"]["body_md"]
    assert len(calls(env, "agent_alpha")) == 2 and len(calls(env, "agent_beta")) == 2


# ------------------------------------------------------------------ A1, A2 length limit

def test_a1_limit_boundary_and_next_prompt_sees_truncated_text(env):
    m = create(env, rounds=1)
    control(env, "agent_alpha", text="甲" * 100)
    control(env, "agent_beta", text="乙" * 101)
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    a = attempts(env, m["id"])
    assert (a[0]["truncated"], a[0]["char_count"]) == (0, 100)
    assert (a[1]["truncated"], len(a[1]["text"]), a[1]["char_count"]) == (1, 100, 101)
    m2 = create(env, rounds=2, participants=("agent_alpha", "agent_beta"))
    control(env, "agent_beta", text="乙" * 150)
    act(env, m2["id"], "start")
    drive(env, lambda: state(env, m2["id"])["state"] == "completed")
    last_prompt = calls(env, "agent_beta")[-1]["prompt"]
    assert "乙" * 100 in last_prompt and "乙" * 101 not in last_prompt


def test_a2_moderator_round_one_exempt(env):
    m = create(env, rounds=2, moderator="agent_alpha")
    control(env, "agent_alpha", text="长" * 150)
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    by_round = {(a["round"], a["seat"]): a for a in attempts(env, m["id"])}
    cedar_seat = 1
    assert by_round[(1, cedar_seat)]["truncated"] == 0 and by_round[(1, cedar_seat)]["char_count"] == 150
    assert by_round[(2, cedar_seat)]["truncated"] == 1


# ------------------------------------------------------------------ A3, A4, A5 ways out

def test_a3_paused_meeting_does_not_lock_its_members(env):
    m = create(env, organizer="moderator", participants=("agent_alpha", "agent_beta"))
    act(env, m["id"], "start")
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    other = create(env, organizer="agent_alpha", participants=("agent_beta", "agent_gamma"))
    r = act(env, other["id"], "start", user="agent_alpha")
    assert r.status_code == 200, r.text


def test_a4_leave_is_self_only_and_idempotent(env):
    m = create(env, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    act(env, m["id"], "start")
    req = rid()
    first = act(env, m["id"], "leave", user="agent_beta", request_id=req)
    again = act(env, m["id"], "leave", user="agent_beta", request_id=req)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert act(env, m["id"], "leave", user="agent_beta").status_code == 200  # a new request is still fine
    seat = next(p["seat"] for p in state(env, m["id"])["participants"] if p["username"] == "agent_beta")
    assert {s["state"] for s in state(env, m["id"])["slots"] if s["seat"] == seat} == {"skipped"}
    assert act(env, m["id"], "close", user="agent_gamma").status_code == 403
    assert env.client.patch(f"/api/meetings/{m['id']}", headers=tok("agent_gamma"),
                            json={"request_id": rid(), "expected_version": state(env, m["id"])["version"],
                                  "remove": ["agent_alpha"]}).status_code == 403


def test_a5_dormant_meeting_can_be_closed_by_a_participant(env):
    m = create(env)
    act(env, m["id"], "start")
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    assert act(env, m["id"], "close", user="agent_alpha").status_code == 403  # paused, not yet dormant
    env.offset = 86_400 + 5
    env.runner = env.new_runner()  # a new process after a long gap
    assert state(env, m["id"])["state"] == "dormant"
    r = act(env, m["id"], "close", user="agent_alpha")
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["exit_reason"]) == ("closed", "participant_close_dormant")
    assert len(r.json()["attempts"]) >= 1


# ------------------------------------------------------------------ A6 single entry point

def test_a6_organizer_is_seated(env):
    m = create(env, organizer="agent_alpha", participants=("agent_beta",))
    assert m["organizer_user_id"] == next(p["user_id"] for p in m["participants"] if p["username"] == "agent_alpha")
    assert m["participants"][0]["username"] == "agent_alpha" and m["participants"][0]["kind"] == "agent"


# ------------------------------------------------------------------ A7, A8, A9 stopping

def test_a7_timeout_cancels_then_reaps_then_minutes_change_on_close(env):
    m = create(env, turn_timeout_s=10)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    env.offset = 11
    drive(env, lambda: attempts(env, m["id"])[0]["state"] == "cancelled")
    s = state(env, m["id"])
    assert (s["state"], s["exit_reason"], s["attempts"][0]["error_code"]) == ("paused", "turn_timeout", "timeout")
    mins = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()
    assert "incomplete: true" in mins["latest"]["body_md"] and "timeout" in mins["latest"]["body_md"]
    before = len(mins["revisions"])
    act(env, m["id"], "close")
    after = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()
    assert len(after["revisions"]) == before + 1 and after["latest"]["state"] == "closed"


@pytest.mark.parametrize("how", ["pause", "close", "leave"])
def test_a8_stop_during_hanging_turn(env, how):
    m = create(env, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    pgid = env.runner.procs[next(iter(env.runner.procs))].pgid
    r = act(env, m["id"], how, user="agent_alpha" if how == "leave" else "moderator")
    assert r.status_code == 200, r.text
    assert attempts(env, m["id"])[0]["state"] == "cancel_requested"
    if how == "pause":
        assert act(env, m["id"], "resume").json()["error"] == "draining"
    drive(env, lambda: attempts(env, m["id"])[0]["state"] == "cancelled")
    assert group_alive(pgid) is False
    a = attempts(env, m["id"])[0]
    with env_conn() as conn:
        assert env.service.confirm_terminated(conn, a["id"], env.runner.owner, env.runner.generation) == "noop"
    if how == "pause":
        control(env, "agent_alpha", mode="answer")
        assert act(env, m["id"], "resume").status_code == 200
        drive(env, lambda: any(a["attempt"] == 2 and a["state"] == "accepted" for a in attempts(env, m["id"])))
    if how == "leave":
        drive(env, lambda: any(a["seat"] == 2 and a["state"] == "accepted" for a in attempts(env, m["id"])))


def env_conn():
    from app.database import get_connection

    class _C:
        def __enter__(self):
            self.cm = get_connection()
            conn = self.cm.__enter__()
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, *exc):
            return self.cm.__exit__(*exc)
    return _C()


def test_a9_late_result_is_discarded(env):
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    a = attempts(env, m["id"])[0]
    act(env, m["id"], "pause")
    with env_conn() as conn:
        verdict = env.service.record_turn_result(conn, a["id"], env.runner.owner, env.runner.generation,
                                                 {"status": "accepted", "text": "too late"})
    assert verdict == "discarded"
    s = state(env, m["id"])
    assert s["state"] == "paused" and s["attempts"][0]["text"] is None


# ------------------------------------------------------------------ A10, A11 recovery

def _expire_lease():
    with env_conn() as conn:
        conn.execute("UPDATE meeting_scheduler SET lease_until = 0")


def test_a10_restart_kills_orphan_and_pauses(env):
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    pgid = next(iter(env.runner.procs.values())).pgid
    env.runner.procs.clear()  # the old server vanished without cleaning up
    _expire_lease()
    second = env.new_runner()
    second.tick()
    s = state(env, m["id"])
    assert (s["state"], s["exit_reason"]) == ("paused", "interrupted_restart")
    assert s["attempts"][0]["state"] == "interrupted" and len(s["attempts"]) == 1
    assert group_alive(pgid) is False
    assert env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]
    second.fence("done")


def test_a11_single_owner_and_stale_owner_writes_nothing(env):
    env.runner.tick()
    other = env.new_runner()
    other.tick()
    assert other.generation is None and env.runner.generation == 1
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    stale_owner, stale_gen = env.runner.owner, env.runner.generation
    a = attempts(env, m["id"])[0]
    pgid = next(iter(env.runner.procs.values())).pgid
    env.runner._lease_until_local = 0  # as if SIGSTOPped past the lease
    _expire_lease()
    other.tick()  # takes over, kills the child, pauses the meeting
    assert other.generation == 2
    assert group_alive(pgid) is False
    with env_conn() as conn:
        events = conn.execute("SELECT COUNT(*) FROM meeting_events").fetchone()[0]
        assert env.service.record_turn_result(conn, a["id"], stale_owner, stale_gen,
                                              {"status": "accepted", "text": "x"}) == "discarded"
        assert conn.execute("SELECT COUNT(*) FROM meeting_events").fetchone()[0] == events
    env.runner.tick()  # the old owner wakes up and fences itself
    assert env.runner.generation is None and not env.runner.procs
    other.fence("done")


def test_a11_quarantine_blocks_only_that_agent(env):
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    a = attempts(env, m["id"])[0]
    leaked = next(iter(env.runner.procs.values()))
    env.runner.procs.clear()
    with env_conn() as conn:  # pretend it ran on another machine
        conn.execute("UPDATE meeting_attempts SET exec_host = 'elsewhere' WHERE id = ?", (a["id"],))
    _expire_lease()
    second = env.new_runner()
    second.tick()
    assert attempts(env, m["id"])[0]["state"] == "quarantined"
    assert act(env, m["id"], "resume").json()["error"] == "quarantined"
    other = create(env, organizer="agent_beta", participants=("agent_gamma",) + ("agent_alpha",))
    act(env, other["id"], "start", user="agent_beta")
    drive(env, lambda: any(x["state"] == "accepted" for x in attempts(env, other["id"])), runner=second)
    assert env.client.post(f"/api/meetings/attempts/{a['id']}/release", headers=tok("moderator"),
                           json={"request_id": rid()}).status_code == 403
    assert env.client.post(f"/api/meetings/attempts/{a['id']}/release", headers=tok("owner"),
                           json={"request_id": rid()}).status_code == 200
    os.killpg(leaked.pgid, signal.SIGKILL)  # nobody else may touch it; the test cleans up its own
    leaked.popen.wait(timeout=5)
    second.fence("done")


# ------------------------------------------------------------------ A12 request ids

def test_a12_request_ids_and_versions(env):
    m = create(env)
    start_req = rid()
    v = state(env, m["id"])["version"]
    one = act(env, m["id"], "start", version=v, request_id=start_req)
    two = act(env, m["id"], "start", version=v, request_id=start_req)
    assert one.status_code == two.status_code == 200 and one.json() == two.json()
    pause_req = rid()
    v = state(env, m["id"])["version"]
    p1 = act(env, m["id"], "pause", version=v, request_id=pause_req)
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    act(env, m["id"], "resume")
    replay = act(env, m["id"], "pause", version=v, request_id=pause_req)
    assert replay.json() == p1.json() and state(env, m["id"])["state"] == "running"
    stale = act(env, m["id"], "pause", version=v)
    assert stale.status_code == 409 and stale.json()["error"] == "version_conflict"
    reuse = env.client.post(f"/api/meetings/{m['id']}/close", headers=tok("moderator"),
                            json={"request_id": pause_req, "expected_version": v})
    assert reuse.json()["error"] == "request_id_reused"


# ------------------------------------------------------------------ A13, A14 access

def _external(e):
    from app.auth import hash_token
    with env_conn() as conn:
        cur = conn.execute("""INSERT INTO users(username, display_name, role, status, membership_kind)
                              VALUES ('visitor','Visitor','member','active','external')""")
        conn.execute("INSERT INTO tokens(user_id, token_hash, label) VALUES (?,?,?)",
                     (cur.lastrowid, hash_token("visitor-dev-token"), "test"))


def test_a13_external_and_outsider(env):
    m = create(env)
    _external(env)
    for method, path in [("get", "/api/meetings"), ("get", f"/api/meetings/{m['id']}"),
                         ("post", "/api/meetings"), ("post", f"/api/meetings/{m['id']}/start"),
                         ("get", "/api/meetings/adapters"), ("get", f"/api/meetings/{m['id']}/minutes")]:
        r = getattr(env.client, method)(path, headers=tok("visitor"), **({"json": {}} if method == "post" else {}))
        assert r.status_code == 403, (path, r.text)
    assert get(env, m["id"], "observer").status_code == 404


def _material(e, name, visible_to):
    (e.library / name).write_text(f"---\nvisible_to: [{', '.join(visible_to)}]\n---\nThe pier plan.\n", encoding="utf-8")


def test_a14_materials_must_be_readable_by_everyone(env):
    _material(env, "pier.md", ["moderator", "agent_alpha"])
    r = env.client.post("/api/meetings", headers=tok("moderator"),
                        json={"request_id": rid(), "title": "t", "participants": ["agent_alpha", "agent_beta"], "materials": ["pier.md"]})
    assert r.status_code == 409 and r.json()["error"] == "material_not_readable"
    _material(env, "open.md", ["moderator", "agent_alpha", "agent_beta"])
    m = create(env, materials=["open.md"])
    v = state(env, m["id"])["version"]
    r = env.client.patch(f"/api/meetings/{m['id']}", headers=tok("moderator"),
                         json={"request_id": rid(), "expected_version": v, "add": ["agent_gamma"]})
    assert r.status_code == 409 and r.json()["error"] == "material_not_readable"


# ------------------------------------------------------------------ A15, A18 export

def test_a15_a18_export_rules(env):
    m = create(env, rounds=1)
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    rev = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]["revision"]
    body = lambda board: {"request_id": rid(), "revision": rev, "board": board}
    public = env.client.post(f"/api/meetings/{m['id']}/export", headers=tok("moderator"), json=body("tea_house"))
    assert public.status_code == 409 and public.json()["error"] == "export_scope"
    assert env.client.post(f"/api/meetings/{m['id']}/export", headers=tok("agent_alpha"), json=body("engineering")).status_code == 403
    one = env.client.post(f"/api/meetings/{m['id']}/export", headers=tok("moderator"), json=body("engineering")).json()
    two = env.client.post(f"/api/meetings/{m['id']}/export", headers=tok("moderator"), json=body("engineering")).json()
    assert one["thread_id"] == two["thread_id"] and two["existing"] is True
    _material(env, "narrow.md", ["moderator", "agent_alpha", "agent_beta"])
    n = create(env, rounds=1, materials=["narrow.md"])
    act(env, n["id"], "start")
    drive(env, lambda: state(env, n["id"])["state"] == "completed")
    rev = env.client.get(f"/api/meetings/{n['id']}/minutes", headers=tok("moderator")).json()["latest"]["revision"]
    narrow = env.client.post(f"/api/meetings/{n['id']}/export", headers=tok("moderator"),
                             json={"request_id": rid(), "revision": rev, "board": "engineering"})
    assert narrow.status_code == 409 and narrow.json()["error"] == "export_scope"


# ------------------------------------------------------------------ A16 health

def test_a16_health_never_calls_a_model(env):
    r = env.client.get("/api/meetings/adapters", headers=tok("moderator"))
    assert r.status_code == 200 and {m["username"] for m in r.json()["members"]} == set(AGENTS)
    assert all(calls(env, name) == [] for name in AGENTS)


# ------------------------------------------------------------------ A17, A19 membership over time

def test_a17_left_member_sees_a_frozen_snapshot_until_readded(env):
    m = create(env, rounds=3, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    act(env, m["id"], "start")
    drive(env, lambda: sum(a["state"] == "accepted" for a in attempts(env, m["id"])) >= 3)
    act(env, m["id"], "leave", user="agent_beta")
    frozen = get(env, m["id"], "agent_beta").json()
    assert frozen["view"] == "snapshot"
    assert max(a["round"] for a in frozen["attempts"] if a["state"] == "accepted") == 1
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    later = get(env, m["id"], "agent_beta").json()
    assert later == frozen  # state, version, members, slots and turns all stay as they were
    listed = env.client.get("/api/meetings", headers=tok("agent_beta")).json()["meetings"]
    assert next(x for x in listed if x["id"] == m["id"])["state"] == frozen["state"]
    full = state(env, m["id"])
    assert full["state"] == "completed" and max(a["round"] for a in full["attempts"]) == 3


def test_a17_snapshot_after_readd_and_second_leave(env):
    m = create(env, rounds=3, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    act(env, m["id"], "start")
    act(env, m["id"], "leave", user="agent_beta")
    first = get(env, m["id"], "agent_beta").json()
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    v = state(env, m["id"])["version"]
    r = env.client.patch(f"/api/meetings/{m['id']}", headers=tok("moderator"),
                         json={"request_id": rid(), "expected_version": v, "add": ["agent_beta"]})
    assert r.status_code == 200, r.text
    assert get(env, m["id"], "agent_beta").json()["view"] == "full"
    act(env, m["id"], "leave", user="agent_beta")
    second = get(env, m["id"], "agent_beta").json()
    assert second["view"] == "snapshot" and second["state"] == "paused" and first["state"] == "running"
    act(env, m["id"], "close")
    assert get(env, m["id"], "agent_beta").json() == second


def test_r1_outsider_mutations_reveal_nothing(env):
    m = create(env)
    for action in ("pause", "start", "resume", "close", "leave", "materials", "export"):
        r = env.client.post(f"/api/meetings/{m['id']}/{action}", headers=tok("agent_gamma"),
                            json={"request_id": rid(), "expected_version": -1})
        assert r.status_code == 404 and r.json().get("detail") is None, (action, r.text)
    r = env.client.patch(f"/api/meetings/{m['id']}", headers=tok("agent_gamma"), json={"request_id": rid(), "expected_version": -1})
    assert r.status_code == 404
    req = rid()
    assert act(env, m["id"], "start", request_id=req).status_code == 200
    replay = env.client.post(f"/api/meetings/{m['id']}/start", headers=tok("agent_gamma"),
                             json={"request_id": req, "expected_version": 1})
    assert replay.status_code == 404  # a cached answer is not a way in


def test_a19_add_while_paused_and_restart(env):
    m = create(env, rounds=3)
    act(env, m["id"], "start")
    drive(env, lambda: sum(a["state"] == "accepted" for a in attempts(env, m["id"])) >= 2)
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    v = state(env, m["id"])["version"]
    r = env.client.patch(f"/api/meetings/{m['id']}", headers=tok("moderator"),
                         json={"request_id": rid(), "expected_version": v, "add": ["agent_gamma"]})
    assert r.status_code == 200, r.text
    agent_gamma = next(p for p in r.json()["participants"] if p["username"] == "agent_gamma")
    assert agent_gamma["joined_from_round"] == 3
    open_before = [s for s in r.json()["slots"] if s["state"] == "open"][0]
    env.runner.fence("restart")
    _expire_lease()
    env.runner = env.new_runner()
    open_after = [s for s in state(env, m["id"])["slots"] if s["state"] == "open"][0]
    assert open_before == open_after


# ------------------------------------------------------------------ A20, A23 failures

def test_a20_a23_wrong_model_fails_and_pauses_without_retry(env):
    m = create(env)
    control(env, "agent_alpha", mode="wrong_model")
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "paused")
    for _ in range(5):
        env.runner.tick()
    s = state(env, m["id"])
    assert (s["exit_reason"], [a["state"] for a in s["attempts"]], s["attempts"][0]["error_code"]) == \
        ("turn_failed", ["failed"], "model_mismatch")


def test_a23_leave_then_confirm_keeps_slots_skipped(env):
    m = create(env, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    act(env, m["id"], "leave", user="agent_alpha")
    drive(env, lambda: attempts(env, m["id"])[0]["state"] == "cancelled")
    seat = next(p["seat"] for p in state(env, m["id"])["participants"] if p["username"] == "agent_alpha")
    assert {s["state"] for s in state(env, m["id"])["slots"] if s["seat"] == seat} == {"skipped"}


# ------------------------------------------------------------------ A21, A22 process groups

def test_a21_launcher_never_execs_without_go(env, tmp_path):
    from app.meetings import runner as runner_mod
    marker = tmp_path / "ran"
    r, w = os.pipe()
    identity = tmp_path / "id.json"
    p = subprocess.Popen([sys.executable, str(runner_mod.LAUNCHER), str(identity), str(r), "--",
                          "/usr/bin/touch", str(marker)], pass_fds=(r,), start_new_session=True)
    os.close(r)
    deadline = time.time() + 5
    while not identity.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert identity.exists()
    os.kill(p.pid, signal.SIGSTOP)
    os.close(w)  # the parent goes away while the launcher is stopped
    os.kill(p.pid, signal.SIGCONT)
    assert p.wait(timeout=5) == 75 and not marker.exists()


def test_a22_descendant_keeps_slot_until_group_gone(env):
    m = create(env, rounds=1)
    control(env, "agent_alpha", mode="orphan")
    act(env, m["id"], "start")
    drive(env, lambda: attempts(env, m["id"])[0]["state"] == "accepted")
    out = subprocess.run(["pgrep", "-f", "time.sleep\\(3600\\)"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


# ------------------------------------------------------------------ A11 with real processes

PROC = Path(__file__).with_name("meeting_runner_proc.py")


def _spawn_runner(e, name, **extra):
    env = dict(os.environ, MEETING_TEST_RUN_DIR=str(e.tmp / "run"), **extra)
    log = open(e.tmp / f"{name}.log", "w")
    p = subprocess.Popen([sys.executable, str(PROC)], env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return p, e.tmp / f"{name}.log"


def _wait(cond, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _db_rows(sql, args=()):
    from app.database import get_connection
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _stop(*procs):
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGCONT)
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(timeout=5)


def test_a11_real_processes_sigstop_takeover_and_sigcont(env):
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    a_proc, a_log = _spawn_runner(env, "a")
    b_proc = None
    try:
        _wait(lambda: _db_rows("SELECT owner_id FROM meeting_scheduler"))
        act(env, m["id"], "start")
        _wait(lambda: any(r["state"] == "running" for r in _db_rows(
            "SELECT state FROM meeting_attempts WHERE meeting_id = ?", (m["id"],))))
        att = _db_rows("SELECT * FROM meeting_attempts WHERE meeting_id = ?", (m["id"],))[0]
        os.kill(a_proc.pid, signal.SIGSTOP)  # the owner freezes with its child running
        b_proc, b_log = _spawn_runner(env, "b")
        _wait(lambda: _db_rows("SELECT state FROM meeting_attempts WHERE id = ?", (att["id"],))[0]["state"] == "interrupted")
        assert group_alive(att["exec_pgid"]) is False  # the new owner killed the old child first
        s = state(env, m["id"])
        assert (s["state"], s["exit_reason"]) == ("paused", "interrupted_restart")
        events = len(_db_rows("SELECT id FROM meeting_events"))
        attempts_before = _db_rows("SELECT id, state, generation FROM meeting_attempts ORDER BY id")
        os.kill(a_proc.pid, signal.SIGCONT)  # the old owner wakes up
        _wait(lambda: "fenced" in a_log.read_text())
        time.sleep(0.5)
        assert len(_db_rows("SELECT id FROM meeting_events")) == events
        assert _db_rows("SELECT id, state, generation FROM meeting_attempts ORDER BY id") == attempts_before
        assert _db_rows("SELECT generation FROM meeting_scheduler")[0]["generation"] == 2
    finally:
        _stop(*(p for p in (a_proc, b_proc) if p))


def test_a11_real_processes_stall_between_mark_running_and_go(env):
    m = create(env)
    a_proc, a_log = _spawn_runner(env, "a", MEETING_TEST_STALL_GO="4")
    b_proc = None
    try:
        _wait(lambda: _db_rows("SELECT owner_id FROM meeting_scheduler"))
        act(env, m["id"], "start")
        _wait(lambda: "stalling before go" in a_log.read_text())
        att = _db_rows("SELECT * FROM meeting_attempts WHERE meeting_id = ?", (m["id"],))[0]
        assert att["state"] == "running" and att["exec_pgid"]
        b_proc, _ = _spawn_runner(env, "b")
        _wait(lambda: _db_rows("SELECT state FROM meeting_attempts WHERE id = ?", (att["id"],))[0]["state"] == "interrupted")
        _wait(lambda: "fenced" in a_log.read_text(), timeout=10)
        time.sleep(0.3)
        assert calls(env, "agent_alpha") == []  # the adapter command never ran
        assert group_alive(att["exec_pgid"]) is False
    finally:
        _stop(*(p for p in (a_proc, b_proc) if p))


def test_a11_recovery_probe_holds_no_database_lock(env):
    from app.database import get_connection
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    env.runner.procs.clear()
    _expire_lease()
    second = env.new_runner()
    seen = []
    real_probe = second.probe

    def probe(attempt):
        with get_connection() as conn:  # another writer during the probe must not wait
            conn.execute("PRAGMA busy_timeout=200")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE meetings SET updated_at = updated_at WHERE id = ?", (m["id"],))
            seen.append("wrote")
        return real_probe(attempt)

    second.probe = probe
    second.tick()
    assert seen == ["wrote"] and attempts(env, m["id"])[0]["state"] == "interrupted"
    second.fence("done")


# ------------------------------------------------------------------ the leak detector itself

def test_leak_detector_covers_launcher_and_descendants(tmp_path, monkeypatch):
    from app.meetings import runner as runner_mod
    tag = f"--tag={tmp_path}/detector"
    r, w = os.pipe()
    launcher = subprocess.Popen([sys.executable, str(runner_mod.LAUNCHER), str(tmp_path / "id.json"), str(r), "--",
                                 sys.executable, str(MOCK), tag], pass_fds=(r,), start_new_session=True)
    os.close(r)
    control_file = tmp_path / "orphan.json"
    control_file.write_text(json.dumps({"mode": "orphan"}), encoding="utf-8")
    # no pipes: the descendant would inherit them and keep run() waiting
    mock = subprocess.run([sys.executable, str(MOCK), tag], input=b"hi", stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=dict(os.environ, MOCK_CONTROL=str(control_file)),
                          start_new_session=True, timeout=10)
    assert mock.returncode == 0  # the leader is gone; its sleeping descendant is not
    time.sleep(0.3)
    monkeypatch.setattr(sys.modules[__name__], "RECEIPTS", tmp_path / "receipts")
    receipt = scan_and_reap(tag, "detector")
    os.close(w)
    launcher.wait(timeout=5)
    data = json.loads(Path(receipt).read_text(encoding="utf-8"))
    commands = " ".join(m["command"] for leak in data["leaks"] for m in leak["process_group_ref"]["members"])
    assert "launcher.py" in commands and "time.sleep(3600)" in commands
    assert all(leak["termination_receipt"]["group_gone"] for leak in data["leaks"])
    assert scan_and_reap(tag, "detector") is None


def test_r2_snapshot_hides_materials_added_after_leaving_in_draft(env):
    _material(env, "later.md", ["moderator", "agent_alpha", "agent_beta", "agent_gamma"])
    m = create(env, participants=("agent_alpha", "agent_beta", "agent_gamma"))
    act(env, m["id"], "leave", user="agent_gamma")
    v = state(env, m["id"])["version"]
    r = env.client.post(f"/api/meetings/{m['id']}/materials", headers=tok("moderator"),
                        json={"request_id": rid(), "expected_version": v, "paths": ["later.md"]})
    assert r.status_code == 200, r.text
    assert get(env, m["id"], "agent_gamma").json()["materials"] == []
    listed = env.client.get("/api/meetings", headers=tok("agent_gamma")).json()["meetings"]
    assert next(x for x in listed if x["id"] == m["id"])["view"] == "snapshot"
    assert get(env, m["id"], "agent_alpha").json()["materials"] == ["later.md"]


# ------------------------------------------------------------------ P2 D1: per-member model choice

def test_d1_models_only_from_each_members_allowlist(env):
    bad = env.client.post("/api/meetings", headers=tok("moderator"), json={
        "request_id": rid(), "title": "t", "participants": ["agent_alpha", "agent_beta"], "models": {"agent_alpha": "opus-of-doom"}})
    assert bad.status_code == 422 and bad.json()["detail"]["allowed"] == ["mock-1"]
    human = env.client.post("/api/meetings", headers=tok("moderator"), json={
        "request_id": rid(), "title": "t", "participants": ["agent_alpha", "agent_beta"], "models": {"moderator": "mock-1"}})
    assert human.status_code == 422
    m = create(env, models={"agent_alpha": "mock-1"})
    assert {p["username"]: p["model"] for p in m["participants"]}["agent_alpha"] == "mock-1"
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    a = attempts(env, m["id"])[0]
    assert (a["requested_model"], a["model"], a["fallback_used"]) == ("mock-1", "mock-1", 0)
    assert a["adapter_rev"]


# ------------------------------------------------------------------ units outside the process group (D3 review R2)
# An adapter may start work the runner's process group does not contain (OpenClaw: a Docker container the
# daemon owns). What a launch starts is recorded before launch (exec_external); cancel, exit and restart
# recovery act on that record only, never on the live adapter config, and free the slot only once those
# units are confirmed gone. A missing record is not evidence of none.

@pytest.mark.parametrize("confirmed", [True, False])
def test_cancel_frees_the_slot_only_when_external_units_are_confirmed_gone(env, monkeypatch, confirmed):
    import app.meetings.runner as runner_mod
    seen = []
    monkeypatch.setattr(runner_mod, "external_units_gone", lambda snap: seen.append(snap) or confirmed)
    m = create(env, participants=("agent_alpha", "agent_beta"))
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    assert act(env, m["id"], "pause").status_code == 200
    final = "cancelled" if confirmed else "quarantined"
    drive(env, lambda: attempts(env, m["id"])[0]["state"] == final)
    assert seen and all(snap == {"kind": "none", "adapter": "mock"} for snap in seen)  # cancellation and cleanup both use the launch snapshot
    if not confirmed:
        assert act(env, m["id"], "resume").json()["error"] == "quarantined"


def test_finished_turn_is_not_accepted_while_an_external_unit_may_still_run(env, monkeypatch):
    import app.meetings.runner as runner_mod
    monkeypatch.setattr(runner_mod, "external_units_gone", lambda snap: False)
    m = create(env, participants=("agent_alpha", "agent_beta"))
    act(env, m["id"], "start")
    drive(env, lambda: attempts(env, m["id"]) and attempts(env, m["id"])[0]["state"] == "quarantined")
    assert all(a["state"] != "accepted" for a in attempts(env, m["id"]))


def test_launch_snapshot_is_persisted_with_the_attempt(env):
    m = create(env, participants=("agent_alpha", "agent_beta"))
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    with env_conn() as conn:
        rows = conn.execute("SELECT exec_external FROM meeting_attempts WHERE meeting_id = ?", (m["id"],)).fetchall()
    assert [json.loads(r["exec_external"]) for r in rows] == [{"adapter": "mock", "kind": "none"}] * 4


def test_launch_is_refused_when_the_snapshot_cannot_be_taken(env, monkeypatch):
    import app.meetings.runner as runner_mod

    def broken(member, attempt_id):
        raise RuntimeError("cannot resolve the Docker endpoint")
    monkeypatch.setattr(runner_mod, "launch_external_snapshot", broken)
    m = create(env, participants=("agent_alpha", "agent_beta"))
    act(env, m["id"], "start")
    drive(env, lambda: attempts(env, m["id"]) and attempts(env, m["id"])[0]["state"] == "failed")
    a = attempts(env, m["id"])[0]
    assert a["error_code"] == "adapter_unavailable" and calls(env, "agent_alpha") == []  # nothing was started


def _dead_group_probe(env, attempt_id, exec_external):
    dead = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    dead.wait()
    return {"id": attempt_id, "exec_host": env.runner.host, "exec_pgid": dead.pid, "exec_pid": dead.pid,
            "exec_started_at": "", "exec_external": exec_external}


DOCKER_SNAP = json.dumps({"kind": "docker", "adapter": "openclaw", "docker": "/x/docker", "host": "unix:///x.sock",
                          "container": "meeting-openclaw-att_1", "state_root": None, "turn_dir": "turn-att_1"})


@pytest.mark.parametrize("exec_external,confirmed,verdict", [
    (json.dumps({"kind": "none", "adapter": "mock"}), None, "gone"),
    (DOCKER_SNAP, None, "unknown"),
    (None, None, "unknown"),            # no record: not evidence of no container
    ("{not json", None, "unknown"),
    (json.dumps({"kind": "vm"}), None, "unknown"),
])
def test_recovery_probe_acts_on_the_launch_snapshot(env, monkeypatch, exec_external, confirmed, verdict):
    import app.meetings.adapters as adapters_mod
    import app.meetings.runner as runner_mod
    m = create(env, participants=("agent_alpha", "agent_beta"))
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    attempt_id = attempts(env, m["id"])[0]["id"]
    seen = []
    if confirmed is not None:
        monkeypatch.setattr("app.meetings.adapters.openclaw.remove_turn_units", lambda snap: seen.append(snap) or confirmed)
    # the member's live config no longer matters: deleted, or switched to another adapter type
    monkeypatch.setattr(runner_mod, "load_members", lambda: {})
    assert env.runner.probe(_dead_group_probe(env, attempt_id, exec_external)) == verdict
    if confirmed is not None:
        assert seen and seen[0]["host"] == "unix:///x.sock"  # the recorded daemon, not the current context


# ------------------------------------------------------------------ 6.1 human interjections (v0.2.1)

def say(e, mid, text, user="moderator", request_id=None):
    return e.client.post(f"/api/meetings/{mid}/interject", headers=tok(user),
                         json={"request_id": request_id or rid(), "text": text})


def test_interjection_reaches_later_prompts_and_minutes_in_time_order(env):
    m = create(env, rounds=2)
    act(env, m["id"], "start")
    drive(env, lambda: sum(a["state"] == "accepted" for a in attempts(env, m["id"])) == 1)
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    r = say(env, m["id"], "  先说预算，再说位置。  ")
    assert r.status_code == 200, r.text
    said = r.json()["interjections"]
    assert [(i["username"], i["text"], i["round"]) for i in said] == [("moderator", "先说预算，再说位置。", 1)]
    resumed = act(env, m["id"], "resume")
    assert resumed.status_code == 200, resumed.text
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    assert "先说预算" not in calls(env, "agent_alpha")[0]["prompt"]
    later = calls(env, "agent_beta")[-2]["prompt"]  # agent_beta's round-1 turn, claimed after the interjection
    assert "Harbour" in later and "(human, in the room): 先说预算，再说位置。" in later
    assert later.index("先说预算") > later.index("[round 1] ")  # after agent_alpha's round-1 turn
    body = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]["body_md"]
    turns = [line for line in body.splitlines() if line.startswith("- round ")]
    assert "(interjection): 先说预算，再说位置。" in turns[1] and len(turns) == 5


def test_interjection_rules(env):
    m = create(env, participants=("agent_alpha", "agent_beta", "reviewer"))  # reviewer has no adapter here: a human participant
    assert say(env, m["id"], "too early").status_code == 409  # draft: use the topic
    act(env, m["id"], "start")
    assert say(env, m["id"], "agents wait for their turn", user="agent_alpha").status_code == 403
    assert say(env, m["id"], "outsider", user="agent_gamma").status_code == 404
    assert say(env, m["id"], "   ").status_code == 400
    assert say(env, m["id"], "长" * 2001).status_code == 422
    assert say(env, m["id"], "长" * 2000).status_code == 200  # max_chars does not apply to people
    before = state(env, m["id"])["version"]
    req = rid()
    first = say(env, m["id"], "admin, not seated", user="owner", request_id=req)
    again = say(env, m["id"], "admin, not seated", user="owner", request_id=req)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert say(env, m["id"], "changed", user="owner", request_id=req).status_code == 409  # request_id_reused
    assert state(env, m["id"])["version"] == before + 1  # no expected_version needed while turns move it
    assert say(env, m["id"], "human participant", user="reviewer").status_code == 200
    act(env, m["id"], "leave", user="reviewer")
    frozen = get(env, m["id"], "reviewer").json()
    assert say(env, m["id"], "after leaving", user="reviewer").status_code == 403
    assert say(env, m["id"], "organizer again").status_code == 200
    assert get(env, m["id"], "reviewer").json() == frozen
    assert [i["text"] for i in frozen["interjections"]][-1] == "human participant"
    assert [a["state"] for a in attempts(env, m["id"])] == ["queued"]  # no slot, no attempt, no reorder
    act(env, m["id"], "close")
    assert say(env, m["id"], "too late").status_code == 409
    import sqlite3  # the event keeps the length, never the text
    with sqlite3.connect(os.environ["PELAGO_DB_PATH"]) as conn:
        payloads = [p for (p,) in conn.execute("SELECT payload_json FROM meeting_events WHERE kind = 'interjection'")]
    assert payloads and all("长" not in p and "human participant" not in p for p in payloads)


def test_interjection_order_is_by_time_even_mid_turn():
    from app.meetings.adapters import format_prompt
    from app.meetings.minutes import build_minutes
    from app.meetings.service import _transcript
    parts = {0: {"seat": 0, "display_name": "A", "kind": "agent", "status": "active", "joined_from_round": 1},
             1: {"seat": 1, "display_name": "B", "kind": "agent", "status": "active", "joined_from_round": 1}}
    turns = [{"round": 1, "seat": 0, "text": "a1", "ended_at": 10.0}, {"round": 1, "seat": 1, "text": "b1", "ended_at": 20.0}]
    said = [{"id": "int_1", "round": 1, "display_name": "Owner", "username": "owner", "text": "hi", "created_at": 15.0},
            {"id": "int_2", "round": 2, "display_name": "Owner", "username": "owner", "text": "bye", "created_at": 25.0}]
    script = _transcript(turns, parts, said)  # B was mid-turn at 15: it is placed by when it ended
    assert [t["text"] for t in script] == ["a1", "hi", "b1", "bye"] and script[1]["human"]
    prompt = format_prompt({"meeting": {"title": "t", "rounds": 2, "max_chars": 100}, "speaker": {"display_name": "A"},
                            "attempt": {"round": 2}, "exempt": False, "materials": [], "turns": script})
    assert "[round 1] Owner (human, in the room): hi" in prompt and "[round 1] A: a1" in prompt
    slots = [{"round": 1, "seat": 0, "state": "filled", "filled_attempt_id": "x"},
             {"round": 1, "seat": 1, "state": "filled", "filled_attempt_id": "y"}]
    atts = [dict(id="x", round=1, seat=0, attempt=1, state="accepted", text="a1", ended_at=10.0),
            dict(id="y", round=1, seat=1, attempt=1, state="accepted", text="b1", ended_at=20.0)]
    body = build_minutes({"title": "t", "state": "completed", "organizer_user_id": 1}, parts.values(), slots, atts, said)
    lines = [line for line in body.splitlines() if line.startswith("- round ")]
    assert [line.rsplit(": ", 1)[1] for line in lines] == ["a1", "hi", "b1", "bye"]


def test_command_admission_drift_fails_without_launch(env, monkeypatch):
    import app.meetings.runner as runner_mod
    def broken(*args, **kwargs):
        raise ValueError('agy runtime gate: fingerprint changed after snapshot')
    monkeypatch.setattr(runner_mod, 'build_command', broken)
    m = create(env, participants=('agent_alpha', 'agent_beta'))
    act(env, m['id'], 'start')
    drive(env, lambda: attempts(env, m['id']) and attempts(env, m['id'])[0]['state'] == 'failed')
    assert attempts(env, m['id'])[0]['error_code'] == 'adapter_unavailable'
    assert calls(env, 'agent_alpha') == []


# ------------------------------------------------------------------ section 4: meetings an agent organizes (v0.2.2)

def _policy(e, table: str | None, models: str = '["mock-1", "mock-2"]'):
    path = e.tmp / "adapters.toml"
    text = path.read_text(encoding="utf-8").split("[agent_meetings]")[0]
    text = text.replace('models = ["mock-1"]', f"models = {models}")
    path.write_text(text + (table or ""), encoding="utf-8")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))


SMALL = ('[agent_meetings]\npeople = ["owner", "moderator"]\nauto_start_models = ["mock-1"]\nmax_agents = 2\nmax_rounds = 3\n'
         'max_meeting_timeout_s = 600\nper_day = 1\n')


def _start(e, mid, user):
    return act(e, mid, "start", user=user)


def test_agent_meetings_need_approval_without_a_policy(env):
    _policy(env, None)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",))
    r = _start(env, m["id"], "agent_alpha")
    assert r.status_code == 409 and r.json()["error"] == "approval_required"
    assert r.json()["detail"]["reasons"] == ["agent_meetings_not_enabled"]
    view = get(env, m["id"], "owner").json()
    assert view["approval"] == {"needed": True, "reasons": ["agent_meetings_not_enabled"]}
    listed = env.client.get("/api/meetings", headers=tok("owner")).json()["meetings"]
    assert next(x for x in listed if x["id"] == m["id"])["awaiting_approval"] is True
    assert act(env, m["id"], "approve", user="owner").status_code == 200
    assert _start(env, m["id"], "agent_alpha").status_code == 200
    human = create(env, organizer="owner", participants=("agent_alpha", "agent_beta"))  # no list: administrators are people
    assert get(env, human["id"], "owner").json()["approval"] is None  # people are not limited by this
    assert _start(env, human["id"], "owner").status_code == 200
    unlisted = create(env, organizer="moderator", participants=("agent_alpha", "agent_beta"))
    assert _start(env, unlisted["id"], "moderator").status_code == 409  # not listed as a person: an agent


def test_small_agent_meeting_starts_once_a_day(env):
    _policy(env, SMALL)
    first = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=3)
    assert get(env, first["id"], "agent_alpha").json()["approval"] == {"needed": False, "reasons": []}
    assert _start(env, first["id"], "agent_alpha").status_code == 200
    second = create(env, organizer="agent_beta", participants=("agent_alpha",), rounds=1)
    r = _start(env, second["id"], "agent_beta")
    assert r.status_code == 409 and r.json()["detail"]["reasons"] == ["per_day>1"]
    env.offset = 86_400  # next day: allowed again
    assert _start(env, second["id"], "agent_beta").status_code == 200


@pytest.mark.parametrize("settings,reason", [
    ({"participants": ("agent_beta", "agent_gamma")}, "agents>2"),
    ({"rounds": 4}, "rounds>3"),
    ({"meeting_timeout_s": 900}, "meeting_timeout_s>600"),
    ({"models": {"agent_beta": "mock-2"}}, "model:agent_beta=mock-2"),
])
def test_bigger_or_dearer_agent_meetings_wait_for_approval(env, settings, reason):
    _policy(env, SMALL)
    settings = {"participants": ("agent_beta",), **settings}
    m = create(env, organizer="agent_alpha", **settings)
    r = _start(env, m["id"], "agent_alpha")
    assert r.status_code == 409 and reason in r.json()["detail"]["reasons"]
    assert state(env, m["id"])["state"] == "draft"
    act(env, m["id"], "approve", user="owner")
    assert _start(env, m["id"], "agent_alpha").status_code == 200  # approved: the limits no longer apply


def test_unapproved_agent_meeting_cannot_grow_while_paused(env):
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=2)
    _start(env, m["id"], "agent_alpha")
    act(env, m["id"], "pause", user="agent_alpha")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    patch = lambda user, **body: env.client.patch(f"/api/meetings/{m['id']}", headers=tok(user), json={
        "request_id": rid(), "expected_version": state(env, m["id"])["version"], **body})
    r = patch("agent_alpha", rounds=5)
    assert r.status_code == 409 and r.json()["detail"]["reasons"] == ["rounds>3"]
    assert state(env, m["id"])["settings"]["rounds"] == 2  # the refused edit left nothing behind
    assert patch("agent_alpha", rounds=3).status_code == 200
    assert patch("agent_alpha", models={"agent_beta": "mock-2"}).status_code == 409
    assert patch("owner", rounds=5).status_code == 200  # the administrator decides for herself


def test_unapproved_agent_meeting_turns_get_no_fallback_outside_the_list(env):
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=1)
    _start(env, m["id"], "agent_alpha")
    first = attempts(env, m["id"])[0]["id"]
    with env_conn() as conn:
        assert env.service._attempt_context(conn, first)["auto_models"] == ["mock-1"]


def test_r1_crew_without_an_adapter_is_an_agent_not_a_person(env):
    """engineer review R1: unconfigured_agent is crew with no adapter and not in `people`."""
    _policy(env, SMALL)
    m = create(env, organizer="unconfigured_agent", participants=("agent_alpha", "agent_beta"), rounds=6, models={"agent_alpha": "mock-2"})
    r = _start(env, m["id"], "unconfigured_agent")
    assert r.status_code == 409 and {"rounds>3", "model:agent_alpha=mock-2"} <= set(r.json()["detail"]["reasons"])
    assert get(env, m["id"], "owner").json()["approval"]["needed"] is True
    ok = create(env, organizer="unconfigured_agent", participants=("agent_alpha", "agent_beta"))
    r = _start(env, ok["id"], "unconfigured_agent")  # engineer R1b: unconfigured_agent only listens, but is still the third agent
    assert r.status_code == 409 and r.json()["detail"]["reasons"] == ["agents>2"]
    _policy(env, SMALL.replace("max_agents = 2", "max_agents = 3"))
    assert _start(env, ok["id"], "unconfigured_agent").status_code == 200  # within the limits it may start once
    # removing an adapter does not turn an agent into a person, nor give back today's meeting
    path = env.tmp / "adapters.toml"
    kept, skipping = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("[") and not line.startswith("[["):
            skipping = line.startswith("[members.agent_gamma")
        if not skipping:
            kept.append(line)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")  # agent_gamma's adapter is gone
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 2_000_000))
    again = create(env, organizer="agent_alpha", participants=("agent_beta",))
    assert _start(env, again["id"], "agent_alpha").json()["detail"]["reasons"] == ["per_day>1"]
    said = env.client.post(f"/api/meetings/{ok['id']}/interject", headers=tok("unconfigured_agent"),
                           json={"request_id": rid(), "text": "I am not a person here"})
    assert said.status_code == 403  # agents speak in their turns, adapter or not
    assert get(env, ok["id"], "unconfigured_agent").json()["can_interject"] is False
    assert get(env, ok["id"], "owner").json()["can_interject"] is True


def test_r2_resume_and_new_turns_follow_todays_policy(env):
    """engineer review R2: closing or tightening the policy holds unapproved agent meetings."""
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=2)
    assert _start(env, m["id"], "agent_alpha").status_code == 200
    act(env, m["id"], "pause", user="agent_alpha")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    _policy(env, None)  # automatic agent meetings switched off
    r = act(env, m["id"], "resume", user="agent_alpha")
    assert r.status_code == 409 and r.json()["detail"]["reasons"] == ["agent_meetings_not_enabled"]
    _policy(env, SMALL)
    assert act(env, m["id"], "resume", user="agent_alpha").status_code == 200  # same meeting: not counted twice today
    drive(env, lambda: sum(a["state"] == "accepted" for a in attempts(env, m["id"])) >= 1)
    _policy(env, SMALL.replace('auto_start_models = ["mock-1"]', 'auto_start_models = ["mock-2"]'))
    drive(env, lambda: state(env, m["id"])["state"] == "paused")
    s = state(env, m["id"])
    assert s["exit_reason"] == "agent_policy_changed"
    assert not any(a["state"] in ("queued", "claimed", "running") for a in s["attempts"])
    act(env, m["id"], "approve", user="owner")
    assert act(env, m["id"], "resume", user="agent_alpha").status_code == 200  # approved: today's policy no longer binds it
    drive(env, lambda: state(env, m["id"])["state"] == "completed")


@pytest.mark.parametrize("change", ["policy_off", "model_removed"])
def test_r2b_a_turn_queued_before_the_change_never_launches(env, change):
    """engineer review R2b: the check before launch catches a turn queued under the old policy."""
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=2)
    assert _start(env, m["id"], "agent_alpha").status_code == 200
    assert [a["state"] for a in attempts(env, m["id"])] == ["queued"]
    _policy(env, None if change == "policy_off" else SMALL.replace('["mock-1"]', '["mock-2"]'))
    drive(env, lambda: state(env, m["id"])["state"] == "paused"
          and not any(a["state"] in ("queued", "claimed", "running", "cancel_requested") for a in attempts(env, m["id"])))
    assert calls(env, "agent_alpha") == [] and calls(env, "agent_beta") == []  # no model was called
    s = state(env, m["id"])
    assert s["exit_reason"] == "agent_policy_changed"
    assert [a["state"] for a in s["attempts"]] == ["cancelled"]
    _policy(env, SMALL)
    assert act(env, m["id"], "resume", user="agent_alpha").status_code == 200  # not counted against the day again


# ------------------------------------------------------------------ section 5: the wrap-up slot (v0.2.3)

def test_wrap_up_needs_a_named_agent_when_a_person_organizes(env):
    m = create(env, wrap_up=True)
    assert m["settings"]["wrap_up"] == 1 and m["settings"]["wrap_up_seat"] is None  # 待指定, not guessed
    r = act(env, m["id"], "start")
    assert r.status_code == 422 and r.json()["detail"]["wrap_up_by"] is None
    bad = env.client.post("/api/meetings", headers=tok("moderator"), json={
        "request_id": rid(), "title": "t", "participants": ["agent_alpha", "agent_beta"], "wrap_up_by": "moderator"})
    assert bad.status_code == 422  # a person does not wrap up; an agent who speaks does
    patch = lambda **body: env.client.patch(f"/api/meetings/{m['id']}", headers=tok("moderator"), json={
        "request_id": rid(), "expected_version": state(env, m["id"])["version"], **body})
    assert patch(wrap_up_by="agent_beta").status_code == 200
    assert state(env, m["id"])["settings"]["wrap_up_seat"] == 2
    assert act(env, m["id"], "start").status_code == 200
    assert patch(wrap_up=False).status_code == 409  # decided before the meeting starts
    agent_led = create(env, organizer="agent_alpha", participants=("agent_beta",), wrap_up=True)
    assert agent_led["settings"]["wrap_up_seat"] == 0  # an agent organizer wraps up by default


def test_wrap_up_turn_closes_the_meeting_with_cited_minutes(env):
    m = create(env, rounds=1, wrap_up=True, wrap_up_by="agent_beta")
    names = {p["username"]: p["display_name"] for p in m["participants"]}
    wrap = (f"## 讨论摘要\n码头往东挪 (round 1, {names['agent_alpha']})。\n## 达成的决定\n待确认\n## 分歧与未决\n无\n"
            f"## 行动项\n- 画草图，负责人 待确认 (round 5, 不存在的人)\n## 给主持人的简报\n先看草图。" + "。" * 1300)
    control(env, "agent_beta", text=wrap)
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    s = state(env, m["id"])
    last = s["attempts"][-1]
    assert (last["round"], last["seat"], last["state"]) == (99, 2, "accepted")
    assert last["truncated"] == 1 and len(last["text"]) == 1200  # its own limit, not max_chars
    first_beta = next(a for a in s["attempts"] if a["round"] == 1 and a["seat"] == 2)
    assert len(first_beta["text"]) == 100  # ordinary turns keep max_chars
    prompt = calls(env, "agent_beta")[-1]["prompt"]
    assert "## 讨论摘要" in prompt and f"[round 1] {names['agent_alpha']}:" in prompt and "(round N, Name)" in prompt
    assert s["settings"]["rounds"] == 1  # the wrap-up is not a round
    body = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]["body_md"]
    assert f"## 会后纪要（{names['agent_beta']} 整理，AI 生成，待确认）" in body
    assert "以下引用找不到对应发言：(round 5, 不存在的人)" in body
    assert "round 99" not in body.split("## 会后纪要")[0]  # not listed as a turn
    # Owner 2026-10-21: with a summary, the minutes leave the turn-by-turn record to the meeting page
    assert "## Turns" not in body and "码头往东挪" in body
    assert "完整发言记录在会议页：2 次发言、0 次插话，这里不再逐条列出。" in body


def test_wrap_up_slot_survives_round_edits_and_a_leaving_speaker(env):
    m = create(env, rounds=2, wrap_up=True, wrap_up_by="agent_gamma", participants=("agent_alpha", "agent_beta", "agent_gamma"))
    act(env, m["id"], "start")
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in attempts(env, m["id"])))
    r = env.client.patch(f"/api/meetings/{m['id']}", headers=tok("moderator"), json={
        "request_id": rid(), "expected_version": state(env, m["id"])["version"], "rounds": 1})
    assert r.status_code == 200
    assert any(s["round"] == 99 for s in state(env, m["id"])["slots"])  # fewer rounds, same wrap-up
    act(env, m["id"], "leave", user="agent_gamma")
    act(env, m["id"], "resume")
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    wrap = next(s for s in state(env, m["id"])["slots"] if s["round"] == 99)
    assert wrap["state"] == "skipped"
    body = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]["body_md"]
    assert "（没有收尾：left）" in body


def test_small_agent_meeting_wrap_up_is_not_a_fourth_round(env):
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=3, wrap_up=True)
    assert act(env, m["id"], "start", user="agent_alpha").status_code == 200  # 3 rounds + wrap-up is still small
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    assert [a["round"] for a in attempts(env, m["id"])][-1] == 99
    said = env.client.post(f"/api/meetings/{m['id']}/interject", headers=tok("owner"), json={"request_id": rid(), "text": "x"})
    assert said.status_code == 409  # over; and while the wrap-up waits, a line is filed under the last round


def test_wrap_up_citation_check_reads_full_width_brackets_and_reports_none():
    from app.meetings.minutes import citation_check, citations
    known = {(1, "甲 / Alpha")}
    assert citations("（round 1, 甲 / Alpha）和（round 2，乙）") == [(1, "甲 / Alpha"), (2, "乙")]
    assert citation_check("（round 1, 甲 / Alpha）（round 2，乙）", known) == ["(round 2, 乙)"]
    assert citations("没有引用") == []
    # a real wrap-up (agent_gamma, 2026-10-21) grouped citations in one bracket
    assert citations("(round 2, 丙 / Gamma; round 2, 甲 / Alpha)；（round 1，丁）") == [
        (2, "丙 / Gamma"), (2, "甲 / Alpha"), (1, "丁")]
    assert citation_check("(round 2, 甲 / Alpha; round 9, 鬼)", known | {(2, "甲 / Alpha")}) == ["(round 9, 鬼)"]


def test_station_counts_running_and_paused_v02_meetings(env):
    # the base overview's MEETINGS figure counts the v0.2 meetings too, now that they are the default room (agent_gamma S2)
    systems = lambda: env.client.get("/api/station/snapshot", headers=tok("owner")).json()["systems"]
    before = systems()["running_sessions"] + systems()["paused_sessions"]
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    assert systems()["running_sessions"] == before + 1
    act(env, m["id"], "pause")
    assert systems()["paused_sessions"] >= 1


def test_skip_only_the_failed_turn_and_the_speaker_returns_next_round(env):
    # product requirement: a seat whose adapter failed can be skipped for this round only, without removing anyone
    m = create(env, rounds=2)
    control(env, "agent_alpha", mode="fail")
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "paused")
    assert act(env, m["id"], "skip", user="agent_alpha", username="agent_alpha").status_code == 403  # organizer or administrator
    assert act(env, m["id"], "skip", username="agent_beta").json()["error"] == "not_next"  # a stale page names someone else
    r = act(env, m["id"], "skip", username="agent_alpha")
    assert r.status_code == 200, r.text
    assert [(s["round"], s["seat"], s["state"], s["skip_reason"]) for s in r.json()["slots"] if s["state"] == "skipped"] \
        == [(1, 1, "skipped", "skipped_by_organizer")]
    control(env, "agent_alpha", mode="answer")
    assert act(env, m["id"], "resume").status_code == 200
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    accepted = [(a["round"], a["seat"]) for a in attempts(env, m["id"]) if a["state"] == "accepted"]
    assert accepted == [(1, 2), (2, 1), (2, 2)]  # agent_beta spoke in round 1, agent_alpha is back in round 2
    mins = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("moderator")).json()["latest"]["body_md"]
    assert "skipped_by_organizer" in mins


def test_skip_needs_a_stopped_meeting(env):
    m = create(env)
    control(env, "agent_alpha", mode="hang")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["state"] == "running" for a in attempts(env, m["id"])))
    assert act(env, m["id"], "skip", username="agent_alpha").json()["error"] == "invalid_transition"


def test_delete_hides_a_finished_meeting_but_keeps_its_rows(env):
    # product requirement: failed trial meetings pile up in the list; deleting hides them, nothing is erased
    m = create(env)
    act(env, m["id"], "start")
    assert act(env, m["id"], "delete").json()["error"] == "invalid_transition"  # close a live meeting first
    drive(env, lambda: state(env, m["id"])["state"] == "completed")
    listed = env.client.get("/api/meetings", headers=tok("moderator")).json()["meetings"]
    assert next(i for i in listed if i["id"] == m["id"])["can_delete"] is True
    assert act(env, m["id"], "delete", user="agent_alpha").status_code == 403  # organizer or administrator only
    r = act(env, m["id"], "delete")
    assert r.status_code == 200 and r.json()["status"] == "deleted", r.text
    for user in ("moderator", "owner", "agent_alpha"):
        assert all(i["id"] != m["id"] for i in env.client.get("/api/meetings", headers=tok(user)).json()["meetings"])
        assert get(env, m["id"], user).status_code == 404
    assert env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("owner")).status_code == 404
    with env_conn() as conn:
        row = conn.execute("SELECT deleted_at, deleted_by FROM meetings WHERE id = ?", (m["id"],)).fetchone()
        assert row["deleted_at"] and row["deleted_by"]
        assert conn.execute("SELECT COUNT(*) FROM meeting_attempts WHERE meeting_id = ?", (m["id"],)).fetchone()[0] > 0


def test_a_draft_can_be_deleted_too(env):
    m = create(env)
    assert act(env, m["id"], "delete").status_code == 200
    assert get(env, m["id"]).status_code == 404
