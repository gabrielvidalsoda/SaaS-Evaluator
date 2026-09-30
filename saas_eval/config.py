"""Application settings — read from environment variables (.env in the current directory).

Everything the tool ships with (prompts, weight profiles, axe-core) lives inside the
package, so it works the same whether it runs from a clone (`uv run saas-eval`) or
is installed as a tool (`uv tool install` / `pipx install`). Run data is written
relative to the directory you run the command from, unless SAAS_EVAL_DATA_DIR says
otherwise."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Loads .env from the current working directory, if present.
load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    model: str
    max_pages: int
    data_dir: Path
    profiles_dir: Path
    prompts_dir: Path
    login_user: str | None
    login_pass: str | None


def _dir(env: str, default: Path) -> Path:
    value = os.getenv(env)
    return Path(value).expanduser() if value else default


def load_settings() -> Settings:
    return Settings(
        model=os.getenv("SAAS_EVAL_MODEL", "sonnet"),
        max_pages=int(os.getenv("SAAS_EVAL_MAX_PAGES", "12")),
        data_dir=_dir("SAAS_EVAL_DATA_DIR", Path("data")),
        profiles_dir=_dir("SAAS_EVAL_PROFILES_DIR", PACKAGE_DIR / "profiles"),
        prompts_dir=_dir("SAAS_EVAL_PROMPTS_DIR", PACKAGE_DIR / "prompts"),
        login_user=os.getenv("SAAS_EVAL_USER") or None,
        login_pass=os.getenv("SAAS_EVAL_PASS") or None,
    )


settings = load_settings()
