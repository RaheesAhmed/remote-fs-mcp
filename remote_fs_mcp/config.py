"""Runtime configuration, read once from environment variables (or a .env file)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val not in (None, "") else default


def _list(name: str) -> list[str]:
    val = os.environ.get(name, "")
    return [v.strip() for v in val.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # Network
    host: str = "0.0.0.0"
    port: int = 8765
    path: str = "/mcp"
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)

    # Auth
    tokens: list[str] = field(default_factory=list)
    token_was_generated: bool = False
    auth_disabled: bool = False
    url_token: bool = False  # also accept https://host/<token>[/mcp] for clients that can't set headers

    # Sandbox
    root: Path = Path.cwd()
    read_only: bool = False
    allow_commands: bool = True
    allow_delete: bool = True
    allow_hidden: bool = True
    denied_globs: list[str] = field(default_factory=list)

    # Limits
    max_read_bytes: int = 5_000_000
    max_write_bytes: int = 20_000_000
    max_output_chars: int = 100_000
    default_command_timeout: int = 120
    max_command_timeout: int = 1800
    max_processes: int = 16
    shell: str | None = None


def load_settings() -> Settings:
    _load_dotenv(Path.cwd() / ".env")

    tokens = _list("MCP_FS_TOKENS") or _list("MCP_FS_TOKEN")
    generated = False
    auth_disabled = _bool("MCP_FS_DISABLE_AUTH", False)
    if not tokens and not auth_disabled:
        tokens = [secrets.token_urlsafe(32)]
        generated = True

    root = Path(os.environ.get("MCP_FS_ROOT") or Path.cwd()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    path = os.environ.get("MCP_FS_PATH", "/mcp")
    if not path.startswith("/"):
        path = "/" + path

    return Settings(
        host=os.environ.get("MCP_FS_HOST", "0.0.0.0"),
        port=_int("MCP_FS_PORT", _int("PORT", 8765)),
        path=path,
        allowed_hosts=_list("MCP_FS_ALLOWED_HOSTS"),
        allowed_origins=_list("MCP_FS_ALLOWED_ORIGINS"),
        tokens=tokens,
        token_was_generated=generated,
        auth_disabled=auth_disabled,
        url_token=_bool("MCP_FS_URL_TOKEN", False),
        root=root,
        read_only=_bool("MCP_FS_READ_ONLY", False),
        allow_commands=_bool("MCP_FS_ALLOW_COMMANDS", True),
        allow_delete=_bool("MCP_FS_ALLOW_DELETE", True),
        allow_hidden=_bool("MCP_FS_ALLOW_HIDDEN", True),
        denied_globs=_list("MCP_FS_DENY_GLOBS"),
        max_read_bytes=_int("MCP_FS_MAX_READ_BYTES", 5_000_000),
        max_write_bytes=_int("MCP_FS_MAX_WRITE_BYTES", 20_000_000),
        max_output_chars=_int("MCP_FS_MAX_OUTPUT_CHARS", 100_000),
        default_command_timeout=_int("MCP_FS_COMMAND_TIMEOUT", 120),
        max_command_timeout=_int("MCP_FS_MAX_COMMAND_TIMEOUT", 1800),
        max_processes=_int("MCP_FS_MAX_PROCESSES", 16),
        shell=os.environ.get("MCP_FS_SHELL") or None,
    )
