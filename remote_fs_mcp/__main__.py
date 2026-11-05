"""Entry point:  python -m remote_fs_mcp   (or the `remote-fs-mcp` command)."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="remote-fs-mcp", description="Remote filesystem + terminal MCP server")
    ap.add_argument("--root", help="Folder the AI can access (default: current folder / MCP_FS_ROOT)")
    ap.add_argument("--host", help="Bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, help="Port (default 8765)")
    ap.add_argument("--read-only", action="store_true", help="Disable all write, delete and command tools")
    ap.add_argument("--no-commands", action="store_true", help="Disable terminal tools")
    ap.add_argument("--stdio", action="store_true", help="Run over stdio instead of HTTP (local use, no auth)")
    ap.add_argument("--workers", type=int, default=1,
                    help="Uvicorn workers. Keep 1 if the AI uses start_process (process ids live in one worker).")
    a = ap.parse_args()

    if a.root:
        os.environ["MCP_FS_ROOT"] = a.root
    if a.host:
        os.environ["MCP_FS_HOST"] = a.host
    if a.port:
        os.environ["MCP_FS_PORT"] = str(a.port)
    if a.read_only:
        os.environ["MCP_FS_READ_ONLY"] = "true"
    if a.no_commands:
        os.environ["MCP_FS_ALLOW_COMMANDS"] = "false"

    if a.stdio:
        os.environ["MCP_FS_DISABLE_AUTH"] = "true"  # stdio is a local pipe; HTTP auth doesn't apply
        from .server import build_server
        build_server().run(transport="stdio", show_banner=False)
        return

    import uvicorn

    from .config import load_settings

    s = load_settings()
    if s.auth_disabled:
        print("WARNING: auth is DISABLED (MCP_FS_DISABLE_AUTH=true). Anyone who can reach this port "
              "can read/write files and run commands.", file=sys.stderr)
    elif s.token_was_generated:
        # Persist so restarts and workers share it
        os.environ["MCP_FS_TOKEN"] = s.tokens[0]
        print("\n  No MCP_FS_TOKEN set - generated one for this run:\n"
              f"\n    {s.tokens[0]}\n\n  Put it in .env (MCP_FS_TOKEN=...) to keep it across restarts.\n",
              file=sys.stderr)

    print(f"  remote-fs-mcp  ->  http://{s.host}:{s.port}{s.path}\n"
          f"  root: {s.root}\n  read_only={s.read_only} commands={s.allow_commands and not s.read_only} "
          f"delete={s.allow_delete and not s.read_only}", file=sys.stderr)
    if s.allowed_hosts:
        print(f"  allowed public hosts: {', '.join(s.allowed_hosts)}", file=sys.stderr)
    else:
        print("  NOTE: behind a tunnel or domain (ngrok, cloudflared, Caddy...)? Set\n"
              "        MCP_FS_ALLOWED_HOSTS=your.domain  (wildcards ok: *.ngrok-free.app)\n"
              "        or requests from that domain get '421 Misdirected Request'.", file=sys.stderr)
    if s.url_token and not s.auth_disabled:
        print("  URL token enabled: https://<your-domain>/<token>  works without an auth header.",
              file=sys.stderr)
    print("", file=sys.stderr)

    uvicorn.run(
        "remote_fs_mcp.asgi:app",
        host=s.host,
        port=s.port,
        workers=a.workers,
        log_level=os.environ.get("MCP_FS_LOG_LEVEL", "warning"),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
