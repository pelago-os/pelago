"""Meeting modes (contract 5.1, v0.2.4): the old room's agenda protocols as fixed lists of rounds.

Pure data and pure functions. A mode's rounds each have a name and an instruction; a blind round hides
the other agents' turns of that round from the prompt. Every mode except `discussion` ends with the wrap-up.
"""
from __future__ import annotations

from typing import Any

DISCUSSION = "discussion"

PERSPECTIVES = (
    ("可行性与工作量", "Judge technical feasibility, staffing, order of work and how much effort delivery takes."),
    ("架构与风险", "Judge the impact on the architecture, migration, security, upkeep and how it can fail."),
    ("用户价值与体验", "Judge the value to the people who will use it, their workflow, ease of use, and whether it should exist."),
)

MODES: dict[str, dict[str, Any]] = {
    DISCUSSION: {"label": "自由讨论", "rounds": None, "wrap_focus": None},
    "alignment": {
        "label": "项目对齐",
        "rounds": [
            {"name": "各自盘点", "blind": True,
             "instruction": "Independently state your own part of the work: inputs, dependencies, deliverables, "
                            "how we will know it is done, and risks. Do not assume someone else covers missing work."},
            {"name": "互相查漏", "blind": False,
             "instruction": "Review everyone's inventory. Name ownership gaps, duplicated work and order conflicts, "
                            "and propose at least one concrete change. Do not merely agree."},
        ],
        "wrap_focus": "Turn it into an executable plan: who does what, in which order, how each part is checked; "
                      "gaps nobody took are listed under 分歧与未决 as 待确认.",
    },
    "brainstorm": {
        "label": "头脑风暴",
        "rounds": [
            {"name": "各自盲写", "blind": True,
             "instruction": "Without seeing anyone else, propose two or three directions that differ from each other. "
                            "Go for range, not polish."},
            {"name": "拉开差异", "blind": False,
             "instruction": "Read the ideas so far. Do not repeat or merely praise them: push one further in a new "
                            "direction, combine two, or name what nobody has covered yet."},
        ],
        "wrap_focus": "Group the ideas into themes, keep the unusual ones visible, then name the two or three "
                      "directions worth pursuing and why.",
    },
    "initiation": {
        "label": "项目立项",
        "perspectives": True,
        "rounds": [
            {"name": "独立评估", "blind": True,
             "instruction": "Assess the proposal from your perspective alone, before seeing anyone else's view."},
            {"name": "交叉质询", "blind": False,
             "instruction": "Challenge the other assessments from your perspective. Ask the hardest question you have."},
            {"name": "表态", "blind": False,
             "instruction": "State your position: go, no-go, or go with conditions, with your single strongest reason."},
        ],
        "wrap_focus": "Make the call as chair: go, no-go, or go with conditions. Record the reasons and any dissent; "
                      "do not invent agreement.",
    },
}


def mode_rounds(mode: str) -> int | None:
    rounds = MODES[mode]["rounds"]
    return len(rounds) if rounds else None


def stage(mode: str | None, round_no: int) -> dict[str, Any] | None:
    """The named round for this mode, or None for `discussion` and the wrap-up."""
    rounds = MODES.get(mode or DISCUSSION, MODES[DISCUSSION])["rounds"]
    if not rounds or not 1 <= round_no <= len(rounds):
        return None
    return rounds[round_no - 1]


def perspective(role: str | None) -> tuple[str, str] | None:
    return next((p for p in PERSPECTIVES if p[0] == role), None)
