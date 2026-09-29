"""SaaS Evaluator CLI — single entry point, terminal only (no server, no web UI)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import settings
from .storage.files import get_run, list_runs, read_json, read_jsonl

# The legacy Windows console codepage (cp1252) can't encode the emoji/symbols used
# in the output; reconfigure to UTF-8 (supported by every modern terminal).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

app = typer.Typer(
    help="SaaS Evaluator — scores a SaaS application across pillars (security, UX, performance, …).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
runs_app = typer.Typer(help="Browse past evaluations.")
checks_app = typer.Typer(help="Inspect the scoring rubric.")
app.add_typer(runs_app, name="runs")
app.add_typer(checks_app, name="checks")

console = Console()


@app.command()
def evaluate(
    url: str = typer.Argument(..., help="URL of the SaaS to evaluate (e.g. https://app.example.com)."),
    deterministic: bool = typer.Option(
        False, "--deterministic", help="Run ONLY the deterministic checks (no AI). Default: deterministic + AI."
    ),
    a11y: bool = typer.Option(
        False, "--a11y", help="Also evaluate the Accessibility & Multi-device pillar (off by default)."
    ),
    profile: str = typer.Option("default", "--profile", "-p", help="Weights profile: default | enterprise | smb | path to a YAML."),
    max_pages: Optional[int] = typer.Option(None, "--max-pages", help=f"Pages visited in the browser (default {settings.max_pages})."),
    mode: str = typer.Option(
        "background", "--mode", "-m", help="'background' = headless with a spinner; 'visible' = opens the browser and logs every step."
    ),
    credentials: Optional[Path] = typer.Option(
        None, "--credentials", "-c", help="YAML file with username/password (+ optional login_url): automatic login.",
    ),
    manual_login: bool = typer.Option(
        False, "--manual-login", help="Open a browser window, log in yourself (SSO/MFA ok), press Enter to continue.",
    ),
    login: bool = typer.Option(
        False, "--login", help="Automatic login with SAAS_EVAL_USER/SAAS_EVAL_PASS from .env (prompted if missing).",
    ),
    app_pages: int = typer.Option(8, "--app-pages", help="In-app pages visited after login."),
) -> None:
    """Evaluate a SaaS application and produce a scored report.

    [bold]Log in[/bold] with --credentials FILE, --manual-login or --login to evaluate the product itself
    (in-app speed, stability, session security, in-app UX). Without them only the public surface is scored.

    [bold]Default[/bold]: 7 pillars (no A11Y), deterministic + AI.
    """
    if mode not in ("background", "visible"):
        console.print("[red]--mode must be 'background' or 'visible'.[/red]")
        raise typer.Exit(code=2)

    from .collectors.urls import normalize_target
    from .pipeline import EvaluateOptions, evaluate_target
    from .scoring.engine import ProfileError

    try:
        normalize_target(url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    from .credentials import Credentials, CredentialsError, load_credentials_file

    if sum([bool(credentials), manual_login, login]) > 1:
        console.print("[red]Use only one of --credentials, --manual-login, --login.[/red]")
        raise typer.Exit(code=2)
    creds: Credentials | None = None
    login_method = "none"
    if credentials:
        try:
            creds = load_credentials_file(credentials)
        except CredentialsError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
        login_method = "credentials"
    elif login:
        # Collected here (never logged) and kept in memory only.
        creds = Credentials(
            username=settings.login_user or typer.prompt("Login (e-mail)"),
            password=settings.login_pass or typer.prompt("Password", hide_input=True),
        )
        login_method = "credentials"
    elif manual_login:
        login_method = "manual"

    opts = EvaluateOptions(
        deterministic=deterministic,
        a11y=a11y,
        profile=profile,
        max_pages=max_pages or settings.max_pages,
        mode=mode,
        login=login_method,
        credentials=creds,
        app_pages=app_pages,
    )
    try:
        evaluate_target(url, opts, settings, console)
    except ProfileError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    except RuntimeError as exc:  # actionable setup errors (claude CLI missing, etc.)
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        raise typer.Exit(code=130)


@runs_app.command("list")
def runs_list() -> None:
    """List past evaluations (most recent first)."""
    runs = list_runs(settings.data_dir)
    if not runs:
        console.print("No evaluations yet.")
        return
    table = Table(header_style="bold")
    for col in ("Run", "Target", "Mode", "Pillars", "Grade", "Score", "Coverage", "Status"):
        table.add_column(col)
    for r in runs:
        score = r.get("overall")
        table.add_row(
            r["run_id"],
            r.get("target_url", "?"),
            r.get("mode_label", r.get("mode", "?")),
            str(len(r.get("pillars", []))),
            r.get("grade") or "—",
            f"{score:.1f}" if isinstance(score, (int, float)) else "—",
            f"{r['coverage']:.0%}" if isinstance(r.get("coverage"), (int, float)) else "—",
            r.get("status", "?"),
        )
    console.print(table)


def _run_dir(run_id: str) -> Path:
    if get_run(settings.data_dir, run_id) is None:
        console.print(f"[red]Run '{run_id}' not found (see 'saas-eval runs list').[/red]")
        raise typer.Exit(code=2)
    return settings.data_dir / "runs" / run_id


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run id (see 'saas-eval runs list')."),
    fmt: str = typer.Option("md", "--format", "-f", help="md | json"),
    open_file: bool = typer.Option(False, "--open", help="Open the report with the OS default viewer."),
    rerender: bool = typer.Option(False, "--rerender", help="Re-render report.md from scores.json (no re-collection)."),
) -> None:
    """Print the path of a run's report (optionally re-render or open it)."""
    run_dir = _run_dir(run_id)
    scores_path = run_dir / "scores.json"
    if not scores_path.exists():
        console.print(f"[yellow]Run found but it has no scores (status: {get_run(settings.data_dir, run_id).get('status')}).[/yellow]")
        raise typer.Exit(code=1)
    if rerender:
        from .report.markdown import render_markdown

        (run_dir / "report.md").write_text(render_markdown(read_json(scores_path)), encoding="utf-8")
    path = scores_path if fmt == "json" else run_dir / "report.md"
    console.print(str(path))
    if open_file:
        _open_in_os(path)


@app.command()
def compare(run_ids: list[str] = typer.Argument(..., help="Two or more run ids to compare side by side.")) -> None:
    """Compare evaluations side by side (like the LeanIX vendor template)."""
    docs = []
    for run_id in run_ids:
        path = _run_dir(run_id) / "scores.json"
        if not path.exists():
            console.print(f"[red]Run '{run_id}' has no scores.[/red]")
            raise typer.Exit(code=2)
        docs.append(read_json(path))

    from .terminal import GRADE_COLOR, score_color

    table = Table(title="Comparison", header_style="bold", show_lines=True)
    table.add_column("Pillar")
    for d in docs:
        table.add_column(f"{d['run']['host']}\n[dim]{d['run']['mode_label']}[/dim]", justify="center")
    codes: list[str] = []
    for d in docs:
        for p in d["evaluation"]["pillars"]:
            if p["code"] not in codes:
                codes.append(p["code"])
    for code in codes:
        row, name = [], code
        for d in docs:
            p = next((p for p in d["evaluation"]["pillars"] if p["code"] == code), None)
            if p is None:
                row.append("[dim]not run[/dim]")
                continue
            name = f"{p['name']} ({code})"
            s = "—" if p["score"] is None else f"{p['score']:.1f}"
            row.append(f"[{score_color(p['score'])}]{s}[/] [dim]{p['coverage']:.0%}[/dim]")
        table.add_row(name, *row)
    overall_row = []
    for d in docs:
        e = d["evaluation"]
        s = "—" if e["overall"] is None else f"{e['overall']:.1f}"
        overall_row.append(f"[{GRADE_COLOR.get(e['grade'], 'white')}]{e['grade']} ({s})[/] [dim]{e['coverage']:.0%}[/dim]")
    table.add_row("[bold]Overall[/bold]", *overall_row)
    console.print(table)
    pillar_sets = {tuple(d["run"]["pillars"]) for d in docs}
    modes = {d["run"]["mode"] for d in docs}
    if len(pillar_sets) > 1 or len(modes) > 1:
        console.print("[yellow]⚠ Runs differ in pillars and/or mode — overall scores are not directly comparable.[/yellow]")
    console.print("[dim]Cells show score and coverage.[/dim]")


@checks_app.command("list")
def checks_list(
    pillar: Optional[str] = typer.Option(None, "--pillar", help="Only this pillar (e.g. SEC)."),
) -> None:
    """Print the scoring rubric (every check, its method and points)."""
    from .checks.registry import checks_for
    from .models import PILLARS

    codes = [pillar.upper()] if pillar else list(PILLARS)
    unknown = [c for c in codes if c not in PILLARS]
    if unknown:
        console.print(f"[red]Unknown pillar {unknown}. Pillars: {', '.join(PILLARS)}[/red]")
        raise typer.Exit(code=2)
    for code in codes:
        p = PILLARS[code]
        table = Table(title=f"{p.name} ({code}){' — opt-in: --a11y' if p.opt_in else ''}", header_style="bold", title_justify="left")
        table.add_column("Id")
        table.add_column("Check")
        table.add_column("Method")
        table.add_column("Pts", justify="right")
        for d in checks_for([code]):
            table.add_row(d.id, d.title + (" [red](critical)[/red]" if d.critical else ""), d.method, f"{d.max_points:g}")
        console.print(table)


_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_LEVEL_COLOR = {"DEBUG": "dim", "INFO": "white", "WARN": "yellow", "ERROR": "red"}
_BASE_KEYS = ("ts", "run_id", "phase", "level", "type")


@app.command()
def logs(
    run_id: str = typer.Argument(..., help="Run id."),
    level: Optional[str] = typer.Option(None, "--level", "-l", help="Minimum level: DEBUG < INFO < WARN < ERROR."),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Only events of this type (e.g. page, verdict, check)."),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON lines."),
) -> None:
    """Show a run's structured event log (events.jsonl), filterable by level/type."""
    events = read_jsonl(_run_dir(run_id) / "events.jsonl")
    min_level = _LEVEL_ORDER.get((level or "DEBUG").upper(), 0)
    shown = 0
    for ev in events:
        if _LEVEL_ORDER.get(ev.get("level", "INFO"), 1) < min_level or (type_filter and ev.get("type") != type_filter):
            continue
        shown += 1
        if as_json:
            console.print_json(data=ev)
            continue
        lvl = ev.get("level", "INFO")
        ts = str(ev.get("ts", "")).split("T")[-1].split("+")[0][:8]
        payload = {k: v for k, v in ev.items() if k not in _BASE_KEYS}
        console.print(
            f"[dim]{ts}[/dim] {ev.get('phase', ''):<7} [{_LEVEL_COLOR.get(lvl, 'white')}]{lvl:<5}[/] "
            f"[cyan]{ev.get('type', '?')}[/cyan] {escape(str(payload))[:300]}"
        )
    if shown == 0:
        console.print("[yellow]No events match.[/yellow]")


def _open_in_os(path: Path) -> None:
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("win"):
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


if __name__ == "__main__":
    app()
