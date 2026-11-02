# Historical reference only. Not integrated or supported for deployment. See README.md.
from __future__ import annotations

import os
import subprocess
import time
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


def resolve_hermes_binary() -> str:
    configured = os.environ.get("HERMES_BINARY")
    if configured:
        return configured
    default = Path.home() / ".local" / "bin" / "hermes"
    if default.exists():
        return str(default)
    return "hermes"


def run_hermes_adapter(task: dict[str, Any]) -> dict[str, Any]:
    valid_routes = {
        "agent-a.hermes.workstation": ("agent-a", "hermes", "agent-a"),
        "agent-b.hermes.workstation": ("agent-b", "hermes", "agent-b"),
    }

    instance_id = task.get("instance_id")
    if instance_id not in valid_routes:
        raise ValueError("unsupported hermes instance")

    expected_agent, expected_adapter, expected_route = valid_routes[instance_id]
    if task.get("agent_id") != expected_agent or task.get("adapter") != expected_adapter or task.get("route_key") != expected_route:
        raise ValueError("hermes route mismatch")
    if task.get("permission_policy") != "deny_interactive":
        raise ValueError("interactive permission policy is not allowed")

    prompt = task["prompt"]
    session_ref = task.get("provider_session_ref")
    if not session_ref:
        session_ref = f"meeting-room-{task.get('session_id', 'default')}"

    timeout_seconds = int(task.get("timeout_seconds") or 30)
    started = time.monotonic()

    agent_id = task.get("agent_id", "agent-a")
    gateway_url = os.environ.get(f"HERMES_GATEWAY_URL_{agent_id.upper()}") or os.environ.get("HERMES_GATEWAY_URL")
    gateway_key = os.environ.get(f"HERMES_GATEWAY_KEY_{agent_id.upper()}") or os.environ.get("HERMES_GATEWAY_KEY", "")

    body_text = None
    exit_code = 0
    transport = "gateway"

    if gateway_url:
        print(f"[Worker] Attempting Gateway transport: {gateway_url} for session: {session_ref}")
        req_body = json.dumps({
            "model": "hermes-default",
            "messages": [{"role": "user", "content": prompt}],
            "user": session_ref,
            "session_id": session_ref
        }).encode("utf-8")

        req = urllib.request.Request(gateway_url, data=req_body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {gateway_key}" if gateway_key else ""
        })

        import socket
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                resp_data = json.loads(resp.read())
                body_text = resp_data["choices"][0]["message"]["content"]
                print(f"[Worker] Gateway success. Latency: {time.monotonic() - started:.2f}s")
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise TimeoutError(f"Gateway read timed out: {e}")
            print(f"[Worker] Gateway failed ({e}), falling back to subprocess.")
            transport = "subprocess"
        except TimeoutError as e:
            raise TimeoutError(f"Gateway read timed out: {e}")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Gateway returned malformed JSON response: {e}")
        except Exception as e:
            print(f"[Worker] Gateway failed ({e}), falling back to subprocess.")
            transport = "subprocess"
    else:
        transport = "subprocess"

    if transport == "subprocess":
        print(f"[Worker] Using Subprocess transport for session: {session_ref}")
        result = subprocess.run(
            [
                resolve_hermes_binary(),
                "--profile",
                task.get("agent_id", "agent-a"),
                "-z",
                prompt,
                "-c",
                session_ref,
                "--yolo",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 10,
        )

        body_text = (result.stdout or "").strip()
        if not body_text:
            body_text = (result.stderr or "").strip()

        if result.returncode != 0:
            raise RuntimeError(f"hermes exited {result.returncode}: {body_text[-1000:]}")
        exit_code = result.returncode

    duration_ms = int((time.monotonic() - started) * 1000)

    max_chars = int(task.get("max_output_chars") or 2000)
    output_truncated = len(body_text) > max_chars
    if output_truncated:
        body_text = body_text[:max_chars]

    print(f"[Worker] Diagnostics: transport={transport}, duration_ms={duration_ms}")

    return {
        "body": body_text,
        "provider_session_ref": session_ref,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "output_truncated": output_truncated,
    }
