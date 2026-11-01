"""Actual scheduler/process execution with a synthetic native speaker, never a provider."""
import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import time
import pytest
from test_meetings import env, create, act, drive, state, tok  # noqa: F401
from test_meeting_claude_dispatch import candidate as claude_candidate  # noqa: F401
from app.meetings.adapter_config import _parse, AdapterConfigError
from app.meetings.adapters import launch_external_snapshot, build_command, parse_result, external_units_gone
from app.meetings.adapters import hermes_runtime as hr


@pytest.fixture
def candidate(claude_candidate):
    d = dict(claude_candidate, adapter='hermes', served_routes={
        'model-1': [['model-1', 'NVIDIA']], 'model-2': [['model-2', 'NVIDIA']]})
    Path(d['config_dir'], 'control.json').write_text(json.dumps({
        'speaker': hashlib.sha256(Path(d['command'][0]).read_bytes()).hexdigest()}))
    return d


def member(d):
    return _parse({'members': {'agent_alpha': d}})['agent_alpha']


def setup(env, candidate, monkeypatch, **control):
    from app.meetings import adapter_config, runner, service
    m = member(candidate)
    members = adapter_config.load_members().copy(); members['agent_alpha'] = m
    for module in (adapter_config, runner, service):
        monkeypatch.setattr(module, 'load_members', lambda: members)
    p = Path(m.config_dir, 'control.json'); data=json.loads(p.read_text());data.update(control);p.write_text(json.dumps(data))
    return m


def test_hermes_scheduler_mixed_mock_and_wrap(env, candidate, monkeypatch):
    setup(env, candidate, monkeypatch, text='## 讨论摘要\nTest\n## 达成的决定\n待确认\n## 分歧与未决\n待确认\n## 行动项\n待确认\n## 给主持人的简报\nTest')
    m = create(env, rounds=1, models={'agent_alpha': 'model-2'}, wrap_up=True, wrap_up_by='agent_alpha')
    assert act(env,m['id'],'start').status_code == 200
    drive(env, lambda: state(env,m['id'])['state']=='completed')
    s=state(env,m['id']); assert len(s['attempts'])==3
    assert all(a['state']=='accepted' for a in s['attempts'])
    assert s['attempts'][0]['model']=='model-2'
    assert s['attempts'][0]['support_tier']=='experimental'
    assert s['attempts'][-1]['round']==99
    body=env.client.get(f"/api/meetings/{m['id']}/minutes",headers=tok('moderator')).json()['latest']['body_md']
    assert 'Experimental adapter' in body and 'Mock 演示' in body


@pytest.mark.parametrize('control', [{'served':'model-2'},{'tools':['Bash']},{'session':True},{'speaker':'0'*64}])
def test_reject_bad_receipts(env,candidate,monkeypatch,control):
    setup(env,candidate,monkeypatch,**control)
    m=create(env,rounds=1);act(env,m['id'],'start')
    drive(env,lambda: bool(state(env,m['id'])['attempts']) and state(env,m['id'])['attempts'][0]['state']=='failed')


def test_pause_reclaims_process(env,candidate,monkeypatch):
    setup(env,candidate,monkeypatch,sleep=True)
    m=create(env,rounds=1);act(env,m['id'],'start');drive(env,lambda: bool(env.runner.procs))
    assert act(env,m['id'],'pause').status_code==200
    drive(env,lambda: not env.runner.procs)
    assert state(env,m['id'])['state']=='paused'


def test_isolated_attempt_and_environment(candidate,monkeypatch):
    m=member(candidate); snap=launch_external_snapshot(m,'att_'+'a'*16)
    monkeypatch.setenv('NVIDIA_API_KEY','never-inherit');monkeypatch.setenv('PYTHONPATH','/untrusted')
    argv,environment,cwd,prompt=build_command(m,{'attempt':{'id':snap['attempt_id'],'round':1},'model':'model-1',
        'meeting':{'title':'test','rounds':1,'max_chars':30},'speaker':{'display_name':'Test'},
        'exempt':False,'materials':[],'turns':[]},snap)
    assert 'NVIDIA_API_KEY' not in environment and 'PYTHONPATH' not in environment
    assert environment['HOME']!=m.home_dir and Path(cwd).parent==Path(environment['HOME'])
    assert '--fallback' not in argv and external_units_gone(snap)
    output=subprocess.run(argv,input=prompt,text=True,capture_output=True,env=environment,cwd=cwd,check=True)
    assert parse_result(m,output.stdout,'',0,'model-1',snap)['status']=='accepted'
    with pytest.raises(ValueError):
        hr.build(m,{'attempt':{'id':snap['attempt_id'],'round':1},'model':'model-1'},snap)
    assert not external_units_gone({'kind':'none','adapter':'hermes'})
    assert parse_result(m,output.stdout,'',0,'model-1',dict(snap,speaker_sha256='0'*64))['status']=='failed'


def test_gate_drift_and_expiry(candidate):
    m=member(candidate);snap=launch_external_snapshot(m,'att_'+'b'*16)
    hr._APPROVALS[hr._key(m)]=dataclasses.replace(hr._APPROVALS[hr._key(m)],expires_at=time.monotonic()-1)
    with pytest.raises(ValueError): launch_external_snapshot(m,'att_'+'c'*16)
    assert parse_result(m,'','',0,'model-1',snap)['error_code']=='runtime_gate_rejected'


@pytest.mark.parametrize('field,value',[('experimental',False),('policy_audited',False),('env',{'X':'y'}),
    ('served_routes',{}),('served_routes',{'model-1':[['model-1','ANTHROPIC']]}),('cleanup_conversations',True)])
def test_config_rejects_unsafe_settings(candidate,field,value):
    candidate[field]=value
    with pytest.raises(AdapterConfigError): member(candidate)
