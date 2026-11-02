# Historical reference only. Not integrated or supported for deployment. See README.md.
from __future__ import annotations

import os
import subprocess
import time
import json
import shutil
from pathlib import Path
from typing import Any


def resolve_openclaw_binary() -> str:
    configured = os.environ.get("OPENCLAW_BINARY")
    if configured:
        return configured
    openclaw = shutil.which("openclaw")
    if openclaw:
        return openclaw
    return "openclaw"


def run_openclaw_adapter(task: dict[str, Any]) -> dict[str, Any]:
    valid_routes = {
        "agent-c.openclaw.workstation": ("agent-c", "openclaw", "route-c"),
        "agent-d.openclaw.workstation": ("agent-d", "openclaw", "route-d"),
    }

    instance_id = task.get("instance_id")
    if instance_id not in valid_routes:
        raise ValueError("unsupported openclaw instance")

    expected_agent, expected_adapter, expected_route = valid_routes[instance_id]
    if task.get("agent_id") != expected_agent or task.get("adapter") != expected_adapter or task.get("route_key") != expected_route:
        raise ValueError("openclaw route mismatch")
    if task.get("permission_policy") != "deny_interactive":
        raise ValueError("interactive permission policy is not allowed")

    prompt = task["prompt"]
    session_ref = task.get("provider_session_ref")
    if not session_ref:
        session_ref = f"meeting-room-{task.get('session_id', 'default')}"

    timeout_seconds = int(task.get("timeout_seconds") or 600)
    started = time.monotonic()

    # Always use the "host" profile based on system topography
    profile = "host"
    agent_name = expected_route

    cmd = [
        resolve_openclaw_binary(),
        "--profile", profile,
        "agent",
        "--agent", agent_name,
        "--local",
        "--json",
        "--session-id", session_ref,
        "-m", prompt
    ]

    print(f"[Worker] Using Subprocess transport for session: {session_ref}")
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
    )

    exit_code = result.returncode
    body_text = None

    if exit_code == 0:
        stdout = result.stdout.strip()

        # Strategy 1: Try parsing the entire output as JSON (openclaw --json outputs multi-line JSON)
        try:
            parsed = json.loads(stdout)
            # Format A: {"result": {"finalAssistantVisibleText": "..."}}
            if "result" in parsed and "finalAssistantVisibleText" in parsed["result"]:
                body_text = parsed["result"]["finalAssistantVisibleText"]
            # Format B: {"payloads": [{"text": "..."}], "meta": {...}}
            elif "payloads" in parsed and isinstance(parsed["payloads"], list) and len(parsed["payloads"]) > 0:
                body_text = parsed["payloads"][0].get("text")
            # Format C: {"finalAssistantVisibleText": "..."} at top level
            elif "finalAssistantVisibleText" in parsed:
                body_text = parsed["finalAssistantVisibleText"]
            else:
                body_text = parsed.get("response") or parsed.get("message") or parsed.get("reply") or parsed.get("content")
        except json.JSONDecodeError:
            pass

        # Strategy 2: Line-by-line fallback (for single-line JSON outputs)
        if body_text is None:
            lines = stdout.splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        if "result" in parsed and "finalAssistantVisibleText" in parsed["result"]:
                            body_text = parsed["result"]["finalAssistantVisibleText"]
                        elif "payloads" in parsed and isinstance(parsed["payloads"], list) and len(parsed["payloads"]) > 0:
                            body_text = parsed["payloads"][0].get("text")
                        else:
                            body_text = parsed.get("response") or parsed.get("message") or parsed.get("reply") or parsed.get("content")
                        if body_text is not None:
                            break
                    except json.JSONDecodeError:
                        continue

        # Strategy 3: Raw output fallback
        if body_text is None:
            body_text = stdout
    else:
        body_text = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"openclaw exited {exit_code}: {body_text[-1000:]}")

    duration_ms = int((time.monotonic() - started) * 1000)

    max_chars = int(task.get("max_output_chars") or 2000)
    output_truncated = len(body_text) > max_chars
    if output_truncated:
        body_text = body_text[:max_chars]

    print(f"[Worker] Diagnostics: transport=subprocess, duration_ms={duration_ms}")

    return {
        "body": body_text,
        "provider_session_ref": session_ref,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "output_truncated": output_truncated,
    }
