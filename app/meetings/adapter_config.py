"""Administrator-owned adapter file (contract section 10). Requests never carry commands."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# exact names only: no wildcards or prefixes; "/" and ":" appear in provider-qualified names
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9./:\-]{1,120}$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")  # backend model_enum values, e.g. MODEL_PLACEHOLDER_M320
ADAPTER_TYPES = frozenset({"mock", "claude_code", "hermes"})
ROUTED_ADAPTERS = {"claude_code": frozenset({"ANTHROPIC"}),
                   "hermes": frozenset({"NVIDIA", "DEEPSEEK", "OPENROUTER", "GEMINI", "OPENAI_CODEX"})}


@dataclass(frozen=True)
class MemberAdapter:
    username: str
    adapter: str
    command: tuple[str, ...]
    cwd: str
    models: tuple[str, ...] = ()
    default_model: str = ""
    timeout_s: int = 120
    config_dir: str = ""
    experimental: bool = False
    policy_audited: bool = False
    runtime_root: str = ""
    runtime_manifest: str = ""
    runtime_manifest_sha256: str = ""
    home_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    # Reserved configuration fields for later adapter verification; no cleanup in P1.
    cleanup_conversations: bool = False
    # Explicitly approved runtime and meeting agent; never learned from the installed binary.
    runtime_version: str = ""
    runtime_sha256: str = ""
    agent_sha256: str = ""
    # requested model -> names the provider reports back for it, each one seen in a real response (no patterns)
    # what the backend may report for a request, as exact (served model, model_enum) pairs taken from evidence
    served_routes: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)


class AdapterConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentMeetingPolicy:
    """What an agent may start without approval (contract section 4). Absent table: nothing."""
    auto_start_models: tuple[str, ...] = ()
    # the people (not agents) on this forum, by username; empty means only administrators count as people
    people: tuple[str, ...] = ()
    max_agents: int = 0
    max_rounds: int = 0
    max_meeting_timeout_s: int = 0
    per_day: int = 0


def _parse_policy(data: dict) -> AgentMeetingPolicy:
    table = data.get("agent_meetings")
    if table is None:
        return AgentMeetingPolicy()
    if not isinstance(table, dict):
        raise AdapterConfigError("agent_meetings must be a table")
    models = table.get("auto_start_models") or []
    if not isinstance(models, list) or not all(isinstance(m, str) and _MODEL_ID.match(m) for m in models):
        raise AdapterConfigError("agent_meetings.auto_start_models must list exact model names")
    people = table.get("people") or []
    if not isinstance(people, list) or not all(isinstance(u, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", u) for u in people):
        raise AdapterConfigError("agent_meetings.people must list forum usernames")
    numbers = {}
    for key in ("max_agents", "max_rounds", "max_meeting_timeout_s", "per_day"):
        value = table.get(key, 0)
        if type(value) is not int or value < 0:
            raise AdapterConfigError(f"agent_meetings.{key} must be a non-negative integer")
        numbers[key] = value
    return AgentMeetingPolicy(auto_start_models=tuple(models), people=tuple(people), **numbers)


def _parse(data: dict) -> dict[str, MemberAdapter]:
    members = {}
    for username, entry in (data.get("members") or {}).items():
        adapter = entry.get("adapter", "")
        if adapter not in ADAPTER_TYPES:
            raise AdapterConfigError(f"members.{username}.adapter must be one of {sorted(ADAPTER_TYPES)}")
        if adapter in ("claude_code", "hermes"):
            if entry.get("experimental") is not True or entry.get("policy_audited") is not True:
                raise AdapterConfigError("Experimental adapter requires explicit experimental and policy_audited opt-in")
            for key in ("runtime_root", "runtime_manifest", "home_dir", "config_dir"):
                if not isinstance(entry.get(key), str) or not Path(entry[key]).is_absolute():
                    raise AdapterConfigError(f"Experimental adapter requires an absolute {key}")
            if not isinstance(entry.get("runtime_version"), str) or not entry["runtime_version"]:
                raise AdapterConfigError("Experimental adapter requires an approved runtime_version label")
            if not isinstance(entry.get("runtime_manifest_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["runtime_manifest_sha256"]):
                raise AdapterConfigError("Experimental adapter requires an explicitly approved manifest digest")
            if entry.get("env") or entry.get("cleanup_conversations"):
                raise AdapterConfigError("Experimental adapter does not accept extra environment or cleanup settings")
        command = tuple(entry.get("command") or ())
        if not command or not Path(command[0]).is_absolute():
            raise AdapterConfigError(f"members.{username}.command must start with an absolute path")
        if adapter in ("claude_code", "hermes") and len(command) != 1:
            raise AdapterConfigError("Experimental adapter command must name only the pinned entrypoint, with no custom flags")
        cwd = str(entry.get("cwd", "")).strip()
        if not cwd or not Path(cwd).is_absolute():
            raise AdapterConfigError(f"members.{username}.cwd must be an absolute directory used only for meetings")
        if entry.get("tools", False):
            raise AdapterConfigError(f"members.{username}.tools: v0.2 adapters run with tools disabled")
        models = tuple(entry.get("models") or ())
        if adapter in ("claude_code", "hermes") and (not models or any(not isinstance(m, str) or not _MODEL_ID.fullmatch(m) for m in models)):
            raise AdapterConfigError("Experimental adapter requires explicit valid model names")
        default_model = str(entry.get("default_model", "") or (models[0] if models else ""))
        if models and default_model not in models:
            raise AdapterConfigError(f"members.{username}.default_model must be listed in models")
        env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        if "served_aliases" in entry:
            raise AdapterConfigError(f"members.{username}.served_aliases was replaced by served_routes "
                                     "(exact [served model, model_enum] pairs)")
        routes = {}
        for requested, pairs in (entry.get("served_routes") or {}).items():
            if requested not in models:
                raise AdapterConfigError(f"members.{username}.served_routes.{requested} is not one of its models")
            if (not isinstance(pairs, list) or not pairs or not all(
                    isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) and _MODEL_ID.match(p[0])
                    and isinstance(p[1], str) and _ENUM.match(p[1]) for p in pairs)):
                raise AdapterConfigError(f"members.{username}.served_routes.{requested} must list exact "
                                         "[served model, model_enum] pairs")
            routes[requested] = tuple((p[0], p[1]) for p in pairs)
        for model in models if adapter in ROUTED_ADAPTERS else ():
            bound = {route for _, route in routes.get(model, ())}
            if len(bound) != 1 or not bound <= ROUTED_ADAPTERS[adapter]:
                raise AdapterConfigError(f"members.{username}.served_routes.{model} must name exactly one route of "
                                         f"{sorted(ROUTED_ADAPTERS[adapter])}")
        members[username] = MemberAdapter(
            username=username, adapter=adapter, command=command, cwd=cwd, models=models,
            default_model=default_model, timeout_s=int(entry.get("timeout_s", 120)),
            config_dir=str(entry.get("config_dir", "") or ""), env=env,
            experimental=entry.get("experimental") is True, policy_audited=entry.get("policy_audited") is True,
            runtime_root=str(entry.get("runtime_root", "")), runtime_manifest=str(entry.get("runtime_manifest", "")),
            runtime_manifest_sha256=str(entry.get("runtime_manifest_sha256", "")), home_dir=str(entry.get("home_dir", "")),
            cleanup_conversations=bool(entry.get("cleanup_conversations", False)), served_routes=routes,
            runtime_version=str(entry.get("runtime_version", "")),
            runtime_sha256=str(entry.get("runtime_sha256", "")),
            agent_sha256=str(entry.get("agent_sha256", "")))
    return members


_cache: dict[str, tuple[float, dict[str, MemberAdapter], AgentMeetingPolicy]] = {}


def load_members() -> dict[str, MemberAdapter]:
    """Read PELAGO_MEETING_ADAPTERS; an unset variable means no agents are configured."""
    return _load()[0]


def load_agent_meeting_policy() -> AgentMeetingPolicy:
    return _load()[1]


def _load() -> tuple[dict[str, MemberAdapter], AgentMeetingPolicy]:
    path_text = os.environ.get("PELAGO_MEETING_ADAPTERS", "").strip()
    if not path_text:
        return {}, AgentMeetingPolicy()
    path = Path(path_text).expanduser()
    try:
        info = path.stat()
    except FileNotFoundError:
        raise AdapterConfigError(f"adapter file not found: {path}") from None
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or info.st_uid != os.geteuid():
        raise AdapterConfigError("adapter file must be owned by the operator and not writable by others")
    parent_info = path.resolve().parent.stat()
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AdapterConfigError("adapter parent directory must be controlled by the operator")
    cached = _cache.get(str(path))
    if cached and cached[0] == info.st_mtime:
        return cached[1], cached[2]
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    members, policy = _parse(data), _parse_policy(data)
    _cache[str(path)] = (info.st_mtime, members, policy)
    return members, policy
