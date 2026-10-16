from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    db_path: Path
    audit_log_path: Path
    deepsea_memory_root: Path
    review_memory_roots: tuple[Path, ...]
    upload_root: Path | None = None
    # Compatibility for callers until their next refactor; no execution capability enabled.
    cross_base_worker_enabled: bool = False
    crew_elevated_sessions: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    base_name: str = "Pelago Base"
    static_dir: Path = PROJECT_ROOT / "frontend"

    @classmethod
    def from_env(cls, base_dir: Path | None = None,
                 env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        base = (base_dir or PROJECT_ROOT).resolve()

        def path(name, default):
            value = env.get(name, str(default)).strip()
            if not value:
                raise ValueError(f"{name} cannot be empty")
            result = Path(value).expanduser()
            return (result if result.is_absolute() else base / result).resolve()

        data = path("PELAGO_DATA_DIR", "data")
        library = path("PELAGO_LIBRARY_ROOT", "library")
        roots = []
        for value in env.get("PELAGO_REVIEW_ROOTS", "").split(os.pathsep):
            if value.strip():
                root = Path(value.strip()).expanduser()
                roots.append((root if root.is_absolute() else base / root).resolve())
        try:
            port = int(env.get("PELAGO_PORT", "8787"))
        except ValueError:
            raise ValueError("PELAGO_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ValueError("PELAGO_PORT must be between 1 and 65535")
        host = env.get("PELAGO_HOST", "127.0.0.1").strip()
        name = env.get("PELAGO_BASE_NAME", "Pelago Base").strip()
        if not host or not name:
            raise ValueError("PELAGO_HOST and PELAGO_BASE_NAME cannot be empty")
        return cls(db_path=path("PELAGO_DB_PATH", data / "forum.db"),
                   audit_log_path=path("PELAGO_AUDIT_LOG_PATH", data / "audit.jsonl"),
                   deepsea_memory_root=library,
                   review_memory_roots=tuple(dict.fromkeys(roots or [library])),
                   upload_root=path("PELAGO_UPLOAD_ROOT", data / "uploads"),
                   host=host, port=port, base_name=name,
                   static_dir=path("PELAGO_STATIC_DIR", "frontend"))

    @staticmethod
    def trusted_proxy_ips() -> set[str]:
        # Read on request to preserve runtime policy overrides; never trust invalid input.
        try:
            return {str(ipaddress.ip_address(value.strip())) for value in
                    os.environ.get("PELAGO_TRUSTED_PROXY_IPS", "").split(",") if value.strip()}
        except ValueError:
            return set()


settings = Settings.from_env()
