# saas-evaluator — instructions for Claude Code

Terminal tool that scores a SaaS application (by URL) across evaluation pillars.
See `README.md` for usage and the scoring model; the design rationale is in the
plan this project was built from (pillars from the Josys checklist, weighting from
the LeanIX vendor template in `../References/`).

## Invariants — keep them when changing code

- **Passive only.** Collectors may only issue GET/HEAD requests to public pages and
  the fixed well-known paths in `collectors/http_probe.py`. No payloads, fuzzing,
  form submissions (except the user's own login) or load testing.
- **Logged-in evaluation is read-only.** The in-app crawl follows only safe same-host
  links (`collectors/auth.py:UNSAFE_LINK_RX`); the explorer's clicks are guarded by
  `ai/explorer.py:UNSAFE_CLICK_RX`. The session (storage_state) lives in memory only —
  never write it to disk or evidence.
- **AI never sets scores.** AI returns verdicts through `submit_verdicts`; positive
  verdicts must pass `ai/verify.py` (verbatim quote found in collected text).
  Scores are computed only by `checks/registry.py` + `scoring/engine.py`.
- **Check functions are pure** (`Evidence -> Outcome | None`) and know nothing about
  run modes; the registry applies `auto` / `ai` / `auto+ai` rules.
- **A11Y is opt-in** (`--a11y`); **deterministic-only** is `--deterministic`. The
  default is 7 pillars, deterministic + AI.
- **Never log or store secrets**: cookie values are stripped, credentials are typed
  by `type_credential` (never shown to the model), `Credentials.__repr__` hides the
  password, and `storage/events.py` redacts. Credential files go in `credentials/`
  (git-ignored).

## Conventions
@.claude/rules/logging.md
