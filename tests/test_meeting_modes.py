"""Meeting modes, blind rounds, perspectives (contract 5.1) and rejecting a meeting with a reason (section 4)."""
import pytest

from test_meetings import (SMALL, _policy, _start, act, calls, control, create, drive, env, get, rid, say, state,  # noqa: F401
                           tok)


def patch(e, mid, user="moderator", **body):
    return e.client.patch(f"/api/meetings/{mid}", headers=tok(user), json={
        "request_id": rid(), "expected_version": state(e, mid)["version"], **body})


def finish(e, mid):
    drive(e, lambda: state(e, mid)["state"] == "completed")


def test_alignment_first_round_is_blind_and_the_second_is_not(env):
    control(env, "agent_alpha", text="CHE-INVENTORY")
    control(env, "agent_beta", text="CHEN-INVENTORY")
    m = create(env, mode="alignment", wrap_up=True, wrap_up_by="agent_beta")
    assert m["settings"]["rounds"] == 2 and m["settings"]["mode"] == "alignment"
    assert act(env, m["id"], "start").status_code == 200
    finish(env, m["id"])
    agent_alpha, agent_beta = calls(env, "agent_alpha"), calls(env, "agent_beta")
    assert len(agent_alpha) == 2 and len(agent_beta) == 3  # agent_beta also wraps up
    # round 1: agent_beta speaks after agent_alpha but must not see agent_alpha's turn
    assert "CHE-INVENTORY" not in agent_beta[0]["prompt"]
    assert "This round is 各自盘点" in agent_beta[0]["prompt"] and "This round is blind" in agent_beta[0]["prompt"]
    assert "This is a 项目对齐 meeting." in agent_alpha[0]["prompt"]
    # round 2 sees round 1, and is not blind
    assert "CHE-INVENTORY" in agent_beta[1]["prompt"] and "CHEN-INVENTORY" in agent_alpha[1]["prompt"]
    assert "This round is 互相查漏" in agent_beta[1]["prompt"] and "This round is blind" not in agent_beta[1]["prompt"]
    # the wrap-up sums it up as the chair, with the mode's focus
    assert "sum it up as its chair" in agent_beta[2]["prompt"] and "executable plan" in agent_beta[2]["prompt"]
    minutes = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("owner")).json()["latest"]["body_md"]
    assert "模式：项目对齐" in minutes


def test_people_in_the_room_are_heard_in_a_blind_round(env):
    control(env, "agent_alpha", text="CHE-IDEA")
    m = create(env, mode="brainstorm", wrap_up=True, wrap_up_by="agent_alpha")
    act(env, m["id"], "start")
    assert say(env, m["id"], "别忘了笔友那边", user="owner").status_code == 200
    finish(env, m["id"])
    first = calls(env, "agent_alpha")[0]["prompt"]
    assert "别忘了笔友那边" in first and "This round is 各自盲写" in first
    assert "CHE-IDEA" not in calls(env, "agent_beta")[0]["prompt"]


def test_initiation_gives_public_perspectives_in_seat_order(env):
    m = create(env, mode="initiation", participants=("agent_alpha", "agent_beta", "agent_gamma"), wrap_up=True, wrap_up_by="agent_gamma")
    assert m["settings"]["rounds"] == 3
    act(env, m["id"], "start")
    roles = {p["username"]: p["role"] for p in state(env, m["id"])["participants"]}
    assert roles == {"moderator": None, "agent_alpha": "可行性与工作量", "agent_beta": "架构与风险", "agent_gamma": "用户价值与体验"}
    finish(env, m["id"])
    assert "Your perspective here is 架构与风险" in calls(env, "agent_beta")[0]["prompt"]
    assert "This round is 表态" in calls(env, "agent_alpha")[2]["prompt"]
    assert "go, no-go, or go with conditions" in calls(env, "agent_gamma")[-1]["prompt"]


def test_a_discussion_prompt_carries_no_mode_lines(env):
    m = create(env, rounds=1)
    assert m["settings"]["mode"] == "discussion"
    act(env, m["id"], "start")
    finish(env, m["id"])
    prompt = calls(env, "agent_alpha")[0]["prompt"]
    assert "This is a" not in prompt and "This round is" not in prompt and "perspective" not in prompt


@pytest.mark.parametrize("settings,field", [
    ({"mode": "alignment", "rounds": 3}, "rounds"),
    ({"mode": "brainstorm", "wrap_up": False}, "wrap_up"),
    ({"mode": "agenda"}, "mode"),
])
def test_modes_refuse_settings_they_do_not_have(env, settings, field):
    body = {"request_id": rid(), "title": "T", "topic": "x", "participants": ["agent_alpha", "agent_beta"], **settings}
    r = env.client.post("/api/meetings", headers=tok("moderator"), json=body)
    assert r.status_code == 422 and field in r.json()["detail"]


def test_mode_is_chosen_in_draft_and_keeps_its_rounds(env):
    m = create(env, rounds=4)
    r = patch(env, m["id"], mode="initiation", wrap_up_by="agent_alpha")
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert (s["mode"], s["rounds"], s["wrap_up"]) == ("initiation", 3, 1)
    assert patch(env, m["id"], rounds=5).status_code == 422
    assert patch(env, m["id"], wrap_up=False).status_code == 422
    assert patch(env, m["id"], rounds=3).status_code == 200  # the same count is fine
    act(env, m["id"], "start")
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in state(env, m["id"])["attempts"]))
    assert patch(env, m["id"], mode="discussion").status_code == 409
    assert patch(env, m["id"], rounds=6).status_code == 422
    back = patch(env, create(env, mode="alignment", wrap_up=True, wrap_up_by="agent_alpha")["id"], mode="discussion", rounds=1)
    assert back.status_code == 200 and back.json()["settings"]["rounds"] == 1


def test_a_late_joiner_gets_the_next_perspective(env):
    m = create(env, mode="initiation", wrap_up=True, wrap_up_by="agent_alpha")
    act(env, m["id"], "start")
    act(env, m["id"], "pause")
    drive(env, lambda: not any(a["state"] == "cancel_requested" for a in state(env, m["id"])["attempts"]))
    assert patch(env, m["id"], add=["agent_gamma"]).status_code == 200
    roles = {p["username"]: p["role"] for p in state(env, m["id"])["participants"]}
    assert roles["agent_gamma"] == "用户价值与体验"


def test_reject_closes_the_draft_and_tells_the_organizer_why(env):
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=4)
    assert _start(env, m["id"], "agent_alpha").status_code == 409
    assert act(env, m["id"], "reject", user="agent_alpha", reason="no").status_code == 403
    assert act(env, m["id"], "reject", user="owner", reason="  ").status_code == 400
    assert act(env, m["id"], "reject", user="owner", reason="长" * 501).status_code == 422
    r = act(env, m["id"], "reject", user="owner", reason="四轮太长，三轮内说清楚再来")
    assert r.status_code == 200, r.text
    view = get(env, m["id"], "agent_alpha").json()
    assert (view["state"], view["exit_reason"]) == ("closed", "rejected")
    vv_name = env.client.get("/api/me", headers=tok("owner")).json().get("display_name") or "owner"
    assert view["decision"] == {"rejected": True, "reason": "四轮太长，三轮内说清楚再来", "by": vv_name}
    listed = env.client.get("/api/meetings", headers=tok("agent_alpha")).json()["meetings"]
    assert next(x for x in listed if x["id"] == m["id"])["decision"]["rejected"] is True
    minutes = env.client.get(f"/api/meetings/{m['id']}/minutes", headers=tok("agent_alpha")).json()["latest"]["body_md"]
    assert "驳回理由：四轮太长，三轮内说清楚再来" in minutes
    assert act(env, m["id"], "reject", user="owner", reason="again").status_code == 409
    assert _start(env, m["id"], "agent_alpha").status_code == 409  # closed for good
    assert calls(env, "agent_alpha") == [] and calls(env, "agent_beta") == []


def test_only_a_draft_waiting_for_approval_can_be_rejected(env):
    _policy(env, SMALL)
    people = create(env, organizer="owner", participants=("agent_alpha", "agent_beta"))
    assert act(env, people["id"], "reject", user="owner", reason="x").status_code == 409  # a person's meeting
    small = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=1)
    assert act(env, small["id"], "approve", user="owner").status_code == 200
    assert act(env, small["id"], "reject", user="owner", reason="x").status_code == 409  # already approved


def test_minutes_label_a_modes_rounds():
    from app.meetings.minutes import build_minutes
    meeting = {"title": "T", "topic": "", "state": "paused", "exit_reason": "user_pause", "organizer_user_id": 1,
               "mode": "brainstorm"}
    parts = [{"seat": 1, "display_name": "A", "kind": "agent", "status": "active", "joined_from_round": 1, "role": None}]
    slots = [{"round": 1, "seat": 1, "state": "filled", "filled_attempt_id": "a1"},
             {"round": 2, "seat": 1, "state": "open", "filled_attempt_id": None}]
    attempts = [{"id": "a1", "round": 1, "seat": 1, "attempt": 1, "state": "accepted", "text": "idea", "ended_at": 1}]
    body = build_minutes(meeting, parts, slots, attempts)
    assert "- mode: 头脑风暴" in body and "### round 1 · 各自盲写（各自写，互不可见）" in body


def test_the_page_names_modes_and_rounds_as_the_server_does():
    """UI contract: the panel keeps its own copy of the mode names; they must not drift from app/meetings/modes.py."""
    import re
    from pathlib import Path
    from app.meetings.modes import MODES
    path = Path(__file__).resolve().parents[1] / "frontend" / "meetings-v2.js"
    if not path.exists():
        pytest.skip("P1 frontend integration pending; backend checkpoint only")
    js = path.read_text(encoding="utf-8")
    block = js.split("const MV2_MODES = {", 1)[1].split("\n};", 1)[0]
    page = {key: (label, re.findall(r"'([^']+)'", rounds) if rounds != "null" else None)
            for key, label, rounds in re.findall(r"(\w+): \{ label: '([^']+)', rounds: (null|\[[^\]]*\])", block)}
    server = {key: (m["label"], [r["name"] for r in m["rounds"]] if m["rounds"] else None) for key, m in MODES.items()}
    assert page == server


# ------------------------------------------------------------------ engineer's backend review (2026-10-22)

@pytest.mark.parametrize("mode", [{}, [], 3, None])
def test_a_mode_that_is_not_text_is_a_bad_request(env, mode):
    body = {"request_id": rid(), "title": "T", "participants": ["agent_alpha", "agent_beta"], "mode": mode}
    assert env.client.post("/api/meetings", headers=tok("moderator"), json=body).status_code == 400  # R1
    m = create(env)
    assert patch(env, m["id"], mode=mode).status_code == 400
    assert patch(env, m["id"], mode="agenda").status_code == 422  # text, but not a mode


def test_a_reject_reason_that_is_not_text_is_a_bad_request(env):
    _policy(env, SMALL)
    m = create(env, organizer="agent_alpha", participants=("agent_beta",), rounds=4)
    assert act(env, m["id"], "reject", user="owner", reason=["no"]).status_code == 400
    assert state(env, m["id"])["state"] == "draft"


def _paused(e, mid):
    act(e, mid, "pause")
    drive(e, lambda: not any(a["state"] == "cancel_requested" for a in state(e, mid)["attempts"]))


def test_someone_removed_in_draft_and_added_back_gets_a_role_and_turns(env):
    """R2: removed before start, so start made no slots; coming back must not leave them silent."""
    m = create(env, mode="initiation", participants=("agent_alpha", "agent_beta", "agent_gamma"), wrap_up=True, wrap_up_by="agent_alpha")
    assert patch(env, m["id"], remove=["agent_gamma"]).status_code == 200
    act(env, m["id"], "start")
    _paused(env, m["id"])
    assert patch(env, m["id"], add=["agent_gamma"]).status_code == 200
    s = state(env, m["id"])
    agent_gamma = next(p for p in s["participants"] if p["username"] == "agent_gamma")
    assert agent_gamma["status"] == "active" and agent_gamma["role"] == "用户价值与体验"
    mine = sorted(x["round"] for x in s["slots"] if x["seat"] == agent_gamma["seat"])
    assert mine == list(range(agent_gamma["joined_from_round"], 4))
    assert all(x["state"] == "open" for x in s["slots"] if x["seat"] == agent_gamma["seat"])


def test_someone_who_left_mid_meeting_comes_back_without_reopening_history(env):
    m = create(env, mode="initiation", participants=("agent_alpha", "agent_beta", "agent_gamma"), wrap_up=True, wrap_up_by="agent_alpha")
    act(env, m["id"], "start")
    drive(env, lambda: any(a["round"] == 2 and a["state"] == "accepted" for a in state(env, m["id"])["attempts"]))
    _paused(env, m["id"])
    before = {(x["round"], x["seat"]): x["state"] for x in state(env, m["id"])["slots"]}
    role = next(p["role"] for p in state(env, m["id"])["participants"] if p["username"] == "agent_beta")
    assert patch(env, m["id"], remove=["agent_beta"]).status_code == 200
    assert patch(env, m["id"], add=["agent_beta"]).status_code == 200
    s = state(env, m["id"])
    agent_beta = next(p for p in s["participants"] if p["username"] == "agent_beta")
    assert agent_beta["role"] == role  # the same perspective, not a new one
    after = {(x["round"], x["seat"]): x["state"] for x in s["slots"]}
    assert len(after) == len(s["slots"])  # no duplicate slot
    for (rnd, seat), was in before.items():
        if was == "filled":
            assert after[(rnd, seat)] == "filled"  # history stays
    assert all(after[(r, agent_beta["seat"])] != "open" for r in range(1, agent_beta["joined_from_round"]))


def test_blind_round_stays_blind_when_a_failed_turn_is_retried(env):
    """engineer's positive case: agent_beta fails in the blind round; on resume the retry still cannot see agent_alpha."""
    control(env, "agent_alpha", text="PRIVATE_FIRST_IDEA")
    control(env, "agent_beta", mode="fail", text="fail")
    m = create(env, mode="brainstorm", wrap_up=True, wrap_up_by="agent_alpha")
    act(env, m["id"], "start")
    drive(env, lambda: state(env, m["id"])["state"] == "paused")
    control(env, "agent_beta", text="retry")
    act(env, m["id"], "resume")
    finish(env, m["id"])
    assert all("PRIVATE_FIRST_IDEA" not in c["prompt"] for c in calls(env, "agent_beta")[:2])
