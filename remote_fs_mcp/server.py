"""Remote Filesystem + Terminal MCP server (FastMCP 4, MCP 2026-07-28 + older clients)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import EntryMiddleware, StaticBearerVerifier, TokenSet
from .config import Settings, load_settings
from .fs_tools import register_fs_tools
from .sandbox import Sandbox
from .shell_tools import platform_info, register_shell_tools

INSTRUCTIONS = """\
Remote filesystem + terminal. All paths are relative to the server root (see server_info).
Tips for speed and low token use:
- Explore with list_directory (depth 2-3), find_files and search_text before reading.
- Read big files in ranges (read_file offset/limit); read several small files with read_many_files.
- Change existing files with edit_file (find/replace + diff) instead of rewriting them with write_file.
- run_command waits for exit; for dev servers/watchers use start_process + process_output(since=next_offset).
"""


def build_server(settings: Settings | None = None) -> FastMCP:
    s = settings or load_settings()
    sb = Sandbox(s)
    pm_holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            pm = pm_holder.get("pm")
            if pm:
                await pm.shutdown()  # never leave orphaned dev servers behind

    auth = None if s.auth_disabled else StaticBearerVerifier(TokenSet(s.tokens))
    mcp = FastMCP(
        name="remote-fs",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=lifespan,
        mask_error_details=True,  # unexpected exceptions don't leak internals; ToolError text still shows
    )

    register_fs_tools(mcp, s, sb)
    pm_holder["pm"] = register_shell_tools(mcp, s, sb)

    @mcp.tool(annotations={"read_only_hint": True, "idempotent_hint": True, "open_world_hint": False}, tags={"meta"})
    def server_info() -> dict[str, Any]:
        """Server root, OS/shell, enabled features and limits. Call once at the start."""
        return {
            "server": "remote-fs-mcp",
            "version": __version__,
            "root": str(s.root),
            **platform_info(s),
            "read_only": s.read_only,
            "commands_enabled": s.allow_commands and not s.read_only,
            "delete_enabled": s.allow_delete and not s.read_only,
            "limits": {
                "max_read_bytes": s.max_read_bytes,
                "max_write_bytes": s.max_write_bytes,
                "max_output_chars": s.max_output_chars,
                "default_command_timeout_s": s.default_command_timeout,
                "max_command_timeout_s": s.max_command_timeout,
            },
        }

    # Hide tools the server won't allow, so the AI doesn't waste calls (or tokens) on them.
    if s.read_only:
        mcp.disable(tags={"write"})
        mcp.disable(tags={"delete"})
        mcp.disable(tags={"shell"})
    else:
        if not s.allow_delete:
            mcp.disable(tags={"delete"})
        if not s.allow_commands:
            mcp.disable(tags={"shell"})

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


def build_app(settings: Settings | None = None):
    """ASGI app (use with uvicorn/gunicorn or any ASGI host)."""
    s = settings or load_settings()
    mcp = build_server(s)
    app = mcp.http_app(
        path=s.path,
        stateless_http=True,   # no sticky sessions needed for legacy clients either
        json_response=True,    # plain JSON responses: lowest latency, proxy-friendly
        # Host/Origin guard (DNS-rebinding protection). Behind a tunnel or domain,
        # list the public hostname in MCP_FS_ALLOWED_HOSTS (wildcards ok, "*" = any).
        host_origin_protection="auto",
        allowed_hosts=s.allowed_hosts or None,
        allowed_origins=s.allowed_origins or None,
    )
    tokens = None if s.auth_disabled else TokenSet(s.tokens)
    wrapped = EntryMiddleware(app, tokens, s.url_token, s.path)
    wrapped.state = app.state  # keep Starlette state reachable for hosts that read it
    return wrapped, s
