"""Isolate imports as well as fixtures from the operator's local environment."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


_sandbox = TemporaryDirectory(prefix="forum-tests-")
_root = Path(_sandbox.name)
_paths = {
    "PELAGO_DB_PATH": _root / "forum.db",
    "PELAGO_AUDIT_LOG_PATH": _root / "audit.jsonl",
    "PELAGO_UPLOAD_ROOT": _root / "uploads",
    "PELAGO_LIBRARY_ROOT": _root / "memory",
    "PELAGO_REVIEW_ROOTS": _root / "memory",
    "DEEPSEA_MEMORY_MIRROR_ROOT": _root / "mirror",
}
for _name, _value in _paths.items():
    os.environ[_name] = str(_value)
os.environ["DEEPSEA_CROSS_BASE_WORKER_ENABLED"] = "0"
os.environ["PELAGO_TEST_SEED_TOKENS"] = "1"


@pytest.fixture(autouse=True)
def restore_settings(monkeypatch):
    from app import config
    from app.services import login_limits

    monkeypatch.setenv("PELAGO_REVIEW_ROOTS", "")
    monkeypatch.setattr(config, "settings", config.Settings.from_env())
    monkeypatch.setattr(login_limits, "_attempts", {})
