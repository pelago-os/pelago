from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8787"


def token_cache() -> Path:
    configured = os.environ.get("PELAGO_FORUM_TOKEN_CACHE")
    if configured:
        return Path(configured).expanduser()
    workspace_cache = Path.cwd() / ".pelago-forum" / "token.json"
    if (Path.cwd() / "skills" / "forum-client").exists() or (
        Path.cwd() / "skills" / "productivity" / "forum-client"
    ).exists():
        return workspace_cache
    install_cache = installed_root() / ".pelago-forum" / "token.json"
    if installed_root() != Path.cwd():
        return install_cache
    return Path.home() / ".pelago-forum" / "token.json"


def installed_root() -> Path:
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if parent.name == "skills":
            return parent.parent
    return Path.cwd()


def base_url(value: str | None = None) -> str:
    return (value or os.environ.get("PELAGO_FORUM_URL") or DEFAULT_BASE_URL).rstrip("/")


def api_json(method: str, url: str, payload: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Forum API error {exc.code}: {detail}") from exc


def save_token(base: str, token: str, username: str) -> None:
    cache = token_cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"base_url": base, "token": token, "username": username}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    cache.chmod(0o600)


def load_token_data() -> dict[str, Any]:
    cache = token_cache()
    if not cache.exists():
        raise SystemExit("Not logged in. Run: python3 skills/forum-client/scripts/forum_login.py --username <name>")
    return json.loads(cache.read_text(encoding="utf-8"))


def load_token() -> tuple[str, str]:
    data = load_token_data()
    return base_url(data["base_url"]), data["token"]


def expected_username(data: dict[str, Any] | None = None) -> str | None:
    configured = os.environ.get("PELAGO_FORUM_EXPECTED_USERNAME")
    if configured:
        return configured
    if data:
        return data.get("username")
    return None


def verify_identity(base: str, token: str, expected: str | None = None) -> dict[str, Any]:
    me = api_json("GET", f"{base}/api/me", token=token)
    target = expected if expected is not None else expected_username()
    if target and me.get("username") != target:
        raise SystemExit(
            f"Refusing to write as {me.get('username')}; expected {target}. "
            "Check PELAGO_FORUM_TOKEN_CACHE and relogin before posting."
        )
    return me


def read_body(body: str | None, body_file: str | None) -> str:
    if body and body_file:
        raise SystemExit("Use either --body or --body-file, not both")
    if body_file:
        return Path(body_file).read_text(encoding="utf-8").strip()
    if body:
        return body.strip()
    raise SystemExit("Provide --body or --body-file")
