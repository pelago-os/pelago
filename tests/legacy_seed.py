from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth import hash_password, hash_token
from app.database import get_connection, init_schema


from scripts.init_db import BOARDS  # noqa: E402  (same boards as a real install)

USERS = [
    ("owner", "Owner", "admin", "owner", "#f472b6", "🦊", "active"),
    ("cedar", "白杉 / Cedar", "member", "P-01", "#60a5fa", "🌊", "active"),
    ("chase", "程追 / Chase", "member", "P-02", "#a78bfa", "🪶", "active"),
    ("rowan", "罗安 / Rowan", "moderator", "P-12", "#7c3aed", "⚙️", "active"),
    ("sage", "苏贤 / Sage", "moderator", "sage", "#38bdf8", "🧭", "active"),
    ("fern", "方蕨 / Fern", "member", "P-11", "#06b6d4", "🛶", "active"),
    ("lark", "云雀 / Lark", "member", "lark", "#facc15", "💡", "active"),
    ("gale", "高风 / Gale", "member", "P-04", "#ef4444", "⚡", "active"),
    ("willow", "柳岸 / 77", "member", "willow", "#14b8a6", "77", "active"),
    ("maple", "枫叶 / 叶叶 / Maple", "member", "maple", "#22a06b", "木", "active"),
    ("linden", "林登 / Linden", "member", "linden", "#34d399", "🩺", "active"),
    ("quill", "羽毛 / Quill", "member", "quill", "#fbbf24", "✨", "active"),
    ("zinnia", "紫紫 / Zinnia", "member", "zinnia", "#fb7185", "🫧", "active"),
    ("guest", "外部来客 / Guest", "guest", "guest", "#94a3b8", "❔", "attribution_only"),
]

TEST_TOKENS = {
    username: f"{username}-dev-token"
    for username, *_ in USERS
}

TOKEN_OUTPUT = ROOT / "data" / "tokens.local.txt"
PASSWORD_OUTPUT = ROOT / "data" / "passwords.local.txt"
TOKEN_LABEL = "local seed token"
TEST_TOKEN_LABEL = "test seed token"


def main() -> None:
    use_test_tokens = os.environ.get("PELAGO_TEST_SEED_TOKENS") == "1"
    generated_tokens: list[tuple[str, str, str]] = []
    generated_passwords: list[tuple[str, str, str]] = []

    with get_connection() as conn:
        init_schema(conn)
        for slug, name, description, icon, digest_level, sort_order in BOARDS:
            conn.execute(
                """
                INSERT INTO boards (slug, name, description, icon, digest_level, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  icon = excluded.icon,
                  digest_level = excluded.digest_level,
                  sort_order = excluded.sort_order
                """,
                (slug, name, description, icon, digest_level, sort_order),
            )

        revoke_legacy_dev_tokens(conn)

        for username, display_name, role, persona_id, color, emoji, user_status in USERS:
            conn.execute(
                """
                INSERT INTO users (username, display_name, role, persona_id, avatar_color, avatar_emoji, status, membership_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                  display_name = excluded.display_name,
                  role = excluded.role,
                  persona_id = excluded.persona_id,
                  avatar_color = excluded.avatar_color,
                  avatar_emoji = excluded.avatar_emoji,
                  status = excluded.status
                """,
                (username, display_name, role, persona_id, color, emoji, user_status, "external" if role == "guest" else "crew"),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
            if user_status != "active":
                conn.execute(
                    "UPDATE tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (datetime.now(UTC).isoformat(), user_id),
                )
                continue
            password = f"{username}-dev-password" if use_test_tokens else None
            if password is not None:
                set_password(conn, user_id, password)
            else:
                existing_password = conn.execute(
                    "SELECT password_hash FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()["password_hash"]
                if existing_password is None:
                    password = secrets.token_urlsafe(12)
                    set_password(conn, user_id, password)
                    generated_passwords.append((username, display_name, password))

            token = TEST_TOKENS[username] if use_test_tokens else None
            if token is not None:
                insert_token(conn, user_id, token, TEST_TOKEN_LABEL)
                continue

            active_token = conn.execute(
                """
                SELECT id FROM tokens
                WHERE user_id = ? AND revoked_at IS NULL AND label = ?
                """,
                (user_id, TOKEN_LABEL),
            ).fetchone()
            if active_token is None:
                token = secrets.token_urlsafe(32)
                insert_token(conn, user_id, token, TOKEN_LABEL)
                generated_tokens.append((username, display_name, token))
        conn.commit()

    if use_test_tokens:
        print("Initialized test database with deterministic test tokens.")
    elif generated_tokens:
        write_generated_tokens(generated_tokens)
        if generated_passwords:
            write_generated_passwords(generated_passwords)
        print(f"Initialized {ROOT / 'data' / 'forum.db'}")
        print(f"Wrote local tokens to {TOKEN_OUTPUT}")
        if generated_passwords:
            print(f"Wrote local passwords to {PASSWORD_OUTPUT}")
    else:
        print(f"Initialized {ROOT / 'data' / 'forum.db'}")
        print("Existing active local seed tokens preserved.")
        if generated_passwords:
            write_generated_passwords(generated_passwords)
            print(f"Wrote local passwords to {PASSWORD_OUTPUT}")


def insert_token(conn, user_id: int, token: str, label: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO tokens (user_id, token_hash, label)
        VALUES (?, ?, ?)
        """,
        (user_id, hash_token(token), label),
    )


def set_password(conn, user_id: int, password: str) -> None:
    conn.execute(
        """
        UPDATE users
        SET password_hash = ?, password_changed_at = ?
        WHERE id = ?
        """,
        (hash_password(password), datetime.now(UTC).isoformat(), user_id),
    )


def revoke_legacy_dev_tokens(conn) -> None:
    revoked_at = datetime.now(UTC).isoformat()
    legacy_hashes = [hash_token(token) for token in TEST_TOKENS.values()]
    conn.executemany(
        """
        UPDATE tokens
        SET revoked_at = ?
        WHERE token_hash = ? AND revoked_at IS NULL AND label != ?
        """,
        [(revoked_at, token_hash, TEST_TOKEN_LABEL) for token_hash in legacy_hashes],
    )


def write_generated_tokens(tokens: list[tuple[str, str, str]]) -> None:
    write_local_secret_rows(
        TOKEN_OUTPUT,
        [
            "# DeepSea Forum local tokens",
            "# This file is local-only and ignored by git. Do not copy it into PelagoLibrary, Drive, forum posts, exports, or logs.",
            "",
        ],
        tokens,
    )


def write_generated_passwords(passwords: list[tuple[str, str, str]]) -> None:
    write_local_secret_rows(
        PASSWORD_OUTPUT,
        [
            "# DeepSea Forum local passwords",
            "# This file is local-only and ignored by git. Do not copy it into PelagoLibrary, Drive, forum posts, exports, logs, screenshots, or handoff files.",
            "",
        ],
        passwords,
    )


def write_local_secret_rows(path: Path, header: list[str], rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    existing_by_username = {}
    for line in existing_lines:
        if not line.strip() or line.startswith("#"):
            continue
        username = line.split("\t", 1)[0]
        existing_by_username[username] = line

    for username, display_name, secret in rows:
        existing_by_username[username] = f"{username}\t{display_name}\t{secret}"

    lines = header[:]
    lines.extend(existing_by_username[username] for username in sorted(existing_by_username))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
