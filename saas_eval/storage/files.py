"""File persistence — no database. All state of a run lives in data/runs/<run-id>/.
JSON writes are atomic (temp file + rename) so nothing is corrupted if the process
is interrupted midway (Ctrl+C)."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "target"


def new_run_id(slug: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slug}-{secrets.token_hex(2)}"


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(tmp_path, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


@dataclass
class RunPaths:
    data_dir: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs" / self.run_id

    @property
    def run_json(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def events_jsonl(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def evidence_json(self) -> Path:
        return self.run_dir / "evidence.json"

    @property
    def scores_json(self) -> Path:
        return self.run_dir / "scores.json"

    @property
    def report_md(self) -> Path:
        return self.run_dir / "report.md"

    @property
    def screenshots_dir(self) -> Path:
        return self.run_dir / "screenshots"

    @property
    def ai_cache_dir(self) -> Path:
        return self.run_dir / "ai"


def create_run(data_dir: Path, target_url: str, slug: str, options: dict[str, Any]) -> RunPaths:
    paths = RunPaths(data_dir=data_dir, run_id=new_run_id(slug))
    paths.screenshots_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        paths.run_json,
        {
            "run_id": paths.run_id,
            "target_url": target_url,
            "status": "running",
            "started_at": utc_now_iso(),
            "finished_at": None,
            "overall": None,
            "grade": None,
            **options,
        },
    )
    return paths


def finalize_run(paths: RunPaths, **fields: Any) -> None:
    data = read_json(paths.run_json)
    data.update({"status": "done", "finished_at": utc_now_iso(), **fields})
    atomic_write_json(paths.run_json, data)


def list_runs(data_dir: Path) -> list[dict[str, Any]]:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        try:
            runs.append(read_json(run_json))
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def get_run(data_dir: Path, run_id: str) -> dict[str, Any] | None:
    run_json = data_dir / "runs" / run_id / "run.json"
    if not run_json.exists():
        return None
    return read_json(run_json)
