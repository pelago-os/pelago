"""Experimental adapter evidence parsers; live runtime acceptance is separate.

These validate receipts, not OS isolation or credential behavior. No returned
error includes provider output, exception messages, credential paths or secrets.
"""
import json
import re


def failed(code='missing_evidence'):
    return {'status': 'failed', 'error_code': code}


def _tool_use(node):
    if isinstance(node, dict):
        return node.get('type') in ('tool_use', 'server_tool_use') or any(_tool_use(v) for v in node.values())
    return isinstance(node, list) and any(_tool_use(v) for v in node)


def parse_claude(stdout, exit_code, requested, served_routes, expected_session):
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if not events or not all(isinstance(e, dict) for e in events):
            return failed()
        init = [e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init']
        results = [e for e in events if e.get('type') == 'result']
        if len(init) != 1 or len(results) != 1:
            return failed()
        init, result = init[0], results[0]
        if init.get('tools') != [] or init.get('mcp_servers') != [] or _tool_use(events):
            return failed('tool_surface_not_empty')
        if (not isinstance(expected_session, str) or not expected_session
                or init.get('session_id') != expected_session or result.get('session_id') != expected_session):
            return failed('session_mismatch')
        if type(result.get('is_error')) is not bool or not isinstance(result.get('result'), str):
            return failed()
        if exit_code != 0 or result['is_error']:
            return failed('provider_error')
        served_names = served_routes.get(requested, ()) if isinstance(served_routes, dict) else ()
        usage = result.get('modelUsage')
        if (not isinstance(usage, dict) or not usage or not isinstance(requested, str) or not requested
                or not served_names or any(not isinstance(v, dict) for v in usage.values())):
            return failed()
        if any(m not in served_names for m in usage):
            return failed('model_mismatch')
        # init.model is request metadata, never proof of what answered.
        return {'status': 'accepted', 'provider': 'claude_code', 'support_tier': 'experimental',
                'requested_model': requested, 'model': ','.join(sorted(usage)),
                'text': result['result'], 'provider_session_id': expected_session,
                'fallback_used': False, 'tool_surface': {'tools': [], 'mcp_servers': []},
                'model_evidence': 'result.modelUsage'}
    except (ValueError, TypeError, AttributeError, RecursionError):
        return failed()


def parse_hermes(stdout, exit_code, requested, routes, expected_session, runtime_sha256, speaker_sha256):
    """Exact route, session and approved-byte binding; no fallback in this milestone."""
    try:
        data = json.loads(stdout)
        if (not isinstance(data, dict) or not isinstance(requested, str) or not requested
                or not isinstance(expected_session, str) or not expected_session):
            return failed()
        if not all(isinstance(x, str) and re.fullmatch(r'[0-9a-f]{64}', x) for x in (runtime_sha256, speaker_sha256)):
            return failed()
        if (data.get('schema') != 'pelago.hermes.v1'
                or data.get('credential_mode') != 'profile-access-only-no-copy'
                or data.get('requested_model') != requested
                or not expected_session or data.get('session_id') != expected_session
                or data.get('runtime_sha256') != runtime_sha256
                or data.get('speaker_sha256') != speaker_sha256
                or data.get('runtime_guard') is not True
                or data.get('title_generation') is not False
                or data.get('tools') != [] or type(data.get('tool_attempts')) is not int
                or data['tool_attempts'] != 0 or not isinstance(data.get('text'), str)):
            return failed()
        calls = data.get('calls')
        if not isinstance(calls, list) or not calls or not routes:
            return failed()
        served = set()
        for call in calls:
            if not isinstance(call, dict):
                return failed()
            provider, model = call.get('provider'), call.get('served')
            source = 'raw.response.completed.model' if provider == 'openai-codex' else 'sdk.response.model'
            if (call.get('source') != source or call.get('tools') != []
                    or call.get('requested') != requested or call.get('session_id') != expected_session
                    or (provider, model) not in routes):
                return failed('route_or_tool_mismatch')
            served.add(model)
        if exit_code != 0 or data.get('error'):
            return failed('provider_error')
        return {'status': 'accepted', 'provider': 'hermes', 'support_tier': 'experimental',
                'requested_model': requested, 'model': ','.join(sorted(served)), 'text': data['text'],
                'provider_session_id': expected_session, 'fallback_used': False,
                'tool_surface': {'tools': [], 'tool_attempts': 0, 'title_generation': False},
                'runtime_sha256': runtime_sha256, 'speaker_sha256': speaker_sha256}
    except (ValueError, TypeError, AttributeError, RecursionError):
        return failed()
