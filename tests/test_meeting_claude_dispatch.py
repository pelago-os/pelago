"""Real subprocess integration with a fake CLI; no live provider or credentials."""
import dataclasses
import hashlib
import json
import sys
import subprocess
import shutil
import time
from pathlib import Path

import pytest
from test_meetings import env, create, act, drive, state, tok
from app.meetings.adapter_config import _parse, AdapterConfigError
from app.meetings.adapters import build_command, launch_external_snapshot, parse_result, external_units_gone
from app.meetings.adapters import claude_runtime as cr
from app.meetings.runtime_manifest import create as manifest_create


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / 'pinned'; root.mkdir()
    exe = root / 'speaker'
    compiler = shutil.which('cc')
    if not compiler:
        pytest.fail('native dispatch fixture requires a C compiler; do not skip release acceptance')
    source = Path(__file__).parent / 'fixtures' / 'meeting_fake_cli.c'
    subprocess.run([compiler, str(source), '-o', str(exe)], check=True, capture_output=True)
    exe.chmod(0o555); root.chmod(0o555)
    manifest = tmp_path / 'approved.json'
    manifest_create(root, manifest, 'fake-1', 'speaker')
    dirs = [tmp_path / n for n in ('cwd', 'home', 'profile')]
    for p in dirs: p.mkdir(mode=0o700)
    data = dict(adapter='claude_code', command=[str(exe)], cwd=str(dirs[0]), home_dir=str(dirs[1]),
                config_dir=str(dirs[2]), experimental=True, policy_audited=True, runtime_root=str(root),
                runtime_manifest=str(manifest), runtime_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                runtime_version='fake-1', models=['model-1', 'model-2'],
                served_routes={'model-1': [['model-1', 'ANTHROPIC']], 'model-2': [['model-2', 'ANTHROPIC']]})
    yield data
    root.chmod(0o755)


def member(data):
    return _parse({'members': {'agent_alpha': data}})['agent_alpha']


def setup(env, candidate, monkeypatch, control=None):
    m = member(candidate)
    # Exercise dispatch through the real scheduler with only the local test configuration replaced.
    from app.meetings import adapter_config, runner, service
    original = adapter_config.load_members()
    original['agent_alpha'] = m
    for module in (adapter_config, runner, service):
        monkeypatch.setattr(module, 'load_members', lambda: original)
    if control:
        (Path(m.cwd) / 'control.json').write_text(json.dumps(control))
    return m


def test_dispatch_real_process_and_requested_model_binding(env, candidate, monkeypatch):
    setup(env, candidate, monkeypatch)
    m = create(env, rounds=1, models={'agent_alpha': 'model-2'})
    assert act(env, m['id'], 'start').status_code == 200
    drive(env, lambda: state(env, m['id'])['state'] == 'completed')
    attempt = state(env, m['id'])['attempts'][0]
    assert attempt['state'] == 'accepted'
    assert attempt['model'] == attempt['requested_model'] == 'model-2'
    assert attempt['support_tier'] == 'experimental'
    from app.database import get_connection
    with get_connection() as conn:
        row = conn.execute('SELECT provider_session_id FROM meeting_attempts WHERE id=?', (attempt['id'],)).fetchone()
    assert row['provider_session_id']


@pytest.mark.parametrize('control,error', [({'served':'model-2'}, 'model_mismatch'),
    ({'tools':['Bash']}, 'tool_surface_not_empty'), ({'session':True}, 'session_mismatch')])
def test_provider_evidence_rejected_in_runner(env, candidate, monkeypatch, control, error):
    setup(env, candidate, monkeypatch, control)
    m = create(env, rounds=1)
    act(env, m['id'], 'start')
    drive(env, lambda: bool(state(env, m['id'])['attempts']) and state(env,m['id'])['attempts'][0]['state'] == 'failed')
    assert state(env,m['id'])['attempts'][0]['error_code'] == error


def test_prelaunch_gate_does_not_run_changed_binary(env, candidate, monkeypatch):
    m = setup(env, candidate, monkeypatch)
    Path(m.command[0]).chmod(0o755)
    meeting = create(env, rounds=1)
    act(env, meeting['id'], 'start')
    drive(env, lambda: bool(state(env,meeting['id'])['attempts']) and state(env,meeting['id'])['attempts'][0]['state']=='failed')
    assert not (Path(m.cwd)/'called').exists()


def test_pause_reclaims_local_cli_not_remote_claim(env, candidate, monkeypatch):
    setup(env, candidate, monkeypatch, {'sleep':True})
    meeting = create(env, rounds=1)
    act(env, meeting['id'], 'start')
    drive(env, lambda: bool(env.runner.procs))
    assert act(env,meeting['id'],'pause').status_code == 200
    drive(env, lambda: not env.runner.procs)
    assert state(env,meeting['id'])['state'] == 'paused'


@pytest.mark.parametrize('field,value', [('experimental',False),('policy_audited',False),
    ('runtime_manifest_sha256','bad'),('command',['/tmp/example','--tools=Bash']),
    ('served_routes',{}),('env',{'ANTHROPIC_API_KEY':'no'})])
def test_config_requires_explicit_bounded_approval(candidate, field, value):
    candidate[field]=value
    with pytest.raises(AdapterConfigError): member(candidate)


def test_snapshot_and_post_run_gate(candidate):
    m = member(candidate)
    snap = launch_external_snapshot(m, 'att_'+'a'*16)
    assert external_units_gone(snap)
    assert not external_units_gone({'kind':'none','adapter':'claude_code'})
    assert parse_result(m,'','',0,'model-1')['error_code']=='runtime_gate_rejected'
    wrong = dict(snap, session_id='wrong')
    assert parse_result(m,'','',0,'model-1',wrong)['error_code']=='runtime_gate_rejected'
    Path(m.command[0]).chmod(0o755)
    assert parse_result(m,'','',0,'model-1',snap)['error_code']=='runtime_gate_rejected'


def test_expired_approval_is_not_relearned(candidate):
    m=member(candidate)
    launch_external_snapshot(m,'att_'+'a'*16)
    key=cr._key(m)
    cr._APPROVALS[key]=dataclasses.replace(cr._APPROVALS[key],expires_at=time.monotonic()-1)
    with pytest.raises(ValueError,match='expired'): launch_external_snapshot(m,'att_'+'b'*16)


def test_manifest_entrypoint_must_match_command(candidate):
    m=dataclasses.replace(member(candidate),command=('/usr/bin/true',))
    with pytest.raises(ValueError,match='entrypoint'): launch_external_snapshot(m,'att_'+'a'*16)


def test_experimental_wrap_keeps_transcript_and_provenance(env, candidate, monkeypatch):
    wrap = "## 讨论摘要\n模拟讨论 (round 1, agent_alpha)\n## 达成的决定\n待确认\n## 分歧与未决\n待确认\n## 行动项\n待确认\n## 给主持人的简报\n测试内容"
    setup(env, candidate, monkeypatch, {'text':wrap})
    m=create(env,rounds=1,wrap_up=True,wrap_up_by='agent_alpha')
    act(env,m['id'],'start')
    drive(env,lambda: state(env,m['id'])['state']=='completed')
    s=state(env,m['id'])
    assert len(s['attempts'])==3
    assert s['attempts'][-1]['round']==99
    assert s['attempts'][-1]['support_tier']=='experimental'
    body=env.client.get(f"/api/meetings/{m['id']}/minutes",headers=tok('moderator')).json()['latest']['body_md']
    assert 'Experimental adapter' in body and 'Mock 演示' in body
    assert '## 达成的决定' in body
    assert s['attempts'][0]['text']


def test_restart_reclaims_snapshot_without_current_config(env,candidate,monkeypatch):
    from test_meetings import _expire_lease
    from app.meetings.runner import group_alive
    from app.meetings import runner
    setup(env,candidate,monkeypatch,{'sleep':True})
    m=create(env,rounds=1)
    act(env,m['id'],'start')
    drive(env,lambda: bool(env.runner.procs))
    processes=list(env.runner.procs.values())
    env.runner.procs.clear()
    _expire_lease()
    monkeypatch.setattr(runner,'load_members',lambda: {})
    second=env.new_runner()
    try:
        second.tick()
        s=state(env,m['id'])
        assert s['state']=='paused' and s['exit_reason']=='interrupted_restart'
        assert s['attempts'][0]['state']=='interrupted'
        assert all(group_alive(p.pgid) is False for p in processes)
    finally:
        second.fence('test complete')
        for p in processes: p.popen.wait(timeout=5)


def test_snapshot_cannot_launch_another_attempt(candidate):
    m=member(candidate)
    snap=launch_external_snapshot(m,'att_'+'a'*16)
    with pytest.raises(ValueError,match='attempt mismatch'):
        build_command(m,{'attempt':{'id':'att_'+'b'*16},'model':'model-1'},snap)


def test_missing_credentials_not_persisted_in_error_detail(env,candidate,monkeypatch):
    from app.database import get_connection
    m=setup(env,candidate,monkeypatch)
    Path(m.config_dir).rmdir()
    meeting=create(env,rounds=1)
    act(env,meeting['id'],'start')
    drive(env,lambda: bool(state(env,meeting['id'])['attempts']) and state(env,meeting['id'])['attempts'][0]['state']=='failed')
    with get_connection() as conn:
        row=conn.execute('SELECT error_detail FROM meeting_attempts WHERE meeting_id=?',(meeting['id'],)).fetchone()
    assert 'operator review required' in row['error_detail']
    assert m.config_dir not in row['error_detail']
    assert not (Path(m.cwd)/'called').exists()
