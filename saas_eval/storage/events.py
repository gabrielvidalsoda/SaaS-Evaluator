"""Structured run log (data/runs/<run-id>/events.jsonl) — one JSON object per
consolidated event. See `.claude/rules/logging.md` for the convention."""

from __future__ import annotations

from typing import Any

from .files import RunPaths, append_jsonl, utc_now_iso

# Default level per event type — can be overridden per call.
_DEFAULT_LEVEL = {
    "run_start": "INFO",
    "collect": "INFO",
    "collect_error": "WARN",
    "page": "INFO",
    "discovery": "INFO",
    "check": "DEBUG",
    "ai_start": "INFO",
    "verdict": "INFO",
    "verdict_rejected": "WARN",
    "ai_error": "ERROR",
    "action": "INFO",
    "tool_error": "ERROR",
    "score": "INFO",
    "finish": "INFO",
    "fatal_error": "ERROR",
}

# Payload keys whose value may contain a secret (typed credentials). Never logged in clear.
_SECRET_KEYS = {"password", "pass", "text", "secret", "token"}


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = {}
    for key, value in payload.items():
        if key.lower() in _SECRET_KEYS and isinstance(value, str):
            redacted[key] = f"<redacted:{len(value)} chars>"
        elif isinstance(value, dict):
            redacted[key] = redact(value)
        else:
            redacted[key] = value
    return redacted


class EventLog:
    def __init__(self, paths: RunPaths) -> None:
        self.paths = paths
        self.phase = "init"

    def log(self, event_type: str, payload: dict[str, Any] | None = None, level: str | None = None) -> None:
        append_jsonl(
            self.paths.events_jsonl,
            {
                "ts": utc_now_iso(),
                "run_id": self.paths.run_id,
                "phase": self.phase,
                "level": level or _DEFAULT_LEVEL.get(event_type, "INFO"),
                "type": event_type,
                **redact(payload or {}),
            },
        )
