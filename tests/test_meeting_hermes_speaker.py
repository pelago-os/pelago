"""Offline enforcement tests against the shipped speaker, with no Hermes import or account."""
import base64
import json
import time
import types
from pathlib import Path
import pytest
from scripts import meeting_hermes_speaker as s


def classes():
    class Agent:
        def __init__(self, session_id=None):
            self.session_id=session_id
            self.tools=[]; self.valid_tool_names=[]; self.provider='nvidia'
        def _execute_tool_calls(self,*args):
            raise AssertionError('original tool executor ran')
        def _interruptible_api_call(self,payload):
            return types.SimpleNamespace(model='served-1')
        def _interruptible_streaming_api_call(self,payload):
            return types.SimpleNamespace(model='served-1')
    class Emitter:
        def attach(self,agent): return True
    return Agent,Emitter


def evidence():
    return dict(runtime_guard=False, session_id='session-1', allowed_models=['request-1'],calls=[],tool_attempts=0)


def test_effective_tools_session_and_actual_response(monkeypatch):
    monkeypatch.setattr(s,'ROUTES',{'request-1':'nvidia'});monkeypatch.setattr(s,'SERVED',{'request-1':['served-1']})
    Agent,Emitter=classes();e=evidence();s.bind_session(Agent,'session-1');s.install_guards(Agent,Emitter,e)
    agent=Agent('other');assert agent.session_id=='session-1'
    assert Emitter().attach(agent)
    agent._interruptible_api_call({'model':'request-1'})
    assert e['calls'][0]['served']=='served-1' and e['calls'][0]['source']=='sdk.response.model'
    assert agent._api_max_retries==1  # Hermes 0.21.5 loops `while retry_count < max_retries`: 0 would never call
    with pytest.raises(s.PolicyViolation): agent._execute_tool_calls([])
    assert e['tool_attempts']==1


@pytest.mark.parametrize('payload',[{'model':'wrong'}, {'model':'request-1','tools':[{}]},
    {'model':'request-1','functions':[{}]}, {'model':'request-1','extra_body':{'model':'other'}}])
def test_tool_or_route_injection_cannot_call_provider(monkeypatch,payload):
    monkeypatch.setattr(s,'ROUTES',{'request-1':'nvidia'});Agent,Emitter=classes();e=evidence()
    Agent._interruptible_api_call=lambda *a: pytest.fail('provider must not run')
    s.install_guards(Agent,Emitter,e);agent=Agent('session-1');Emitter().attach(agent)
    with pytest.raises(s.PolicyViolation): agent._interruptible_api_call(payload)


def test_attach_rejects_tool_capability_and_wrong_session():
    Agent,Emitter=classes();e=evidence();s.install_guards(Agent,Emitter,e)
    agent=Agent('session-1');agent.tools=['shell']
    with pytest.raises(s.PolicyViolation): Emitter().attach(agent)
    agent.tools=[];agent.session_id='wrong'
    with pytest.raises(s.PolicyViolation): Emitter().attach(agent)


def test_auxiliary_calls_denied():
    aux=types.SimpleNamespace(call_llm=lambda:pytest.fail(),async_call_llm=lambda:pytest.fail())
    s.install_aux_guards(aux)
    with pytest.raises(s.PolicyViolation): aux.call_llm()
    with pytest.raises(s.PolicyViolation): aux.async_call_llm()


def token(exp):
    return 'fake.'+base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')+'.fake'


def test_access_only_no_refresh_no_write(tmp_path):
    access=token(time.time()+600)
    p=tmp_path/'auth.json';p.write_text(json.dumps({'credential_pool':{'openai-codex':[{'access_token':access,'refresh_token':'never-use'}]}}));p.chmod(0o600)
    before=p.read_bytes()
    auth=types.SimpleNamespace(resolve_codex_runtime_credentials=lambda:pytest.fail(),_save_codex_tokens=lambda:pytest.fail())
    codex=types.SimpleNamespace(_codex_runtime_result=lambda t,**k:{'api_key':t},refresh_codex_oauth_pure=lambda:pytest.fail())
    s.install_auth_guards(auth,codex,tmp_path,True)
    assert auth.resolve_codex_runtime_credentials()['api_key']==access
    with pytest.raises(s.PolicyViolation): auth.resolve_codex_runtime_credentials(force_refresh=True)
    with pytest.raises(s.PolicyViolation): auth._save_codex_tokens()
    with pytest.raises(s.PolicyViolation): codex.refresh_codex_oauth_pure()
    assert p.read_bytes()==before
    with pytest.raises(s.PolicyViolation): s.check_access_expiry(token(time.time()+10))


def test_credential_symlink_rejected(tmp_path):
    target=tmp_path/'other';target.write_text('{}');target.chmod(0o600)
    (tmp_path/'auth.json').symlink_to(target)
    with pytest.raises(s.PolicyViolation): s.access_token(tmp_path)


def test_layout_rejects_source_dotenv(tmp_path):
    import hashlib
    root=tmp_path/'rt';(root/'hermes').mkdir(parents=True);(root/'hermes'/'.env').write_text('DEBUG=1\n')
    raw=json.dumps({'files':{}}).encode();(tmp_path/'m.json').write_bytes(raw)
    with pytest.raises(s.PolicyViolation, match='source .env'):
        s.verify_layout(root, tmp_path/'m.json', hashlib.sha256(raw).hexdigest())


def test_import_origin_rejects_unrelated_runtime(tmp_path):
    with pytest.raises(s.PolicyViolation): s.check_imports(tmp_path)


def test_codex_actual_terminal_event_not_synthetic_requested(monkeypatch):
    class Assembler:
        def feed(self,event): return event
    e=evidence();s.install_codex_evidence(Assembler,lambda o,k:o.get(k) if isinstance(o,dict) else None,e)
    Assembler().feed({'type':'response.completed','response':{'model':'actual'}})
    assert e['codex_raw_models']==['actual']
    Assembler().feed({'type':'response.failed','response':{'model':'request'}})
    assert e['codex_raw_models'][-1] is None


@pytest.mark.parametrize('pool', [[], [{}, {}], {'access_token': 'fake'}])
def test_ambiguous_pool_never_falls_back(tmp_path, pool):
    p=tmp_path/'auth.json'
    p.write_text(json.dumps({'providers':{'openai-codex':{'tokens':{'access_token':'fake'}}},
                             'credential_pool':{'openai-codex':pool}}))
    p.chmod(0o600)
    with pytest.raises(s.PolicyViolation): s.access_token(tmp_path)
