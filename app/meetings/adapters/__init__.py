"""Mock and explicitly opted-in experimental meeting adapters."""
from __future__ import annotations
import json
import os
from typing import Any
from app.meetings.adapter_config import MemberAdapter
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")

def child_env(member: MemberAdapter) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.update(member.env)  # only what the administrator named on purpose
    return env


WRAP_UP_INSTRUCTIONS = """You are now wrapping up this meeting for the people who called it. Write in the language the meeting used.
Use exactly these five Markdown sections, in this order:
## 讨论摘要
## 达成的决定
## 分歧与未决
## 行动项
## 给主持人的简报
Rules:
- Only record what was actually said. A decision counts only if the participants clearly agreed; otherwise list it under 分歧与未决.
- Each action item: what, owner, deliverable, how we know it is done, deadline. Anything nobody stated is written as 待确认. Never assign work nobody took on, and never approve spending or deployments on anyone's behalf.
- Lines from humans in the room take precedence: when a human corrected or overruled something, follow the human.
- Cite the turns you rely on as (round N, Name), copying N and Name exactly from the transcript labels below; for a human line use its round and name too.
- At most {limit} characters. Do not use tools."""

def format_prompt(ctx: dict[str, Any]) -> str:
    m, speaker = ctx["meeting"], ctx["speaker"]
    if ctx.get("wrap_up"):
        return _format_wrap_up(ctx)
    lines = [f"You are {speaker['display_name']} in a meeting titled \"{m['title']}\".",
             f"Round {ctx['attempt']['round']} of {m['rounds']}."]
    lines += _mode_lines(ctx)
    if m.get("topic"):
        lines += ["Topic:", m["topic"]]
    if not ctx["exempt"]:
        lines.append(f"Answer in at most {m['max_chars']} characters. Longer answers are cut off.")
    for item in ctx["materials"]:
        lines += [f"--- material: {item['path']} ---", item["body"]]
    if ctx["turns"]:
        lines.append("--- what has been said ---")
        lines += [f"[round {t['round']}] {t['speaker']} (human, in the room): {t['text']}" if t.get("human")
                  else f"[round {t['round']}] {t['speaker']}: {t['text']}" for t in ctx["turns"]]
        if any(t.get("human") for t in ctx["turns"]):
            lines.append("Lines marked (human, in the room) were said by people at the meeting between turns; "
                         "you may answer them in your turn.")
    lines.append("Reply with your turn only, speaking only for yourself: never write lines, quotes or replies "
                 "for other participants. Do not use tools.")
    return "\n".join(lines)


def _mode_lines(ctx: dict[str, Any]) -> list[str]:
    """Contract 5.1: the mode, this round's name and instruction, blindness and the speaker's perspective."""
    from app.meetings.modes import MODES
    mode = MODES.get(ctx["meeting"].get("mode") or "discussion")
    stage, view = ctx.get("stage"), ctx.get("perspective")
    if not mode or not mode["rounds"]:
        return []
    lines = [f"This is a {mode['label']} meeting."]
    if view:
        lines.append(f"Your perspective here is {view[0]}: {view[1]}")
    if stage:
        lines.append(f"This round is {stage['name']}: {stage['instruction']}")
        if stage["blind"]:
            lines.append("This round is blind: you cannot see what the others write in it, and they cannot see yours "
                         "until it ends. Write your own view, not a reply.")
    return lines


def _format_wrap_up(ctx: dict[str, Any]) -> str:
    """The wrap-up slot (contract section 5): the whole meeting, then the wrap-up template."""
    from app.meetings.states import WRAP_MAX_CHARS
    m, speaker = ctx["meeting"], ctx["speaker"]
    lines = [f"You are {speaker['display_name']}. The meeting titled \"{m['title']}\" has finished its rounds."]
    from app.meetings.modes import MODES
    mode = MODES.get(m.get("mode") or "discussion")
    if mode and mode["wrap_focus"]:
        lines.append(f"It was a {mode['label']} meeting, and you sum it up as its chair. {mode['wrap_focus']}")
    if m.get("topic"):
        lines += ["Topic:", m["topic"]]
    for item in ctx["materials"]:
        lines += [f"--- material: {item['path']} ---", item["body"]]
    lines.append("--- what has been said ---")
    lines += [f"[round {t['round']}] {t['speaker']} (human, in the room): {t['text']}" if t.get("human")
              else f"[round {t['round']}] {t['speaker']}: {t['text']}" for t in ctx["turns"]] or ["(nothing was said)"]
    lines.append(WRAP_UP_INSTRUCTIONS.format(limit=WRAP_MAX_CHARS))
    return "\n".join(lines)


def adapter_rev() -> dict:
    """Which code produced a receipt: git HEAD, whether the tree was dirty, and a hash of the
    meetings sources as they are on disk right now (a dirty tree is never passed off as HEAD)."""
    import hashlib
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    src = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for f in sorted(src.rglob("*.py")):
        digest.update(str(f.relative_to(src)).encode())
        digest.update(f.read_bytes())

    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    return {"head": git("rev-parse", "--short", "HEAD") or "unknown",
            "dirty": bool(git("status", "--porcelain", "--", "app/meetings")),
            "meetings_src_sha256": digest.hexdigest()[:16]}


def _check_served(member: MemberAdapter, served: list[str], base: dict) -> dict | None:
    bad = [m for m in served if member.models and m not in member.models]
    if bad:
        return dict(base, status="failed", error_code="model_mismatch", detail=f"served by {', '.join(bad)}",
                    model=",".join(served))
    return None



def _require_mock(member):
    if member.adapter != "mock":
        raise ValueError("Only mock is supported in this P1 checkpoint")


def build_command(member, ctx, external=None):
    if member.adapter == "hermes":
        from . import hermes_runtime
        argv, env, cwd = hermes_runtime.build(member, ctx, external)
        return argv, env, cwd, format_prompt(ctx)
    if member.adapter == "claude_code":
        from .claude_runtime import build
        argv, env, cwd = build(member, ctx, external)
        return argv, env, cwd, format_prompt(ctx)
    _require_mock(member)
    return list(member.command), child_env(member), member.cwd, format_prompt(ctx)


def launch_external_snapshot(member, attempt_id):
    if member.adapter == "hermes":
        from . import hermes_runtime
        return hermes_runtime.snapshot(member, attempt_id)
    if member.adapter == "claude_code":
        from .claude_runtime import snapshot
        return snapshot(member, attempt_id)
    _require_mock(member)
    return {"kind": "none", "adapter": "mock"}


def external_units_gone(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "none":
        return False
    if snapshot.get("adapter") == "mock":
        return True
    if snapshot.get("adapter") == "hermes":
        from .hermes_runtime import valid_snapshot
        return valid_snapshot(snapshot)
    if snapshot.get("adapter") == "claude_code":
        from .claude_runtime import valid_snapshot
        return valid_snapshot(snapshot)
    return False


def cleanup_after(member, stdout):
    return []


def parse_result(member, stdout, stderr, exit_code, requested_model=None, execution_snapshot=None):
    if member.adapter == "hermes":
        from . import hermes_runtime
        result = hermes_runtime.finish(member, stdout, exit_code, requested_model, execution_snapshot)
        result["adapter_rev"] = adapter_rev()
        return result
    if member.adapter == "claude_code":
        from .claude_runtime import finish
        result = finish(member, stdout, exit_code, requested_model, execution_snapshot)
        result["adapter_rev"] = adapter_rev()
        return result
    _require_mock(member)
    base = {"provider": "mock", "support_tier": "mock", "requested_model": requested_model, "adapter_rev": adapter_rev()}
    if exit_code:
        return dict(base, status="failed", error_code="exit_status", detail=f"exit {exit_code}")
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not isinstance(data.get("model"), str) or not data["model"]:
        return dict(base, status="failed", error_code="missing_evidence", detail="invalid mock receipt")
    rejected = _check_served(member, [data["model"]], base)
    return rejected or dict(base, status="accepted", text=data["text"], model=data["model"], provider_session_id=None)
