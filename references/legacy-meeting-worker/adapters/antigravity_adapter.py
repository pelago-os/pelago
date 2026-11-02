# Historical reference only. Not integrated or supported for deployment. See README.md.
from __future__ import annotations

import os
import subprocess
import time
import json
import shutil
from pathlib import Path
from typing import Any


def resolve_agy_binary() -> str:
    configured = os.environ.get("AGY_BINARY")
    if configured:
        return configured
    agy = shutil.which("agy")
    if agy:
        return agy
    return "agy"


def _resolve_runtime_dir(session_id: str, generation: int) -> Path:
    """Create and return a meeting-specific runtime directory for agy."""
    safe_session_id = session_id.replace("/", "_").replace("\\", "_")
    runtime_dir = Path.home() / ".example-meeting-worker" / "runtime" / "agy-sessions" / safe_session_id / f"g{generation}"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _find_conversation_id(runtime_dir: Path) -> str | None:
    """Look up conversation id from agy's local cache keyed by runtime cwd."""
    cache_path = Path.home() / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # The cache is keyed by the cwd used when agy was invoked.
    # Try exact match on runtime_dir, then parent paths.
    runtime_str = str(runtime_dir)
    if runtime_str in data:
        entry = data[runtime_str]
        if isinstance(entry, dict):
            return entry.get("conversation_id") or entry.get("id")
        if isinstance(entry, str):
            return entry
    # Try without trailing slash
    if runtime_str.rstrip("/") in data:
        entry = data[runtime_str.rstrip("/")]
        if isinstance(entry, dict):
            return entry.get("conversation_id") or entry.get("id")
        if isinstance(entry, str):
            return entry
    return None


def run_antigravity_adapter(task: dict[str, Any]) -> dict[str, Any]:
    valid_routes = {
        "agent-e.antigravity.workstation": ("agent-e", "antigravity", "agent-e"),
    }

    instance_id = task.get("instance_id")
    if instance_id not in valid_routes:
        raise ValueError("unsupported antigravity instance")

    expected_agent, expected_adapter, expected_route = valid_routes[instance_id]
    if task.get("agent_id") != expected_agent or task.get("adapter") != expected_adapter or task.get("route_key") != expected_route:
        raise ValueError("antigravity route mismatch")
    if task.get("permission_policy") != "deny_interactive":
        raise ValueError("interactive permission policy is not allowed")

    prompt = task["prompt"]
    session_id = task.get("session_id", "default")
    generation = int(task.get("context_generation") or 0)
    timeout_seconds = int(task.get("timeout_seconds") or 600)
    started = time.monotonic()

    # Set up meeting-specific runtime directory
    runtime_dir = _resolve_runtime_dir(session_id, generation)

    # Check for existing conversation to resume
    conversation_id = _find_conversation_id(runtime_dir)

    # Build command
    cmd = [resolve_agy_binary(), f"--print={prompt}"]
    cmd.append(f"--print-timeout={timeout_seconds}s")
    if conversation_id:
        cmd.append(f"--conversation={conversation_id}")

    print(f"[Worker] Using Subprocess transport for session: {session_id}, runtime: {runtime_dir}")

    env = os.environ.copy()
    env["AGY_CLI_HIDE_ACCOUNT_INFO"] = "1"
    env["PATH"] = str(Path.home() / ".local") + ":" + env.get("PATH", "")

    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
        cwd=str(runtime_dir),
        env=env,
    )

    exit_code = result.returncode
    body_text = (result.stdout or "").strip()

    if exit_code != 0:
        stderr_text = (result.stderr or "").strip()
        raise RuntimeError(f"agy exited {exit_code}: {(body_text or stderr_text)[-1000:]}")

    if not body_text:
        raise RuntimeError("agy returned empty output")

    # Look up the conversation id after the run for continuity
    provider_session_ref = _find_conversation_id(runtime_dir)
    if not provider_session_ref:
        provider_session_ref = f"meeting-room-{session_id}"

    duration_ms = int((time.monotonic() - started) * 1000)

    max_chars = int(task.get("max_output_chars") or 2000)
    output_truncated = len(body_text) > max_chars
    if output_truncated:
        body_text = body_text[:max_chars]

    print(f"[Worker] Diagnostics: transport=subprocess, duration_ms={duration_ms}")

    return {
        "body": body_text,
        "provider_session_ref": provider_session_ref,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "output_truncated": output_truncated,
    }
