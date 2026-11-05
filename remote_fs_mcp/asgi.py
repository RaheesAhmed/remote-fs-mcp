"""ASGI entry for uvicorn/gunicorn:  uvicorn remote_fs_mcp.asgi:app"""

from .server import build_app

app, settings = build_app()
