"""Terminal output (Rich): live progress per phase and the final score table."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .report.markdown import top_issues

GRADE_COLOR = {"A": "bold green", "B": "green", "C": "yellow", "D": "dark_orange", "F": "red", "N/A": "dim"}


def score_color(score: float | None) -> str:
    if score is None:
        return "dim"
    return "green" if score >= 70 else "yellow" if score >= 55 else "dark_orange" if score >= 40 else "red"


class Reporter:
    def __init__(self, console: Console, verbose: bool) -> None:
        self.console = console
        self.verbose = verbose
        self._status = None

    def start(self, target: str, mode: str, pillars: list[str], run_id: str) -> None:
        a11y = "A11Y active" if "A11Y" in pillars else "A11Y off (use --a11y)"
        self.console.print(
            Panel.fit(
                f"[bold]{escape(target)}[/bold]\nMode: {mode} · Pillars: {len(pillars)} ({a11y})\nRun: [dim]{run_id}[/dim]",
                title="SaaS Evaluator",
                border_style="cyan",
            )
        )

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if self.verbose:
            self.console.rule(f"[bold cyan]{name}")
            yield
            return
        with self.console.status(f"[cyan]{name}…") as status:
            self._status = status
            try:
                yield
            finally:
                self._status = None
        self.console.print(f"[green]✓[/green] {name}")

    async def ask(self, message: str) -> None:
        """Pauses the spinner and waits for Enter (used by --manual-login)."""
        if self._status is not None:
            self._status.stop()
        self.console.print(f"\n[bold yellow]→[/bold yellow] {message}")
        await asyncio.to_thread(input)
        if self._status is not None:
            self._status.start()

    def progress(self, message: str) -> None:
        if self.verbose:
            self.console.print(f"  [dim]•[/dim] {escape(message)}")
        elif self._status is not None:
            self._status.update(f"[cyan]{escape(message[:100])}…")

    def result(self, doc: dict[str, Any], report_path: str) -> None:
        ev = doc["evaluation"]
        table = Table(title="Scores", header_style="bold", show_lines=False)
        table.add_column("Pillar")
        table.add_column("Score", justify="right")
        table.add_column("Grade", justify="center")
        table.add_column("Coverage", justify="right")
        table.add_column("Biggest gap")
        issues = top_issues(ev, limit=100)
        for p in ev["pillars"]:
            gap = next((i for i in issues if i["pillar"] == p["code"]), None)
            score = "—" if p["score"] is None else f"{p['score']:.1f}" + (" ⛔" if p["capped"] else "")
            table.add_row(
                f"{p['name']} [dim]({p['code']})[/dim]",
                f"[{score_color(p['score'])}]{score}[/]",
                f"[{GRADE_COLOR.get(p['grade'], 'white')}]{p['grade']}[/]",
                f"{p['coverage']:.0%}",
                escape(f"{gap['id']} {gap['title']}") if gap else "[dim]—[/dim]",
            )
        self.console.print(table)
        for d in ev["dealbreakers"]:
            self.console.print(f"[red]⛔ Dealbreaker:[/red] {escape(d)}")
        overall = "—" if ev["overall"] is None else f"{ev['overall']:.1f}/100"
        self.console.print(
            f"\n[bold]Overall:[/bold] [{GRADE_COLOR.get(ev['grade'], 'white')}]{ev['grade']} ({overall})[/] · "
            f"coverage {ev['coverage']:.0%} · {ev['confidence']} confidence"
        )
        uri = Path(report_path).resolve().as_uri()  # file:///C:/… on Windows, file:///home/… elsewhere
        self.console.print(f"Report: [link={uri}]{escape(report_path)}[/link]")
