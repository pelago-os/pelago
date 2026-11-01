"""Opt-in experimental dispatch. Local reclamation never proves remote cancellation."""
import json
from pathlib import Path
import re
import threading
import uuid

from app.meetings.runtime_gate import approve, check_approved
from .claude_candidate import invocation
from .receipts import parse_claude

_LOCK = threading.RLock()
_APPROVALS = {}
_PROTOCOL = 'pelago.claude.local.v1'


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
    _approved(member, initial=True)
    return {'kind': 'none', 'adapter': 'claude_code', 'protocol': _PROTOCOL,
            'attempt_id': attempt_id, 'session_id': _session(attempt_id),
            'runtime_manifest_sha256': member.runtime_manifest_sha256,
            'runtime_version': member.runtime_version,
            'remote_cancellation': 'unknown'}


def valid_snapshot(value):
    try:
        return (isinstance(value, dict) and value.get('kind') == 'none'
                and value.get('adapter') == 'claude_code' and value.get('protocol') == _PROTOCOL
                and value.get('session_id') == _session(value.get('attempt_id'))
                and isinstance(value.get('runtime_version'), str) and bool(value['runtime_version'])
                and isinstance(value.get('runtime_manifest_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', value['runtime_manifest_sha256']) is not None
                and value.get('remote_cancellation') == 'unknown')
    except (TypeError, ValueError):
        return False


def _bound(member, value):
    if (not valid_snapshot(value)
            or value['runtime_manifest_sha256'] != member.runtime_manifest_sha256
            or value['runtime_version'] != member.runtime_version):
        raise ValueError('execution snapshot mismatch')
    return _approved(member)


def build(member, ctx, external):
    approval = _bound(member, external)
    if external['attempt_id'] != ctx['attempt']['id']:
        raise ValueError('execution attempt mismatch')
    model = ctx.get('model') or member.default_model
    return invocation(approval, model, member.models, external['session_id'],
                      member.cwd, member.home_dir, member.config_dir,
                      policy_audited=member.policy_audited)


def finish(member, stdout, exit_code, requested, external):
    base = {'provider': 'claude_code', 'support_tier': 'experimental',
            'requested_model': requested, 'remote_cancellation': 'unknown'}
    try:
        _bound(member, external)
    except (ValueError, OSError, KeyError, TypeError):
        return dict(base, status='failed', error_code='runtime_gate_rejected')
    routes = {name: tuple(model for model, route in pairs if route == 'ANTHROPIC')
              for name, pairs in member.served_routes.items()}
    result = parse_claude(stdout, exit_code, requested, routes, external['session_id'])
    return dict(base, **result)


def health(member):
    # Listing adapters does not approve a runtime, launch a process, or read credentials.
    installed = bool(member.command and Path(member.command[0]).is_file())
    return {'ok': False, 'installed': installed, 'status': 'experimental_requires_acceptance',
            'live_verified': False, 'remote_cancellation': 'unknown'}
