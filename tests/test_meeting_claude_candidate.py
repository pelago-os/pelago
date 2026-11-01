import json
from pathlib import Path
import uuid
import pytest
from test_meeting_runtime_gate import runtime  # noqa: F401
from app.meetings.runtime_gate import approve, RuntimeGateError
from app.meetings.adapters.claude_candidate import invocation


def test_invocation_has_explicit_empty_tools_and_no_inherited_credentials(runtime, tmp_path, monkeypatch):
    approval = approve(*runtime, 'test-1')
    dirs = [tmp_path / n for n in ('work', 'home', 'profile')]
    for p in dirs: p.mkdir(mode=0o700)
    for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN'):
        monkeypatch.setenv(key, 'must-not-inherit')
    session = str(uuid.uuid4())
    argv, env, cwd = invocation(approval, 'model-1', ('model-1',), session, *dirs, policy_audited=True)
    assert argv[0] == str(runtime[0] / 'speaker')
    assert argv[argv.index('--tools')+1] == ''
    assert argv[argv.index('--disallowedTools')+1] == '*'
    assert json.loads(argv[argv.index('--mcp-config')+1]) == {'mcpServers': {}}
    assert json.loads(argv[argv.index('--settings')+1])['disableAllHooks'] is True
    assert '--bare' not in argv  # bare mode changes subscription authentication semantics
    assert 'must-not-inherit' not in env.values()
    assert env['HOME'] == str(dirs[1]) and env['CLAUDE_CONFIG_DIR'] == str(dirs[2])
    assert cwd == str(dirs[0])


def test_candidate_requires_independent_policy_audit(runtime, tmp_path):
    approval = approve(*runtime, 'test-1')
    with pytest.raises(ValueError, match='independently audited'):
        invocation(approval, 'model-1', ('model-1',), str(uuid.uuid4()), tmp_path, tmp_path, tmp_path)


def test_candidate_rejects_stale_runtime_before_launch(runtime, tmp_path):
    approval = approve(*runtime, 'test-1')
    (runtime[0] / 'dependency.py').chmod(0o644)
    with pytest.raises(RuntimeGateError):
        invocation(approval, 'model-1', ('model-1',), str(uuid.uuid4()), tmp_path, tmp_path, tmp_path, policy_audited=True)


def test_candidate_rejects_nested_state_directories(runtime, tmp_path):
    approval = approve(*runtime, 'test-1')
    home = tmp_path / 'private-home'; home.mkdir(mode=0o700)
    work = home / 'work'; work.mkdir(mode=0o700)
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='distinct'):
        invocation(approval, 'model-1', ('model-1',), str(uuid.uuid4()), work, home, profile, policy_audited=True)


def private_dirs(tmp_path):
    dirs=[tmp_path / n for n in ('work','home','secret-profile-name')]
    for p in dirs: p.mkdir(mode=0o700)
    return dirs


def test_fixed_search_path_and_private_temp(runtime,tmp_path,monkeypatch):
    dirs=private_dirs(tmp_path)
    monkeypatch.setenv('PATH','/untrusted/bin')
    monkeypatch.setenv('TMPDIR','/shared/public')
    _,env,_=invocation(approve(*runtime,'test-1'),'model-1',('model-1',),str(uuid.uuid4()),*dirs,policy_audited=True)
    assert env['PATH']=='/usr/bin:/bin'
    assert env['TMPDIR']==str(dirs[1]/'tmp')
    assert (dirs[1]/'tmp').stat().st_mode & 0o777 == 0o700


def test_missing_state_error_never_contains_credential_path(runtime,tmp_path):
    dirs=private_dirs(tmp_path)
    dirs[-1].rmdir()
    with pytest.raises(ValueError) as error:
        invocation(approve(*runtime,'test-1'),'model-1',('model-1',),str(uuid.uuid4()),*dirs,policy_audited=True)
    assert 'secret-profile-name' not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize('case',['symlink','public','file'])
def test_temp_state_rejects_unsafe_existing_entry(runtime,tmp_path,case):
    dirs=private_dirs(tmp_path)
    temp=dirs[1]/'tmp'
    if case=='symlink': temp.symlink_to(dirs[0],target_is_directory=True)
    elif case=='file': temp.write_text('not a directory')
    else: temp.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        invocation(approve(*runtime,'test-1'),'model-1',('model-1',),str(uuid.uuid4()),*dirs,policy_audited=True)
