"""Pinned Hermes wrapper invocation. No credential reads or ambient configuration."""
import json
import os
from pathlib import Path
import stat
from app.meetings.runtime_gate import check_approved


def invocation(approval, model, member, snapshot):
    try:
        return _build(approval, model, member, snapshot)
    except OSError:
        raise ValueError('private meeting state unavailable') from None


def _build(approval, model, member, snapshot):
    from .hermes_runtime import PROVIDERS
    check_approved(approval)
    if model not in member.models or not member.policy_audited:
        raise ValueError('model or policy not approved')
    paths = [Path(p) for p in (member.cwd, member.home_dir, member.config_dir)]
    for path in paths:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or path.resolve().is_relative_to(approval.root.resolve())):
            raise ValueError('state must use private real directories outside runtime')
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != 3 or any(a.is_relative_to(b) for a in resolved for b in resolved if a != b):
        raise ValueError('working, home and credential directories must be distinct')
    # A fresh, exclusive directory belongs to this attempt. Never reuse a prior turn.
    turn = paths[1] / snapshot['attempt_id']
    turn.mkdir(mode=0o700)
    tmp = turn / 'tmp'; tmp.mkdir(mode=0o700)
    work = turn / 'work'; work.mkdir(mode=0o700)
    doc = json.loads(approval.manifest.read_text())
    pairs = member.served_routes[model]
    provider = PROVIDERS[pairs[0][1]]
    argv = [str(approval.root / doc['entrypoint']), '--runtime-root', str(approval.root),
            '--manifest', str(approval.manifest), '--runtime-sha256', member.runtime_manifest_sha256,
            '--profile', str(paths[2]), '--state-root', str(turn), '--model', model,
            '--session-id', snapshot['session_id'], '--route', model + '=' + provider]
    for served, _ in pairs:
        argv += ['--served', model + '=' + served]
    env = {k: os.environ[k] for k in ('LANG', 'LC_ALL', 'LC_CTYPE') if k in os.environ}
    env.update(PATH='/usr/bin:/bin', HOME=str(turn), TMPDIR=str(tmp), PYTHONNOUSERSITE='1',
               PYTHONDONTWRITEBYTECODE='1', PYTHONSAFEPATH='1')
    return argv, env, str(work)
