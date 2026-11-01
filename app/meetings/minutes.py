"""Deterministic minutes (contract section 9). Pure: no model, no clock, no I/O."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

from app.meetings.modes import DISCUSSION, MODES, stage

WRAP_ROUND = 99  # mirrors app.meetings.states.WRAP_ROUND; kept here so this module stays dependency-free
# models writing Chinese often switch to full-width brackets and commas, and group several citations in one
# bracket separated by semicolons: "(round 2, A; round 2, B)". All of these count.
CITATION_GROUP = re.compile(r"[(（]([^()（）\n]*round[^()（）\n]*)[)）]")
CITATION = re.compile(r"round\s+(\d+)\s*[,，]\s*([^;；]+)")


def citations(text: str) -> list[tuple[int, str]]:
    return [(int(r), n.strip()) for group in CITATION_GROUP.findall(text) for r, n in CITATION.findall(group)]


def citation_check(text: str, known: set[tuple[int, str]]) -> list[str]:
    """Citations in a wrap-up that name no turn or interjection. Existence only, never meaning."""
    return sorted({f"(round {r}, {n})" for r, n in citations(text) if (r, n) not in known})


def build_minutes(meeting: Mapping[str, Any], participants: Iterable[Mapping[str, Any]],
                  slots: Iterable[Mapping[str, Any]], attempts: Iterable[Mapping[str, Any]],
                  interjections: Iterable[Mapping[str, Any]] = ()) -> str:
    participants = sorted(participants, key=lambda p: p["seat"])
    by_seat = {p["seat"]: p for p in participants}
    attempts = list(attempts)
    by_id = {a["id"]: a for a in attempts}
    last_problem: dict[tuple[int, int], Mapping[str, Any]] = {}
    for a in sorted(attempts, key=lambda a: (a["round"], a["seat"], a["attempt"])):
        if a["state"] in ("failed", "cancelled", "interrupted", "quarantined"):
            last_problem[(a["round"], a["seat"])] = a

    def name(seat: int) -> str:
        p = by_seat.get(seat)
        return (p.get("display_name") or p.get("username") or f"seat {seat}") if p else f"seat {seat}"

    mode = MODES.get(meeting.get("mode") or DISCUSSION, MODES[DISCUSSION])
    notice = ["> Mock 演示：包含合成测试发言，不是真实模型讨论。", ""] if any(a.get("provider") == "mock" for a in attempts) else []
    if any(a.get("provider") in ("claude_code", "hermes") for a in attempts):
        notice += ["> Experimental adapter：尚未获得正式支持认证；本地退出不证明远端取消或停止计费。", ""]
    lines = [f"# {meeting['title']}", "", *notice]
    if meeting.get("topic"):
        lines += [meeting["topic"], ""]
    complete = meeting["state"] == "completed"
    lines += [
        f"- state: {meeting['state']}",
        f"- exit_reason: {meeting.get('exit_reason') or '-'}",
        f"- incomplete: {'false' if complete else 'true'}",
        f"- organizer: {meeting.get('organizer_name') or meeting['organizer_user_id']}",
        *([f"- mode: {mode['label']}"] if mode["rounds"] else []),
        *([f"- 驳回理由：{meeting['decision_reason']}"] if meeting.get("exit_reason") == "rejected" else []),
        "",
        "## Participants",
        "",
    ]
    for p in participants:
        lines.append(f"- seat {p['seat']}: {name(p['seat'])} ({p['kind']}, {p['status']}, "
                     f"from round {p['joined_from_round']}" + (f", 视角：{p['role']}" if p.get("role") else "") + ")")
    lines += ["", "## Turns", ""]
    ordered = sorted(slots, key=lambda s: (s["round"], s["seat"]))
    last_filled = None
    pending, skipped = [], []
    interjections = list(interjections)
    said = sorted(interjections, key=lambda i: (i["created_at"], i["id"]))
    anything_said = bool(said)

    def human_line(i: Mapping[str, Any]) -> str:
        return f"- round {i['round']} · {i.get('display_name') or i.get('username')} (interjection): {i['text']}"

    wrap_slot = next((s for s in ordered if s["round"] == WRAP_ROUND), None)
    ordered = [s for s in ordered if s["round"] != WRAP_ROUND]
    known = {(int(i["round"]), str(i.get("display_name") or i.get("username"))) for i in said}
    heading = None
    for s in ordered:
        key = (s["round"], s["seat"])
        named = stage(meeting.get("mode"), s["round"])
        if named and heading != s["round"]:  # a mode's rounds have names (contract 5.1)
            heading = s["round"]
            lines.append(f"### round {s['round']} · {named['name']}" + ("（各自写，互不可见）" if named["blind"] else ""))
        if s["state"] == "filled":
            known.add((s["round"], name(s["seat"])))
            a = by_id.get(s["filled_attempt_id"]) or {}
            while said and said[0]["created_at"] < (a.get("ended_at") or 0):
                lines.append(human_line(said.pop(0)))
            mark = " [truncated]" if a.get("truncated") else ""
            served = f" ({a['model']})" if a.get("model") else ""
            lines.append(f"- round {s['round']} · {name(s['seat'])}{served}{mark}: {a.get('text') or ''}")
            last_filled = key
        elif s["state"] == "skipped":
            skipped.append(f"- round {s['round']} · {name(s['seat'])}: skipped ({s.get('skip_reason') or '-'})")
        else:
            problem = last_problem.get(key)
            why = f" (last attempt: {problem.get('error_code') or problem['state']})" if problem else ""
            pending.append(f"- round {s['round']} · {name(s['seat'])}{why}")
    lines += [human_line(i) for i in said]
    if last_filled is None and not anything_said:
        lines.append("- (no turns yet)")
    wrap: list[str] = []
    if wrap_slot is not None:
        wrap_name = name(wrap_slot["seat"])
        wrap += [f"## 会后纪要（{wrap_name} 整理，AI 生成，待确认）", ""]
        if wrap_slot["state"] == "filled":
            a = by_id.get(wrap_slot["filled_attempt_id"]) or {}
            wrap.append((a.get("text") or "") + (" [truncated]" if a.get("truncated") else ""))
            cited = citations(a.get("text") or "")
            missing = citation_check(a.get("text") or "", known)
            check = ("没有找到任何引用，无法对照原发言" if not cited
                     else f"{len(cited)} 处引用都能对上原发言（只核对存在，不保证理解无误）" if not missing
                     else "以下引用找不到对应发言：" + "、".join(missing))
            wrap += ["", "引用核对：" + check]
        elif wrap_slot["state"] == "skipped":
            wrap.append(f"（没有收尾：{wrap_slot.get('skip_reason') or '-'}）")
        else:
            problem = last_problem.get((wrap_slot["round"], wrap_slot["seat"]))
            wrap.append("（还没收尾" + (f"，上次尝试：{problem.get('error_code') or problem['state']}" if problem else "") + "）")
    if pending:
        lines += ["", "## Pending", ""] + pending
    if skipped:
        lines += ["", "## Skipped", ""] + skipped
    lines += ["", f"Last filled slot: {'round %d, seat %d' % last_filled if last_filled else '-'}", ""]
    if wrap_slot is not None and wrap_slot["state"] == "filled":
        # Summary design: once someone has summed it up, the minutes are the summary; who said what in detail stays
        # on the meeting page and in the raw record, not here
        turns = sum(1 for s in ordered if s["state"] == "filled")
        people = "、".join(name(p["seat"]) for p in participants)
        head = [f"# {meeting['title']}", "", *notice]
        meta = (f"- 状态：{meeting['state']}" + ("" if complete else f"（{meeting.get('exit_reason') or '-'}，未完成）")
                + (f" · 模式：{mode['label']}" if mode["rounds"] else "")
                + f" · 主持：{meeting.get('organizer_name') or meeting['organizer_user_id']} · 参会：{people}")
        footer = f"完整发言记录在会议页：{turns} 次发言、{len(interjections)} 次插话，这里不再逐条列出。"
        return "\n".join(head + [meta, ""] + wrap + ["", footer, ""])
    if wrap:  # no summary yet: say so first, then the full record
        at = lines.index("## Participants")
        lines[at:at] = wrap + [""]
    return "\n".join(lines)


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
