from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from app.auth import hash_token
from app.database import get_connection
from app.services import login_limits
from test_forum_api import auth, client


def test_login_limits_without_invalidating_tokens(client):
    for _ in range(10):
        assert (
            client.post(
                "/api/login", json={"username": "owner", "password": "wrong"}
            ).status_code
            == 401
        )
    limited = client.post("/api/login", json={"username": "owner", "password": "wrong"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert client.get("/api/me", headers=auth("owner-dev-token")).status_code == 200
    assert (
        client.post(
            "/api/login", json={"username": "cedar", "password": "cedar-dev-password"}
        ).status_code
        == 200
    )


def test_revoked_token_and_disabled_user_are_denied(client):
    with get_connection() as conn:
        conn.execute(
            "UPDATE tokens SET revoked_at='2026-09-22' WHERE token_hash=?",
            (hash_token("rowan-dev-token"),),
        )
        conn.execute("UPDATE users SET status='disabled' WHERE username='cedar'")
    assert client.get("/api/me", headers=auth("rowan-dev-token")).status_code == 401
    assert client.get("/api/me", headers=auth("cedar-dev-token")).status_code == 401
    assert client.get("/api/me", headers=auth("owner-dev-token")).status_code == 200


def test_limit_is_atomic_and_expires(monkeypatch):
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: 100.0)

    def attempt(_):
        try:
            login_limits.check_login_limit("peer", "user")
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(30))) == 10
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: 160.0)
    assert attempt(0)


def test_limit_bounds_distinct_names_and_ignores_forwarded_headers(client):
    for index in range(60):
        response = client.post(
            "/api/login",
            headers={"X-Forwarded-For": str(index)},
            json={"username": f"unknown-{index}", "password": "wrong"},
        )
        assert response.status_code == 401
    assert (
        client.post(
            "/api/login", json={"username": "another", "password": "wrong"}
        ).status_code
        == 429
    )
