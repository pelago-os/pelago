import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from dataclasses import replace

import pytest
from fastapi import HTTPException

from app import config, database


def test_upload_mount_follows_current_settings_without_creating_directory(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import upload_access
    # This test isolates directory selection; access policy has dedicated API tests.
    monkeypatch.setattr(upload_access, "allowed_media", lambda url, actor: True)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    (first / "example.txt").write_text("first")
    client = TestClient(app)
    monkeypatch.setattr(config, "settings", replace(config.settings, upload_root=first))
    assert client.get("/uploads/example.txt").text == "first"
    monkeypatch.setattr(
        config, "settings", replace(config.settings, upload_root=second)
    )
    assert client.get("/uploads/example.txt").status_code == 404
    assert not second.exists()
    second.mkdir()
    (second / "example.txt").write_text("second")
    assert client.get("/uploads/example.txt").text == "second"


def test_database_follows_current_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PELAGO_DB_PATH", str(tmp_path / "new.db"))
    monkeypatch.setattr(config, "settings", config.Settings.from_env())
    with database.get_connection() as conn:
        assert conn.execute("PRAGMA database_list").fetchone()[2] == str(
            tmp_path / "new.db"
        )




