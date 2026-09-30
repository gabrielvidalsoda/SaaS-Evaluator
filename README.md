# SaaS Evaluator

[![tests](https://github.com/gabrielvidalsoda/SaaS-Evaluator/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielvidalsoda/SaaS-Evaluator/actions/workflows/tests.yml)

Terminal tool that evaluates a SaaS application from its URL and scores it
across pillars — **security, usability, performance, reliability, integration,
data governance, transparency** (and, on request, **accessibility**). It drives a
real browser with Playwright, logs in with your account to measure the product
itself, runs passive protocol probes, and optionally uses Claude to judge what only
a human reader could (compliance claims, SLA wording, visual usability) — with
every AI verdict backed by a machine-verified quote.

No UI, no server, no database: one command in, a Markdown report + JSON out.
Runs on **Linux, macOS and Windows**.

## Install

Prerequisites: **Python 3.11+** and [**uv**](https://docs.astral.sh/uv/getting-started/installation/)
(or pipx/pip).

```bash
git clone https://github.com/gabrielvidalsoda/SaaS-Evaluator.git
cd SaaS-Evaluator
uv sync
uv run playwright install chromium              # Linux: add --with-deps (installs system libraries)
uv run saas-eval --help
```

Or install it as a standalone command (no clone needed):

```bash
uv tool install git+https://github.com/gabrielvidalsoda/SaaS-Evaluator.git    # or: pipx install git+…
playwright install chromium                                                  # Linux: --with-deps
saas-eval --help
```

**AI layer (default mode) — optional.** It uses the
[`claude-agent-sdk`](https://code.claude.com/docs/en/agent-sdk), which runs the
[Claude Code](https://code.claude.com) CLI under the hood and reuses its login
(Claude subscription) — **no API key is configured in this tool**. Requires Node.js 18+:

```bash
npm install -g @anthropic-ai/claude-code
claude          # log in once
```

Without it, run everything with `--deterministic`.

Optional settings go in a `.env` file in the directory you run from (see [`.env.example`](.env.example)).

> Examples below use `uv run saas-eval …` (from a clone). If you installed it as a tool, drop `uv run`.

## Usage

### Evaluate a product you have an account on (recommended)

Logging in is what lets the tool measure the product itself — in-app speed,
stability (JS errors, failing API calls), session security and, with AI, the
in-app UX. Two ways:

```bash
# 1) Credentials file (automatic login)
mkdir credentials && cp credentials.example.yaml credentials/myapp.yaml   # edit it; credentials/ is git-ignored
uv run saas-eval evaluate https://app.example.com/dashboard --credentials credentials/myapp.yaml

# 2) Manual login — a browser window opens, you log in (SSO/MFA/CAPTCHA fine), press Enter
uv run saas-eval evaluate https://app.example.com/dashboard --manual-login
```

`--login` does the same as a credentials file using `SAAS_EVAL_USER`/`SAAS_EVAL_PASS`
from `.env` (prompted if missing). `--app-pages N` sets how many in-app pages are
crawled (default 8). The automatic login fills one- or two-step forms; if that fails
and AI is on, an AI agent retries (it types the credentials through a tool and never
sees them).

Logged-in crawling is **read-only**: it never follows or clicks links that could
log out, delete, pay, export, invite or change settings, and the session is kept in
memory only (never written to disk). Still, **use a test account** where possible.

### Modes and pillars

```bash
uv run saas-eval evaluate https://app.example.com                  # DEFAULT: 7 pillars, deterministic + AI
uv run saas-eval evaluate https://app.example.com --deterministic  # 7 pillars, deterministic only (no AI)
uv run saas-eval evaluate https://app.example.com --a11y           # + Accessibility & Multi-device pillar
uv run saas-eval evaluate https://app.example.com --deterministic --a11y
```

| Command | Pillars | Checks |
|---|---|---|
| `evaluate <url>` (**default**) | 7 (no A11Y) | deterministic + AI |
| `evaluate <url> --deterministic` | 7 (no A11Y) | deterministic only |
| `evaluate <url> --a11y` | 8 | deterministic + AI |
| `evaluate <url> --deterministic --a11y` | 8 | deterministic only |

Other options:

| Option | Meaning |
|---|---|
| `--credentials FILE` / `--manual-login` / `--login` | Log in first (see above). |
| `--app-pages 8` | In-app pages crawled after login. |
| `--profile enterprise` | Weights profile: `default`, `enterprise`, `smb`, or a path to your own YAML. |
| `--max-pages 20` | Public-site browser crawl budget (default 12). Discovered pages beyond it are still read as text. |
| `--mode visible` | Opens the browser window and logs every step (default `background`: headless + spinner). |

Afterwards:

```bash
uv run saas-eval runs list                     # all evaluations
uv run saas-eval report <run-id> --open        # open report.md   (--format json for scores.json)
uv run saas-eval compare <run-id> <run-id>     # side-by-side table (vendor comparison)
uv run saas-eval checks list [--pillar SEC]    # the full rubric
uv run saas-eval logs <run-id> --level warn    # structured event log
```

Each run lives in `data/runs/<run-id>/` (relative to where you run the command, or
`SAAS_EVAL_DATA_DIR`): `report.md`, `scores.json`, `evidence.json` (everything
collected), `events.jsonl` (log), `screenshots/`, `ai/verdicts.json`. Cookie values,
passwords and the logged-in session are never written there.

## How the score works

**Pillars** (weights of the `default` profile; they are relative and re-normalized
over the pillars that run):

| Pillar | Weight | What is measured |
|---|---:|---|
| SEC Security & Compliance | 25 | HTTPS/TLS, HSTS, CSP, headers, cookie flags, exposed files, security.txt, SPF/DMARC, MFA/SSO, attestations, third-party scripts, **logged-in session hardening** |
| UX Usability & User Experience | 20 | Nielsen heuristics (visual), navigation/CTA/search, broken links, JS errors, login form hygiene, help, **in-app stability**, **in-app experience (AI explorer)** |
| PERF Performance & Scalability | 15 | TTFB, Core Web Vitals (LCP/CLS/TBT), page weight, compression/HTTP2-3, caching, CDN, latency stability, **in-app page speed** |
| REL Reliability & Support | 10 | status page, uptime history, SLA, support channels, community |
| INT Integration & Extensibility | 8 | API docs, API standards, webhooks, integrations, SCIM/RBAC |
| DATA Data Governance & Portability | 7 | privacy policy freshness, DPA, residency, export, backup/DR, cookie consent |
| TRN Transparency, Cost & Innovation | 5 | public pricing, free trial, changelog cadence, roadmap, cancellation terms |
| A11Y Accessibility & Multi-device (`--a11y`) | 10 | axe-core WCAG checks, responsive overflow, tap targets, keyboard focus, mobile apps |

Checks in **bold** are logged-in (*n/a* without login).

**Math.** Each check earns `max_points × factor` (pass = 1, fail = 0, partial or
continuous metrics in between). A pillar's score is earned ÷ max of the checks
that were *evaluated*; checks that are *n/a* or *not evaluated* are excluded — the
**coverage %** tells you how much of the rubric was actually evaluated, and the
confidence label follows it (High ≥ 80%, Medium ≥ 50%). The **overall** is the
weighted average of pillar scores → grade A (≥ 85) · B (≥ 70) · C (≥ 55) · D (≥ 40) · F.
A failed **critical** check (no HTTPS) is a dealbreaker: pillar capped at 20,
overall at 50.

**Where AI is (and isn't) used.** About 60% of the points are deterministic
(protocol, browser metrics, discovery heuristics). AI judges only what needs
reading or looking: compliance/SLA/DPA claims, visual usability, in-app
exploration, and the executive summary. The AI **never sets a score** — it
submits a verdict per rubric item, and a `pass`/`partial` verdict is accepted only
if it quotes text that really exists on the collected pages (otherwise the check
becomes *not evaluated*). The prompts' hash is recorded in each run.

**Passive only.** GET requests to public pages plus a few well-known paths
(`/.well-known/security.txt`, `/.git/HEAD`, `/.env`), ~1 navigation per second,
robots.txt respected for the public crawl, honest `SaaS-Evaluator/x.y` user-agent.
No fuzzing, no payloads, no load testing — performance figures are external lab
signals measured from your machine and network. Only evaluate services you are
allowed to access.

Discovery understands English, Portuguese and Spanish page names (pricing/preços,
privacy/privacidade, terms/termos…).

## Customizing

- **Weights**: copy [`saas_eval/profiles/default.yaml`](saas_eval/profiles/default.yaml), edit, run with
  `--profile path/to/mine.yaml` (or point `SAAS_EVAL_PROFILES_DIR` at a folder of profiles).
- **Checks**: every rubric item is a small function in `saas_eval/checks/<pillar>.py`
  decorated with `@check(id, pillar, title, points, method, rubric=...)`.
- **AI behaviour**: edit the prompts in [`saas_eval/prompts/`](saas_eval/prompts) (or point
  `SAAS_EVAL_PROMPTS_DIR` at your own copies).

## Development

```bash
uv sync --extra dev
uv run playwright install chromium     # Linux: --with-deps
uv run pytest -q
```

Unit tests cover the scoring math, the pillar/mode matrix, check logic, discovery,
date parsing, credentials and the AI quote guardrail; integration tests run the
browser collectors and the scripted login against local fixture sites (no network,
no AI). CI runs them on Ubuntu, macOS and Windows.

## Layout

```text
saas_eval/
  cli.py            commands (evaluate, runs, report, compare, checks, logs)
  pipeline.py       collect → AI judge → checks → score → report
  credentials.py    credentials file / env / prompt (in memory only)
  collectors/       http_probe, tls_probe, dns_probe, browser, discovery, crawl, auth, a11y
  checks/           registry + one module per pillar (the rubric)
  ai/               agent (SDK driver), tools (judge tools), verify (quote guardrail),
                    judges (claims + UX sessions, summary), explorer (AI login + in-app exploration)
  scoring/engine.py weights, coverage, gates, grades
  report/markdown.py
  prompts/          judge.md, summary.md, explorer.md, login.md
  profiles/         default.yaml, enterprise.yaml, smb.yaml
  vendor/           axe-core (MPL-2.0)
tests/
```

## Third-party

- [axe-core](https://github.com/dequelabs/axe-core) is vendored unmodified in
  `saas_eval/vendor/axe.min.js` under the Mozilla Public License 2.0.
- Trademarks of evaluated products belong to their owners; reports reflect only
  automated observations at the time of the run.
