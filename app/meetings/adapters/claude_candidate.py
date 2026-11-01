"""Claude invocation builder; dispatch requires explicit experimental configuration.

Operator-managed policy, CLI version and credential behavior need independent
acceptance before dispatch may use this. A receipt cannot prove hooks never ran.
"""
import json
import os
from pathlib import Path
import stat
import uuid
from app.meetings.runtime_gate import check_approved


def _invocation(approval, model, allowed_models, session_id, cwd, home, config_dir, *, policy_audited=False):
    if policy_audited is not True:
        raise ValueError('managed settings and CLI capabilities must be independently audited')
    if not isinstance(model, str) or not model or model not in allowed_models:
        raise ValueError('model is not approved')
    uuid.UUID(session_id)
    check_approved(approval)
    data = json.loads(approval.manifest.read_text())
    paths = [Path(p) for p in (cwd, home, config_dir)]
    for path in paths:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
                or path.resolve().is_relative_to(approval.root.resolve())):
            raise ValueError('state must use private real directories outside the runtime')
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != 3 or any(a.is_relative_to(b) for a in resolved for b in resolved if a != b):
        raise ValueError('working directory, HOME and credential state must be distinct')
    argv = [str(approval.root / data['entrypoint']), '-p', '--output-format', 'stream-json', '--verbose',
            '--no-session-persistence', '--tools', '', '--disallowedTools', '*',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
            '--settings', '{"disableAllHooks":true}', '--session-id', session_id, '--model', model]
    temp = paths[1] / 'tmp'
    temp.mkdir(mode=0o700, exist_ok=True)
    info = temp.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.geteuid()):
        raise ValueError('temporary state must be a private real directory')
    env = {k: os.environ[k] for k in ('LANG', 'LC_ALL', 'LC_CTYPE') if k in os.environ}
    env.update(PATH='/usr/bin:/bin', TMPDIR=str(temp))
    env.update(HOME=str(paths[1]), CLAUDE_CONFIG_DIR=str(paths[2]), DISABLE_AUTOUPDATER='1')
    # Prompt goes to stdin in the runner, not argv. No inherited backend or auth variables.
    return argv, env, str(paths[0])


def invocation(approval, model, allowed_models, session_id, cwd, home, config_dir, *, policy_audited=False):
    try:
        return _invocation(approval, model, allowed_models, session_id, cwd, home, config_dir,
                           policy_audited=policy_audited)
    except OSError:
        # Do not persist credential paths in a runner error or chained traceback.
        raise ValueError('runtime or private state is unavailable; operator review required') from None
