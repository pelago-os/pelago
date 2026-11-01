import os
import stat

import pytest

from app.meetings import artifacts
from test_meetings import env, create, act, drive, state, control  # noqa: F401


def test_files_and_home_cleaned_after_hermes_completion(env, monkeypatch, candidate):
    from test_meeting_hermes_dispatch import setup
    member = setup(env, candidate, monkeypatch)
    meeting = create(env, rounds=1)
    act(env, meeting["id"], "start")
    drive(env, lambda: state(env, meeting["id"])["state"] == "completed")
    assert not list(env.runner.run_dir.glob("att_*"))
    from pathlib import Path
    assert not list(Path(member.home_dir).glob("att_*"))


from test_meeting_hermes_dispatch import candidate  # noqa: E402,F401
from test_meeting_claude_dispatch import candidate as claude_candidate  # noqa: E402,F401


def test_cancel_cleans_only_after_external_confirmation(env, monkeypatch):
    import app.meetings.runner as module
    control(env, "agent_alpha", mode="hang")
    meeting = create(env, rounds=1)
    act(env, meeting["id"], "start")
    drive(env, lambda: bool(env.runner.procs))
    running = next(iter(env.runner.procs.values()))
    assert stat.S_IMODE(env.runner.run_dir.stat().st_mode) == 0o700
    for suffix in (".prompt", ".out", ".err", ".identity"):
        path = env.runner.run_dir / (running.attempt_id + suffix)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.setattr(module, "external_units_gone", lambda _: False)
    act(env, meeting["id"], "pause")
    drive(env, lambda: not env.runner.procs)
    assert running.out_path.exists()
    monkeypatch.setattr(module, "external_units_gone", lambda _: True)
    env.runner.tick()
    assert not running.out_path.exists()


def test_restart_cleans_orphan_files_but_preserves_unrelated(env):
    artifacts.secure_directory(env.runner.run_dir)
    orphan = env.runner.run_dir / ("att_" + "f" * 16 + ".prompt")
    orphan.write_text("private material")
    other = env.runner.run_dir / "operator-note"
    other.write_text("keep")
    env.runner.tick()
    assert not orphan.exists()
    assert other.read_text() == "keep"


def test_home_cleanup_never_follows_symlink(tmp_path):
    parent = tmp_path / "homes"
    parent.mkdir(mode=0o700)
    target = tmp_path / "other"
    target.mkdir()
    (target / "keep").write_text("keep")
    attempt = "att_" + "a" * 16
    (parent / attempt).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        artifacts.remove_home(parent, attempt)
    assert (target / "keep").read_text() == "keep"


def test_private_file_rejects_hardlink_without_truncating(tmp_path):
    target = tmp_path / "original"
    target.write_text("preserve")
    alias = tmp_path / "alias"
    os.link(target, alias)
    with pytest.raises(ValueError):
        artifacts.private_open(alias, "w")
    assert target.read_text() == "preserve"


def test_fenced_process_artifacts_reclaimed_by_recovery(env):
    from app.database import get_connection
    control(env, "agent_alpha", mode="hang")
    meeting = create(env, rounds=1)
    act(env, meeting["id"], "start")
    drive(env, lambda: bool(env.runner.procs))
    attempt = next(iter(env.runner.procs))
    env.runner.fence("test lease loss")
    with get_connection() as conn:
        conn.execute("UPDATE meeting_scheduler SET lease_until=0")
    env.runner.tick()
    assert not list(env.runner.run_dir.glob(attempt + ".*"))
    assert state(env, meeting["id"])["state"] == "paused"


def test_failed_admission_reclaims_new_hermes_home(env, candidate, monkeypatch):
    from pathlib import Path
    from test_meeting_hermes_dispatch import setup
    from app.meetings.adapters import hermes_runtime
    member = setup(env, candidate, monkeypatch)
    original = hermes_runtime.build

    def fail_after_creation(*args):
        original(*args)
        raise ValueError("test admission failure")

    monkeypatch.setattr(hermes_runtime, "build", fail_after_creation)
    meeting = create(env, rounds=1)
    act(env, meeting["id"], "start")
    drive(env, lambda: bool(state(env, meeting["id"])["attempts"])
          and state(env, meeting["id"])["attempts"][0]["state"] == "failed")
    assert not list(Path(member.home_dir).glob("att_*"))


def test_quarantine_without_identity_preserves_evidence(env, monkeypatch):
    from app.database import get_connection
    import app.meetings.runner as module
    control(env, "agent_alpha", mode="hang")
    meeting = create(env, rounds=1)
    act(env, meeting["id"], "start")
    drive(env, lambda: bool(env.runner.procs))
    running = next(iter(env.runner.procs.values()))
    monkeypatch.setattr(module, "external_units_gone", lambda _: False)
    act(env, meeting["id"], "pause")
    drive(env, lambda: not env.runner.procs)
    with get_connection() as conn:
        conn.execute("UPDATE meeting_attempts SET exec_pgid=NULL WHERE id=?", (running.attempt_id,))
    running.out_path.with_suffix(".identity").unlink()
    env.runner.tick()
    assert running.out_path.exists()


def test_bad_config_allows_sweep_and_idle_ticks_do_not_renew(env, monkeypatch):
    from app.meetings import runner, service
    def broken():
        raise ValueError("invalid TOML")
    monkeypatch.setattr(runner, "load_members", broken)
    swept = []
    monkeypatch.setattr(service, "sweep", lambda *args: swept.append(True))
    env.runner.tick()
    assert swept == [True]
    monkeypatch.setattr(service, "renew_lease", lambda *args: pytest.fail("unexpected idle renewal"))
    monkeypatch.setattr(env.runner, "_clean_artifacts", lambda **kw: pytest.fail("unexpected idle scan"))
    env.runner.tick()


def test_private_prompt_uses_utf8_in_ascii_locale(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "prompt"
    script = ("from pathlib import Path\nfrom app.meetings.artifacts import private_open\n"
              "import sys\nwith private_open(Path(sys.argv[1]), 'w') as f: f.write('\\u6df1\\u6d77')")
    subprocess.run([sys.executable, "-c", script, str(path)], check=True,
                   env=dict(os.environ, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"))
    assert path.read_bytes() == "深海".encode("utf-8")
