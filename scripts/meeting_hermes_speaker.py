#!/operator/approved/runtime/bin/python
"""Experimental standalone Hermes speaker; package into an independently audited runtime.

This source is not executable from the checkout. Replace the shebang with the
packaged interpreter path BEFORE generating and approving the complete manifest.
"""
import argparse
import base64
import contextlib
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

API_KEYS = {'nvidia': 'NVIDIA_API_KEY', 'deepseek': 'DEEPSEEK_API_KEY',
            'openrouter': 'OPENROUTER_API_KEY', 'gemini': 'GEMINI_API_KEY', 'openai-codex': None}
ROUTES = {}
SERVED = {}

class PolicyViolation(BaseException):
    """Must not be swallowed by provider retry/fallback Exception handlers."""


class BoundedTextCapture(io.StringIO):
    """Bound Python-level provider chatter before constructing a receipt.

    Direct fd writes still go to the parent's bounded pipes; contaminated JSON
    remains a failed receipt, never a best-effort extraction.
    """
    def __init__(self, limit, *, retain=True):
        super().__init__()
        self.remaining = limit
        self.retain = retain

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('expected text')
        # Avoid encoding an already oversized string just to count its bytes.
        if len(text) > self.remaining:
            raise PolicyViolation('provider output limit exceeded')
        size = len(text.encode('utf-8'))
        if size > self.remaining:
            raise PolicyViolation('provider output limit exceeded')
        self.remaining -= size
        return super().write(text) if self.retain else len(text)


class Terminated(BaseException):
    pass


# Auxiliary entry points (compression, title, MoA, plugins) that would reach a model outside the AIAgent guard.
AUX_CALLS = ('call_llm', 'async_call_llm')


def deny_aux(*args, **kwargs):
    raise PolicyViolation('auxiliary model calls forbidden')


def terminate(signum, frame):
    raise Terminated()


def access_token(profile):
    # Only one distinct access token from this profile; never retain/pass its refresh token.
    data = json.loads(private_file(profile, 'auth.json').read_text())
    tokens = []
    token = data.get('providers', {}).get('openai-codex', {}).get('tokens', {}).get('access_token')
    if token is not None:
        tokens.append(token)
    pool = data.get('credential_pool', {}).get('openai-codex')
    if pool is not None:
        if not isinstance(pool, list) or len(pool) != 1 or not isinstance(pool[0], dict):
            raise PolicyViolation('ambiguous credential pool')
        tokens.append(pool[0].get('access_token'))
    if not tokens or any(not isinstance(t, str) or not t for t in tokens):
        raise PolicyViolation('one profile access token required')
    if len(set(tokens)) != 1:
        raise PolicyViolation('one profile access token required')
    token = tokens[0]
    return token


def check_access_expiry(token):
    try:
        segment = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
        expiry = claims['exp']
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or expiry <= time.time() + 300:
            raise ValueError('expiring')
    except (ValueError, KeyError, IndexError, TypeError):
        raise PolicyViolation('missing or expiring access token') from None


def install_auth_guards(auth, codex_auth, profile, allow_codex):
    # Resolve lazily: an unavailable Codex login does not block an NVIDIA-only response.
    def denied(*args, **kwargs):
        raise PolicyViolation('meeting cannot refresh/import credentials')
    def resolve(*args, **kwargs):
        if not allow_codex or kwargs.get('force_refresh'):
            denied()
        token = access_token(profile)
        check_access_expiry(token)
        return codex_auth._codex_runtime_result(token, source='meeting-profile-access-only', last_refresh=None)
    swaps = []
    for module in (auth, codex_auth):
        if hasattr(module, 'resolve_codex_runtime_credentials'):
            swaps.append((module.resolve_codex_runtime_credentials, resolve))
        module.resolve_codex_runtime_credentials = resolve
        for name in ('_refresh_codex_auth_tokens', '_recover_codex_tokens_from_cli',
                     '_import_codex_cli_tokens', 'refresh_codex_oauth_pure',
                     '_refresh_expired_codex_probe_token', '_save_codex_tokens'):
            if hasattr(module, name):
                swaps.append((getattr(module, name), denied))
                setattr(module, name, denied)
    return swaps


def install_aux_guards(aux):
    # Must run before cli/run_agent import: context_compressor, title_generator and moa_loop bind
    # `from agent.auxiliary_client import call_llm` at load, so a later attribute patch misses them.
    swaps = [(getattr(aux, name), deny_aux) for name in AUX_CALLS]
    for name in AUX_CALLS:
        setattr(aux, name, deny_aux)
    return swaps


def rebind_aliases(swaps):
    # A `from x import f` made before its guard keeps the original f; rebind every module-level copy.
    replacements = {id(original): (original, replacement) for original, replacement in swaps}
    for module in list(sys.modules.values()):
        namespace = getattr(module, '__dict__', None)
        if not isinstance(namespace, dict):
            continue
        for name, value in list(namespace.items()):
            hit = replacements.get(id(value))
            if hit and hit[0] is value:
                namespace[name] = hit[1]


def install_guards(agent_cls, emitter_cls, evidence):
    attach = emitter_cls.attach

    def guarded_attach(self, agent):
        if getattr(agent, 'tools', None) != [] or getattr(agent, 'valid_tool_names', None):
            raise PolicyViolation('nonempty tool surface')
        agent._disable_streaming = True
        agent._api_max_retries = 1  # Hermes counts attempts, not retries: one request, never a silent retry
        evidence['runtime_guard'] = True
        if agent.session_id != evidence['session_id']:
            raise PolicyViolation('unexpected agent session')
        return attach(self, agent)

    emitter_cls.attach = guarded_attach

    def deny_tools(*args, **kwargs):
        evidence['tool_attempts'] += 1
        raise PolicyViolation('tool execution forbidden')

    agent_cls._execute_tool_calls = deny_tools
    for name in ('_interruptible_api_call', '_interruptible_streaming_api_call'):
        original = getattr(agent_cls, name)  # incompatible runtime fails before any call

        def guarded(self, payload, *args, _original=original, **kwargs):
            requested = payload.get('model')
            extra = payload.get('extra_body') or {}
            if (not evidence['runtime_guard'] or self.tools != [] or
                    payload.get('tools') or payload.get('functions') or
                    not isinstance(extra, dict) or any(k in extra for k in ('tools', 'functions', 'model')) or
                    requested not in evidence['allowed_models'] or self.provider != ROUTES[requested]):
                raise PolicyViolation('request outside meeting policy')
            raw_before = len(evidence.get('codex_raw_models', []))
            try:
                response = _original(self, payload, *args, **kwargs)
            except Exception as exc:
                evidence.setdefault('failed_requests', []).append(
                    {'requested': requested, 'provider': self.provider, 'error': type(exc).__name__})
                raise
            served = getattr(response, 'model', None)
            source = 'sdk.response.model'
            if self.provider == 'openai-codex':
                raw_models = evidence.get('codex_raw_models', [])[raw_before:]
                if not raw_models or any(m != requested for m in raw_models):
                    raise PolicyViolation('missing or different raw Codex response model')
                served = raw_models[-1]
                source = 'raw.response.completed.model'
            evidence['calls'].append({'requested': requested, 'provider': self.provider,
                                      'served': served, 'tools': [], 'session_id': self.session_id, 'source': source})
            if served not in SERVED[requested]:
                raise PolicyViolation('missing or different response.model')
            return response

        setattr(agent_cls, name, guarded)


def install_codex_evidence(assembler_cls, field, evidence):
    feed = assembler_cls.feed
    def observed(self, event):
        if field(event, 'type') in ('response.completed', 'response.incomplete', 'response.failed'):
            model = field(field(event, 'response'), 'model') if field(event, 'type') == 'response.completed' else None
            evidence.setdefault('codex_raw_models', []).append(model)
        return feed(self, event)
    assembler_cls.feed = observed


def verify_layout(root, manifest, digest):
    root = root.resolve(strict=True)
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise PolicyViolation('manifest mismatch')
    doc = json.loads(raw)
    # Parent gate validates platform, permissions, complete coverage and interpreter.
    # Child checks execution/import origins and rehashes the pinned files before imports.
    for relative, expected in doc['files'].items():
        path = root / relative
        if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink():
            raise PolicyViolation('runtime path escapes')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise PolicyViolation('runtime bytes changed')
    # Hermes loads <source>/.env at import, outside the selected-key rule; a packaged runtime must not ship one.
    if (root / 'hermes' / '.env').exists():
        raise PolicyViolation('runtime ships a source .env')
    if not Path(sys.executable).resolve().is_relative_to(root):
        raise PolicyViolation('external interpreter')
    if not Path(__file__).resolve().is_relative_to(root):
        raise PolicyViolation('external speaker')
    return root


def check_imports(root):
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename and not filename.startswith('<'):
            if not Path(filename).resolve().is_relative_to(root):
                raise PolicyViolation('external module origin')


def bind_session(agent_cls, session):
    original = agent_cls.__init__
    signature = inspect.signature(original)
    if 'session_id' not in signature.parameters:
        raise PolicyViolation('incompatible session constructor')
    def bound(self, *args, **kwargs):
        arguments = signature.bind(self, *args, **kwargs)
        arguments.arguments['session_id'] = session
        return original(*arguments.args, **arguments.kwargs)
    agent_cls.__init__ = bound


def private_file(profile, name):
    path = profile / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise PolicyViolation('credential file must be private and regular')
    return path


def main():
    parser = argparse.ArgumentParser()
    for name in ('runtime-root', 'manifest', 'profile', 'state-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('runtime-sha256', 'model', 'session-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--route', action='append', default=[])
    parser.add_argument('--served', action='append', default=[])
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    evidence = dict(schema='pelago.hermes.v1', requested_model=args.model, session_id=args.session_id,
                    text='', tools=[], tool_attempts=0, title_generation=False, runtime_guard=False,
                    runtime_sha256=args.runtime_sha256, credential_mode='profile-access-only-no-copy',
                    speaker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    calls=[], allowed_models=[args.model])
    code = 1
    try:
        if len(args.route) != 1:
            raise PolicyViolation('one route required')
        model, separator, provider = args.route[0].partition('=')
        if not separator or model != args.model or provider not in API_KEYS:
            raise PolicyViolation('invalid route')
        ROUTES[model] = provider
        served = []
        for value in args.served:
            requested, separator, name = value.partition('=')
            if not separator or requested != model or not name:
                raise PolicyViolation('invalid served route')
            served.append(name)
        if not served:
            raise PolicyViolation('missing served routes')
        SERVED[model] = tuple(served)
        root = verify_layout(args.runtime_root, args.manifest, args.runtime_sha256)
        home = args.state_root.resolve(strict=True)
        if home.stat().st_mode & 0o077 or home.stat().st_uid != os.geteuid():
            raise PolicyViolation('state must be private')
        # No developer HOME, site customization, PYTHONPATH or shell settings.
        for key in list(os.environ):
            if key not in {'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR',
                           'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE', 'PYTHONSAFEPATH'}:
                del os.environ[key]
        os.environ.update(HERMES_HOME=str(home), CODEX_HOME=str(home / 'disabled-codex-home'),
                          HERMES_SESSION_SOURCE='tool', HERMES_API_TIMEOUT='30',
                          HERMES_API_CALL_STALE_TIMEOUT='30')
        sys.dont_write_bytecode = True
        sys.path[:] = [str(root / 'hermes')] + [p for p in sys.path if p and Path(p).resolve().is_relative_to(root)]
        check_imports(root)
        prompt = sys.stdin.read()
        captured = BoundedTextCapture(8 * 1024 * 1024)
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(BoundedTextCapture(2 * 1024 * 1024, retain=False)):
            from dotenv import dotenv_values
            import yaml
            key = API_KEYS[provider]
            if key:
                values = dotenv_values(private_file(args.profile, '.env'), interpolate=False)
                if not values.get(key):
                    raise PolicyViolation('provider key unavailable')
                os.environ[key] = values[key]
                del values
            config = {'platform_toolsets': {'cli': []}, 'mcp_servers': {},
                      'agent': {'coding_context': False},
                      'auxiliary': {'title_generation': {'enabled': False}}, 'fallback_providers': [],
                      'security': {'tirith_enabled': False}}  # otherwise Hermes downloads a scanner binary at runtime
            (home / 'config.yaml').write_text(yaml.safe_dump(config))
            import hermes_cli.auth as auth
            import hermes_cli.auth_codex as codex_auth
            swaps = install_auth_guards(auth, codex_auth, args.profile, provider == 'openai-codex')
            import agent.auxiliary_client as aux
            swaps += install_aux_guards(aux)
            aux._read_codex_access_token = lambda: auth.resolve_codex_runtime_credentials()['api_key']
            import cli
            from run_agent import AIAgent
            from hermes_cli.stream_json import StreamJsonEmitter
            bind_session(AIAgent, args.session_id)
            install_guards(AIAgent, StreamJsonEmitter, evidence)
            import agent.codex_runtime as codex
            install_codex_evidence(codex._CodexResponseAssembler, codex._event_field, evidence)
            rebind_aliases(swaps)
            check_imports(root)
            try:
                cli.main(query=prompt, model=model, provider=provider, quiet=True, oneshot=True,
                         output_format='stream-json', ignore_rules=True, max_turns=2)
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
            check_imports(root)
        events = []
        for line in captured.getvalue().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        results = [e for e in events if isinstance(e, dict) and e.get('type') == 'result']
        if len(results) != 1 or not evidence['calls']:
            raise PolicyViolation('missing response evidence')
        result = results[0]
        if result.get('session_id') != args.session_id or result.get('exit_code'):
            raise PolicyViolation('failed or mismatched result')
        evidence['text'] = result['text']
    except (Exception, PolicyViolation, Terminated):
        evidence['error'] = 'runtime_policy_or_provider_failure'
        code = 1
    print(json.dumps(evidence, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
