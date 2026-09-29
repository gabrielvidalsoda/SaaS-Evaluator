"""Application settings — read from environment variables (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Loads .env from the current directory (repo root), if present.
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    model: str
    max_pages: int
    data_dir: Path
    profiles_dir: Path
    prompts_dir: Path
    login_user: str | None
    login_pass: str | None


def load_settings() -> Settings:
    return Settings(
        model=os.getenv("SAAS_EVAL_MODEL", "sonnet"),
        max_pages=int(os.getenv("SAAS_EVAL_MAX_PAGES", "12")),
        data_dir=Path(os.getenv("SAAS_EVAL_DATA_DIR", "data")),
        profiles_dir=PROJECT_ROOT / "profiles",
        prompts_dir=PROJECT_ROOT / "prompts",
        login_user=os.getenv("SAAS_EVAL_USER") or None,
        login_pass=os.getenv("SAAS_EVAL_PASS") or None,
    )


settings = load_settings()
