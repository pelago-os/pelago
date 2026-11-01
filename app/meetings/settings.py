"""Feature availability; no configured adapter starts a model by itself."""
import os


def enabled() -> bool:
    return os.environ.get("PELAGO_MEETINGS_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def capabilities() -> dict:
    return {"enabled": enabled(), "support_tiers": ["mock", "experimental"], "project_drafts": False,
            "remote_cancellation": "unknown"}
