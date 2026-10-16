"""Bound login work for this single-process, private deployment."""

import ipaddress
import threading
import time
from collections import deque

from fastapi import HTTPException, Request
from app import config


WINDOW_SECONDS = 60
MAX_KEYS = 4096
_attempts: dict[tuple[str, ...], deque[float]] = {}
_lock = threading.Lock()


def check_login_limit(peer: str, username: str) -> None:
    now = time.monotonic()
    keys = [("peer", peer), ("account", peer, username.casefold())]
    with _lock:
        for key, attempts in list(_attempts.items()):
            while attempts and attempts[0] <= now - WINDOW_SECONDS:
                attempts.popleft()
            if not attempts:
                del _attempts[key]
        if any(
            len(_attempts.get(key, ())) >= limit for key, limit in zip(keys, (60, 10))
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts; try again in one minute",
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )
        # An attacker filling the bounded map must not lock every new source out.
        while len(_attempts) + sum(key not in _attempts for key in keys) > MAX_KEYS:
            oldest = min((key for key in _attempts if key not in keys),
                         key=lambda key: _attempts[key][-1])
            del _attempts[oldest]
        for key in keys:
            _attempts.setdefault(key, deque()).append(now)


def clear_login_failures(peer: str, username: str) -> None:
    with _lock:
        _attempts.pop(("account", peer, username.casefold()), None)


def login_source(request: Request) -> str:
    """Requires Uvicorn --no-proxy-headers so client remains the transport peer."""
    peer = request.client.host if request.client else "unknown"
    try:
        peer = str(ipaddress.ip_address(peer))
        trusted = config.settings.trusted_proxy_ips()
    except ValueError:
        return peer
    if peer not in trusted:
        return peer
    forwarded = request.headers.getlist("x-forwarded-for")
    if len(forwarded) != 1 or len(forwarded[0]) > 2048:
        return peer
    try:
        chain = [str(ipaddress.ip_address(item.strip())) for item in forwarded[0].split(",")]
    except ValueError:
        return peer
    for hop in reversed(chain):
        if hop not in trusted:
            return hop
    return peer
