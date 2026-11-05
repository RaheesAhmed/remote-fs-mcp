"""Path sandbox: every path the AI sends is resolved inside MCP_FS_ROOT.

- Relative paths are relative to the root.
- Absolute paths are accepted only if they land inside the root.
- Symlinks are resolved first, so a link pointing outside the root is rejected.
- Optional deny globs (e.g. ".env,*.pem,.git/**") block sensitive files.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from fastmcp.exceptions import ToolError

from .config import Settings


class Sandbox:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.root = settings.root
        self._root_str = str(self.root)

    # -- resolution -----------------------------------------------------
    def resolve(self, path: str | None, *, must_exist: bool = False) -> Path:
        if path is None or path.strip() in ("", ".", "/", "~"):
            target = self.root
        else:
            if "\x00" in path:
                raise ToolError("Path contains a null byte.")
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = self.root / p
            # strict=False lets us resolve paths that don't exist yet (for writes)
            target = Path(os.path.realpath(p))

        if not self.inside(target):
            raise ToolError(
                f"Access denied: '{path}' is outside the allowed root ({self._root_str})."
            )
        rel = self.rel(target)
        if rel != "." and self._denied(rel):
            raise ToolError(f"Access denied: '{rel}' matches a blocked pattern.")
        if must_exist and not target.exists():
            raise ToolError(f"Not found: '{rel}'.")
        return target

    def inside(self, target: Path) -> bool:
        t = str(target)
        return t == self._root_str or t.startswith(self._root_str.rstrip(os.sep) + os.sep)

    def rel(self, target: Path) -> str:
        if str(target) == self._root_str:
            return "."
        return os.path.relpath(target, self.root).replace(os.sep, "/")

    def _denied(self, rel: str) -> bool:
        if not self.s.allow_hidden and any(
            part.startswith(".") for part in rel.split("/")
        ):
            return True
        for pattern in self.s.denied_globs:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                rel.rsplit("/", 1)[-1], pattern
            ):
                return True
        return False

    def visible(self, rel: str) -> bool:
        """Used when walking directories: silently skip denied entries."""
        return not self._denied(rel)

    # -- permission gates ----------------------------------------------
    def require_write(self) -> None:
        if self.s.read_only:
            raise ToolError("Server is in read-only mode (MCP_FS_READ_ONLY=true).")

    def require_delete(self) -> None:
        self.require_write()
        if not self.s.allow_delete:
            raise ToolError("Deleting is disabled on this server (MCP_FS_ALLOW_DELETE=false).")

    def require_commands(self) -> None:
        if self.s.read_only or not self.s.allow_commands:
            raise ToolError("Running commands is disabled on this server.")
