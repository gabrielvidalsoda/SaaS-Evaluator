# Structured logging — project convention

Adapted from agente-qa's logging rule: structured JSONL events, consistent levels,
never log secrets. No aggregation/alerting infrastructure — this is a local,
single-process tool.

## Where it lives

Every run writes `data/runs/<run-id>/events.jsonl` — one JSON object per
consolidated event, via `EventLog.log()` (`saas_eval/storage/events.py`). This is
the *why/how* trail; `report.md` / `scores.json` are the results.

## Event shape

```json
{"ts": "2026-09-28T19:19:16+00:00", "run_id": "...", "phase": "collect", "level": "INFO", "type": "page", "...": "..."}
```

| Field | Meaning |
|---|---|
| `ts` | ISO-8601 UTC |
| `run_id` | repeated on every line (so logs of several runs can be concatenated/grepped) |
| `phase` | `init` · `collect` · `auth` · `ai` · `score` |
| `level` | `DEBUG` · `INFO` · `WARN` · `ERROR` |
| `type` | `run_start`, `page`, `collect`, `collect_error`, `discovery`, `ai_start`, `action`, `tool_error`, `verdict`, `verdict_rejected`, `ai_error`, `check`, `score`, `finish`, `fatal_error` |

One event per consolidated action (one page visit, one verdict, one check) — never
fragment an action over several lines.

## Levels

| Level | When |
|---|---|
| `DEBUG` | per-check results (`check`) |
| `INFO` | normal flow: pages, discovery, verdicts, score, finish |
| `WARN` | worth a look: page errors/4xx, robots-skipped pages, rejected AI verdicts, failed probes |
| `ERROR` | real failures: `tool_error`, `ai_error`, `fatal_error` |

## Never log secrets

`EventLog.log()` redacts payload keys named `password`, `pass`, `text`, `secret`,
`token` (recursively). Cookie values are stripped at collection time. If a new
event carries free text that might contain a secret, use one of those key names or
extend `_SECRET_KEYS`.

## Adding an event type

Call `log.log("my_type", {...}, level=...)`; add a default level in
`_DEFAULT_LEVEL` (`storage/events.py`) if it's a new type.

## Reading

```bash
uv run saas-eval logs <run-id>                 # formatted
uv run saas-eval logs <run-id> --level warn    # WARN and ERROR only
uv run saas-eval logs <run-id> --type verdict
uv run saas-eval logs <run-id> --json          # raw lines (grep/jq)
```
