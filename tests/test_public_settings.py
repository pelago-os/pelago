import os
import pytest
from app.config import Settings


def test_clean_machine_defaults_without_file_writes(tmp_path):
    s = Settings.from_env(tmp_path, {})
    assert s.host == '127.0.0.1' and s.port == 8787
    assert s.db_path == tmp_path / 'data/forum.db'
    assert s.review_memory_roots == (tmp_path / 'library',)
    assert s.base_name == 'Pelago Base'
    assert not list(tmp_path.iterdir())


def test_explicit_overrides_and_relative_root_resolution(tmp_path):
    s = Settings.from_env(tmp_path, {'PELAGO_DATA_DIR': 'state', 'PELAGO_PORT': '8080',
        'PELAGO_REVIEW_ROOTS': os.pathsep.join(['inbox', 'inbox']),
        'PELAGO_TRUSTED_PROXY_IPS': '127.0.0.1,::1,127.0.0.1'})
    assert s.db_path == tmp_path / 'state/forum.db'
    assert s.port == 8080
    assert s.review_memory_roots == (tmp_path / 'inbox',)



@pytest.mark.parametrize('name,value', [('PELAGO_PORT','0'), ('PELAGO_PORT','65536'),
    ('PELAGO_PORT','secret-invalid'), ('PELAGO_HOST',''), ('PELAGO_DATA_DIR',''),
    ('PELAGO_BASE_NAME',''), ('PELAGO_STATIC_DIR','')])
def test_invalid_configuration_fails_without_leaking_values(tmp_path, name, value):
    with pytest.raises(ValueError) as error:
        Settings.from_env(tmp_path, {name: value})
    assert 'secret-invalid' not in str(error.value)


def test_legacy_local_paths_do_not_affect_public_configuration(tmp_path):
    s = Settings.from_env(tmp_path, {'FORUM_DB_PATH': '/private/old.db',
                                   'DEEPSEA_MEMORY_ROOT': '/private/library'})
    assert s.db_path == tmp_path / 'data/forum.db'
    assert s.deepsea_memory_root == tmp_path / 'library'


def test_launcher_uses_config_and_keeps_transport_peer(monkeypatch):
    import uvicorn
    from scripts import run_server
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    run_server.main()
    assert calls[0][1] == {'host': run_server.settings.host,
                          'port': run_server.settings.port, 'proxy_headers': False}


def test_station_meta_exposes_only_the_configured_base_name(monkeypatch):
    from fastapi.testclient import TestClient
    import app.config as config
    from app.main import app

    from dataclasses import replace
    monkeypatch.setattr(config, "settings", replace(config.settings, base_name="Tide Station"))
    body = TestClient(app).get("/api/station/meta").json()
    assert body == {"base_name": "Tide Station", "product": "Pelago OS", "version": "0.1.0"}
