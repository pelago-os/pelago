"""States, transitions and the character limit (contract sections 4, 6, 7.2)."""

from __future__ import annotations

import os
import unicodedata

# Meeting states (section 4)
DRAFT, RUNNING, PAUSED, DORMANT, COMPLETED, CLOSED = (
    "draft", "running", "paused", "dormant", "completed", "closed")
TERMINAL_MEETING = frozenset({COMPLETED, CLOSED})
STOPPED = frozenset({PAUSED, DORMANT})

MEETING_TRANSITIONS = {
    DRAFT: {RUNNING, CLOSED},
    RUNNING: {PAUSED, COMPLETED, CLOSED},
    PAUSED: {RUNNING, DORMANT, CLOSED},
    DORMANT: {RUNNING, CLOSED},
    COMPLETED: set(),
    CLOSED: set(),
}

EXIT_REASONS = frozenset({
    "completed", "user_pause", "user_close", "participant_close_dormant", "turn_failed",
    "turn_timeout", "time_budget", "no_active_agents", "interrupted_restart",
    "participant_revoked", "material_acl_changed", "adapter_unavailable", "agent_policy_changed",
})

# Attempt states (section 7.2)
QUEUED, CLAIMED, ATTEMPT_RUNNING, CANCEL_REQUESTED, QUARANTINED = (
    "queued", "claimed", "running", "cancel_requested", "quarantined")
ACCEPTED, FAILED, CANCELLED, INTERRUPTED = "accepted", "failed", "cancelled", "interrupted"
SLOT_HOLDING = (CLAIMED, ATTEMPT_RUNNING, CANCEL_REQUESTED, QUARANTINED)
NON_TERMINAL = (QUEUED,) + SLOT_HOLDING
TERMINAL_ATTEMPT = frozenset({ACCEPTED, FAILED, CANCELLED, INTERRUPTED})

CANCEL_REASONS = frozenset({
    "user_pause", "user_close", "left", "removed", "timeout", "time_budget",
    "participant_revoked", "material_acl_changed", "shutdown", "agent_policy_changed",
})

# Slots (section 5)
SLOT_OPEN, SLOT_FILLED, SLOT_SKIPPED = "open", "filled", "skipped"

# Budgets (section 7.5): (default, low, high)
BUDGETS = {
    "rounds": (2, 1, 6),
    "turn_timeout_s": (120, 10, 600),
    "meeting_timeout_s": (600, 60, 3600),
}
AGENT_PARTICIPANTS = (2, 8)
WRAP_ROUND = 99          # the wrap-up slot's round: after every real round, never a round itself (section 5)
WRAP_MAX_CHARS = 1200    # the wrap-up turn's own length limit
MAX_CHARS_RANGE = (20, 4000)
STALE_AFTER_S = 86_400


def default_max_chars() -> int:
    try:
        value = int(os.environ.get("PELAGO_MEETING_MAX_CHARS", "100"))
    except ValueError:
        value = 100
    low, high = MAX_CHARS_RANGE
    return min(high, max(low, value))


def global_slots() -> int:
    try:
        return max(1, int(os.environ.get("PELAGO_MEETING_SLOTS", "2")))
    except ValueError:
        return 2


def approval_required() -> bool:
    return os.environ.get("PELAGO_MEETING_REQUIRE_APPROVAL", "0").strip() == "1"


def can_transition(current: str, target: str) -> bool:
    return target in MEETING_TRANSITIONS.get(current, set())


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def apply_char_limit(raw_text: str, max_chars: int, exempt: bool) -> dict:
    """Section 6: count code points after NFC + strip; only `text` reaches later turns."""
    norm = normalize(raw_text)
    count = len(norm)
    truncated = (not exempt) and count > max_chars
    return {
        "raw_text": raw_text,
        "text": norm[:max_chars] if truncated else norm,
        "char_count": count,
        "truncated": truncated,
    }
