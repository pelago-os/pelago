from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .database import get_connection, row_to_dict

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 180_000


@dataclass(frozen=True)
class Actor:
    id: int
    username: str
    display_name: str
    role: str
    persona_id: str | None
    membership_kind: str = "external"

    @property
    def is_crew(self) -> bool:
        return self.membership_kind == "crew"

    @property
    def is_admin(self) -> bool:
        return self.membership_kind in {"crew", "system"} and self.role == "admin"

    @property
    def is_moderator(self) -> bool:
        return self.membership_kind in {"crew", "system"} and self.role in {"admin", "moderator"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        algorithm, iterations_text, salt, expected = password_hash.split("$", 3)
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != PASSWORD_ALGORITHM or iterations < 100_000:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(digest, expected)


def actor_from_token(token: str) -> Actor | None:
    token_hash = hash_token(token)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT users.*
            FROM tokens
            JOIN users ON users.id = tokens.user_id
            WHERE tokens.token_hash = ? AND tokens.revoked_at IS NULL AND users.status = 'active'
            """,
            (token_hash,),
        ).fetchone()
    data = row_to_dict(row)
    if not data:
        return None
    return Actor(
        id=data["id"],
        username=data["username"],
        display_name=data["display_name"],
        role=data["role"],
        persona_id=data["persona_id"],
        membership_kind=data["membership_kind"],
    )


def require_actor(authorization: str | None = Header(default=None)) -> Actor:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    token = authorization.split(" ", 1)[1].strip()
    actor = actor_from_token(token)
    if actor is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return actor


def optional_actor(authorization: str | None = Header(default=None)) -> Actor | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return actor_from_token(authorization.split(" ", 1)[1].strip())


def require_crew(actor: Actor = Depends(require_actor)) -> Actor:
    if not actor.is_crew:
        raise HTTPException(status_code=403, detail="This area is for crew members")
    return actor


def resolve_acting_user_id(actor: Actor, acting_as: str | None) -> int:
    if acting_as is None or acting_as == actor.username:
        return actor.id
    if not actor.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="acting_as is admin-only")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? AND status = 'active'",
            (acting_as,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="acting_as user not found")
    return int(row["id"])
