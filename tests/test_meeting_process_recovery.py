"""Process ownership and zombie-only EPERM regression, without real models."""
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest
from app.meetings import runner


@pytest.mark.parametrize('alive,expected', [(False, True), (True, False), (None, False)])
def test_eperm_requires_confirmed_dead_group(monkeypatch, alive, expected):
    reaped = []
    def denied(*args):
        raise PermissionError('test')
    monkeypatch.setattr(runner.os, 'killpg', denied)
    def inspect(pgid):
        assert reaped == [True]
        return alive
    monkeypatch.setattr(runner, 'group_alive', inspect)
    assert runner.kill_group(987654, 0, 0, reap=lambda: reaped.append(True)) is expected


def test_ps_failure_is_unknown_not_dead(monkeypatch):
    def denied(*args):
        raise PermissionError('test')
    monkeypatch.setattr(runner.os, 'killpg', denied)
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout=''))
    assert runner.group_alive(987654) is None
    assert runner.kill_group(987654, 0, 0) is False


def test_exited_unreaped_child_is_not_quarantined():
    child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    try:
        # Deliberately do not poll/wait before kill_group: reproduce the zombie window.
        time.sleep(0.4)
        assert runner.kill_group(child.pid, 0.5, 2, reap=child.poll) is True
        assert runner.group_alive(child.pid) is False
    finally:
        child.wait(timeout=3)
