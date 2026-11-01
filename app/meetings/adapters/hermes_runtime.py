"""Opt-in experimental dispatch. Local reclamation never proves remote cancellation."""
import json
from pathlib import Path
import re
import threading
import uuid

from app.meetings.runtime_gate import approve, check_approved
from .hermes_candidate import invocation
from .receipts import parse_hermes

_LOCK = threading.RLock()
_APPROVALS = {}
PROVIDERS = {'NVIDIA': 'nvidia', 'DEEPSEEK': 'deepseek', 'OPENROUTER': 'openrouter',
             'GEMINI': 'gemini', 'OPENAI_CODEX': 'openai-codex'}

_PROTOCOL = 'pelago.hermes.local.v1'


def _key(member):
    if member.experimental is not True or member.policy_audited is not True:
        raise ValueError('experimental runtime requires operator policy approval')
    return (member.runtime_root, member.runtime_manifest,
            member.runtime_manifest_sha256, member.runtime_version)


def _approved(member, *, initial=False):
    key = _key(member)
    with _LOCK:
        approval = _APPROVALS.get(key)
        if approval is None:
            if not initial:
                raise ValueError('runtime approval unavailable')
            approval = approve(*key)
            _APPROVALS[key] = approval
        # Expired or changed approvals are never silently refreshed.
        check_approved(approval)
        entry = json.loads(approval.manifest.read_text())['entrypoint']
        if member.command != (str(approval.root / entry),):
            raise ValueError('command differs from approved entrypoint')
        return approval


def _session(attempt_id):
    if not isinstance(attempt_id, str) or not re.fullmatch(r'att_[0-9a-f]{16}', attempt_id):
        raise ValueError('invalid attempt identifier')
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'pelago:' + attempt_id))


def snapshot(member, attempt_id):
    approval = _approved(member, initial=True)
    doc = json.loads(approval.manifest.read_text())
    return {'kind': 'none', 'adapter': 'hermes', 'protocol': _PROTOCOL,
            'attempt_id': attempt_id, 'session_id': _session(attempt_id),
            'runtime_manifest_sha256': member.runtime_manifest_sha256,
            'runtime_version': member.runtime_version,
            'speaker_sha256': doc['files'][doc['entrypoint']],
            'state_home': member.home_dir,
            'remote_cancellation': 'unknown'}


def valid_snapshot(value):
    try:
        return (isinstance(value, dict) and value.get('kind') == 'none'
                and value.get('adapter') == 'hermes' and value.get('protocol') == _PROTOCOL
                and value.get('session_id') == _session(value.get('attempt_id'))
                and isinstance(value.get('runtime_version'), str) and bool(value['runtime_version'])
                and isinstance(value.get('runtime_manifest_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', value['runtime_manifest_sha256']) is not None
                and isinstance(value.get('speaker_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', value['speaker_sha256']) is not None
                and value.get('remote_cancellation') == 'unknown')
    except (TypeError, ValueError):
        return False


def _bound(member, value):
    if (not valid_snapshot(value)
            or value['runtime_manifest_sha256'] != member.runtime_manifest_sha256
            or value['runtime_version'] != member.runtime_version):
        raise ValueError('execution snapshot mismatch')
    approval = _approved(member)
    doc = json.loads(approval.manifest.read_text())
    if value['speaker_sha256'] != doc['files'][doc['entrypoint']]:
        raise ValueError('speaker snapshot mismatch')
    return approval


def build(member, ctx, external):
    approval = _bound(member, external)
    if external['attempt_id'] != ctx['attempt']['id']:
        raise ValueError('execution attempt mismatch')
    model = ctx.get('model') or member.default_model
    return invocation(approval, model, member, external)



def finish(member, stdout, exit_code, requested, external):
    base = {'provider': 'hermes', 'support_tier': 'experimental',
            'requested_model': requested, 'remote_cancellation': 'unknown'}
    try:
        _bound(member, external)
    except (ValueError, OSError, KeyError, TypeError):
        return dict(base, status='failed', error_code='runtime_gate_rejected')
    routes = {(PROVIDERS[route], model) for model, route in member.served_routes.get(requested, ())}
    result = parse_hermes(stdout, exit_code, requested, routes, external['session_id'],
                          member.runtime_manifest_sha256, external['speaker_sha256'])
    return dict(base, **result)


def health(member):
    # Listing adapters does not approve a runtime, launch a process, or read credentials.
    installed = bool(member.command and Path(member.command[0]).is_file())
    return {'ok': False, 'installed': installed, 'status': 'experimental_requires_acceptance',
            'live_verified': False, 'remote_cancellation': 'unknown'}
