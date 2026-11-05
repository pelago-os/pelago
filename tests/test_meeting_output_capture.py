"""Real local child processes, synthetic bytes only; no provider or account calls."""
import os
import stat
import subprocess
import sys
import time

import pytest

from app.meetings import output_capture as capture_module
from app.meetings.output_capture import OutputCapture
from app.meetings.runner import group_alive, kill_group
from test_meetings import env, create, act, drive, state, control  # noqa: F401


def collect(tmp_path, code, monkeypatch, limit=4096):
    monkeypatch.setattr(capture_module, 'STDOUT_LIMIT', limit)
    monkeypatch.setattr(capture_module, 'STDERR_LIMIT', limit)
    cap = OutputCapture(tmp_path/'out', tmp_path/'err')
    child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    cap.start(child.stdout, child.stderr)
    try:
        child.wait(timeout=10)
        cap.finish()
        assert not cap._thread.is_alive()
    finally:
        kill_group(child.pid, .1, 2, reap=child.poll)
        cap.finish()
    return cap, (tmp_path/'out').read_bytes(), (tmp_path/'err').read_bytes()


@pytest.mark.parametrize('extra', [0, 1])
def test_exact_byte_limit_and_one_byte_over(tmp_path, monkeypatch, extra):
    cap, out, err = collect(tmp_path, f"import os; os.write(1, b'x'*{4096+extra}); os.write(2,b'y'*4096)", monkeypatch)
    assert out == b'x'*4096 and err == b'y'*4096
    assert cap.failed.is_set() is bool(extra)
    assert cap.error_code == ('output_limit_exceeded' if extra else None)
    assert stat.S_IMODE((tmp_path/'out').stat().st_mode) == 0o600


def test_both_streams_far_beyond_pipe_capacity_do_not_deadlock(tmp_path, monkeypatch):
    cap, out, err = collect(tmp_path,
        "import os\nfor _ in range(256):\n os.write(1,b'x'*65536)\n os.write(2,b'y'*65536)", monkeypatch)
    assert cap.error_code == 'output_limit_exceeded'
    assert len(out) == len(err) == 4096


def test_normal_tail_and_split_utf8_are_drained_before_parse(tmp_path, monkeypatch):
    raw = ('深海'*1000).encode()
    cap, out, err = collect(tmp_path,
        "import os\nb=('深海'*1000).encode()\nfor i in range(0,len(b),7): os.write(1,b[i:i+7])\nos.write(2,b'last')",
        monkeypatch, limit=8192)
    assert not cap.failed.is_set() and out == raw and err == b'last'


def test_no_eof_does_not_wait_forever_or_accept_partial_receipt(tmp_path):
    cap = OutputCapture(tmp_path/'out', tmp_path/'err')
    read_out, write_out = os.pipe(); read_err, write_err = os.pipe()
    cap.start(os.fdopen(read_out,'rb'), os.fdopen(read_err,'rb'))
    try:
        os.write(write_out, b'partial')
        started = time.monotonic()
        cap.finish()
        assert time.monotonic() - started < 3
        assert cap.error_code == 'output_capture_failed'
        assert not cap._thread.is_alive()
    finally:
        os.close(write_out); os.close(write_err)


def flood(env, monkeypatch, fd=1, hang=True):
    import app.meetings.runner as runner_module
    monkeypatch.setattr(capture_module, 'STDOUT_LIMIT', 1024)
    monkeypatch.setattr(capture_module, 'STDERR_LIMIT', 1024)
    original = runner_module.build_command
    def command(*args, **kwargs):
        _, environ, cwd, prompt = original(*args, **kwargs)
        code = f"import os,time; os.write({fd}, b'x'*65536); " + ('time.sleep(60)' if hang else '')
        return [sys.executable, '-c', code, f'--tag={env.tmp}'], environ, cwd, prompt
    monkeypatch.setattr(runner_module, 'build_command', command)
    meeting = create(env, rounds=1)
    act(env, meeting['id'], 'start')
    drive(env, lambda: bool(env.runner.procs))
    run = next(iter(env.runner.procs.values()))
    assert run.capture.failed.wait(timeout=3)
    return meeting, run


@pytest.mark.parametrize('fd,hang', [(1,True),(2,True),(1,False)])
def test_overflow_fails_without_parsing_and_only_releases_after_gone(env, monkeypatch, fd, hang):
    import app.meetings.runner as runner_module
    meeting, run = flood(env, monkeypatch, fd, hang)
    monkeypatch.setattr(runner_module, 'parse_result', lambda *a,**k: pytest.fail('must not parse a truncated receipt'))
    drive(env, lambda: state(env, meeting['id'])['state'] == 'paused')
    attempt = state(env, meeting['id'])['attempts'][0]
    assert attempt['state'] == 'failed' and attempt['error_code'] == 'output_limit_exceeded'
    assert group_alive(run.pgid) is False
    assert not run.capture._thread.is_alive()
    assert not list(env.runner.run_dir.glob(run.attempt_id+'.*'))


def test_overflow_preserves_quarantine_when_external_termination_unknown(env, monkeypatch):
    import app.meetings.runner as runner_module
    meeting, run = flood(env, monkeypatch)
    monkeypatch.setattr(runner_module, 'external_units_gone', lambda _: False)
    drive(env, lambda: not env.runner.procs)
    attempt = state(env, meeting['id'])['attempts'][0]
    assert attempt['state'] == 'quarantined'
    assert run.out_path.exists() and run.out_path.stat().st_size == 1024
    assert not run.capture._thread.is_alive()


def test_overflow_and_user_pause_keep_cancel_semantics(env, monkeypatch):
    meeting, run = flood(env, monkeypatch)
    act(env, meeting['id'], 'pause')
    drive(env, lambda: not env.runner.procs)
    assert state(env, meeting['id'])['attempts'][0]['state'] == 'cancelled'
    assert group_alive(run.pgid) is False and not run.capture._thread.is_alive()


def test_fencing_overflow_does_not_write_result(env, monkeypatch):
    meeting, run = flood(env, monkeypatch)
    before = state(env, meeting['id'])['attempts'][0]
    env.runner.fence('lost lease test')
    after = state(env, meeting['id'])['attempts'][0]
    assert before == after
    assert group_alive(run.pgid) is False and not run.capture._thread.is_alive()


def test_capture_start_failure_reclaims_unreleased_launcher(env, monkeypatch):
    def fail_start(self, stdout, stderr):
        stdout.close(); stderr.close()
        raise RuntimeError('synthetic thread startup failure')
    monkeypatch.setattr(OutputCapture, 'start', fail_start)
    meeting = create(env, rounds=1)
    act(env, meeting['id'], 'start')
    drive(env, lambda: state(env, meeting['id'])['state'] == 'paused')
    assert state(env, meeting['id'])['attempts'][0]['error_code'] == 'launch_failed'
    assert not list(env.runner.run_dir.glob('att_*'))


def test_unconfirmed_local_group_is_quarantined_without_touching_other_process(env, monkeypatch):
    import app.meetings.runner as runner_module
    from app.database import get_connection
    other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    meeting, run=flood(env, monkeypatch)
    real_kill=runner_module.kill_group
    monkeypatch.setattr(runner_module,'kill_group',lambda *a,**k:False)
    try:
        drive(env,lambda:not env.runner.procs)
        assert state(env,meeting['id'])['attempts'][0]['state']=='quarantined'
        with get_connection() as conn:
            assert conn.execute('SELECT state FROM meeting_attempts WHERE id=?',(run.attempt_id,)).fetchone()[0]=='quarantined'
        assert run.out_path.stat().st_size==1024
        assert other.poll() is None
    finally:
        real_kill(run.pgid,.1,2,reap=run.popen.poll)
        real_kill(other.pid,.1,2,reap=other.poll)


def test_write_failure_never_accepts_partial_output(tmp_path, monkeypatch):
    original=capture_module.private_open
    class Broken:
        def __init__(self,path,mode): self.file=original(path,mode)
        def __enter__(self): return self
        def __exit__(self,*args): self.file.close()
        def write(self,value): raise OSError('private-path-must-not-be-in-error')
    monkeypatch.setattr(capture_module,'private_open',Broken)
    cap,_,_=collect(tmp_path,"import os; os.write(1,b'x')",monkeypatch)
    assert cap.error_code=='output_capture_failed'
