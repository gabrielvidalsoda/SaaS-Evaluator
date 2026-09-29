"""Test-account credentials for authenticated evaluations.

Sources (first match wins): `--credentials <file.yaml>`, then SAAS_EVAL_USER /
SAAS_EVAL_PASS (.env), then an interactive prompt. Credentials live only in memory:
never logged, never written to evidence, never shown to the AI (the explorer types
them through a tool)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_USER_KEYS = ("username", "user", "login", "email")
_PASS_KEYS = ("password", "pass")


class CredentialsError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)
    login_url: str | None = None

    def __repr__(self) -> str:  # never print the password
        return f"Credentials(username={self.username!r}, password=<hidden>, login_url={self.login_url!r})"


def load_credentials_file(path: Path) -> Credentials:
    """YAML file with `username` (or login/email) and `password` (or pass);
    optional `login_url` when the login page isn't where the target URL redirects."""
    if not path.exists():
        raise CredentialsError(f"Credentials file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CredentialsError(f"Credentials file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CredentialsError(f"Credentials file {path} must be a mapping (username: ..., password: ...).")
    lower = {str(k).lower(): v for k, v in data.items()}
    user = next((str(lower[k]) for k in _USER_KEYS if lower.get(k)), None)
    password = next((str(lower[k]) for k in _PASS_KEYS if lower.get(k)), None)
    if not user or not password:
        raise CredentialsError(f"Credentials file {path} needs 'username' and 'password' keys.")
    return Credentials(username=user, password=password, login_url=lower.get("login_url") or None)
