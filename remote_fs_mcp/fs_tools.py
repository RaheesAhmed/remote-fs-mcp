"""File and folder tools.

All tools are plain sync functions; FastMCP runs them in a worker thread
(run_in_thread=True) so blocking disk IO never stalls the event loop.
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .config import Settings
from .sandbox import Sandbox

# Folders that are almost never useful to an AI and are huge to walk.
HEAVY_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".next", "dist", "build", ".cache", ".tox", "target",
    ".idea", ".gradle", ".terraform",
}

RO = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True, "open_world_hint": False}
WRITE = {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": False, "open_world_hint": False}
WRITE_IDEMPOTENT = {**WRITE, "idempotent_hint": True}
DESTRUCTIVE = {"read_only_hint": False, "destructive_hint": True, "idempotent_hint": False, "open_world_hint": False}

_RG = shutil.which("rg")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # A multi-byte char cut at the end of the sample is fine.
        return e.start < len(sample) - 4


def _atomic_write(target: Path, data: bytes) -> None:
    """Write to a temp file in the same folder, then rename (never half-written)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _iter_files(base: Path):
    """Walk files under base, pruning heavy folders (fallback when ripgrep is absent)."""
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in HEAVY_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


class TextEdit(BaseModel):
    old_text: str = Field(description="Exact text to find (include enough surrounding lines to be unique).")
    new_text: str = Field(description="Replacement text. Empty string deletes old_text.")
    replace_all: bool = Field(default=False, description="Replace every occurrence instead of requiring exactly one.")


def register_fs_tools(mcp: FastMCP, settings: Settings, sb: Sandbox) -> None:
    max_out = settings.max_output_chars

    # ------------------------------------------------------------------ read
    @mcp.tool(annotations=RO, tags={"fs", "read"})
    def read_file(
        path: Annotated[str, "File path, relative to the server root (or absolute inside it)."],
        offset: Annotated[int, "1-based line to start from."] = 1,
        limit: Annotated[int, "Max lines to return."] = 2000,
        line_numbers: Annotated[bool, "Prefix each line with its number (handy before edit_file)."] = False,
        as_base64: Annotated[bool, "Return raw bytes as base64 (for binary files: images, zips, pdf...)."] = False,
    ) -> dict[str, Any]:
        """Read a file. Text is returned by line range; binary files can be returned as base64."""
        p = sb.resolve(path, must_exist=True)
        if p.is_dir():
            raise ToolError(f"'{sb.rel(p)}' is a directory. Use list_directory.")
        size = p.stat().st_size

        if as_base64:
            if size > settings.max_read_bytes:
                raise ToolError(f"File is {size} bytes; base64 limit is {settings.max_read_bytes}.")
            return {"path": sb.rel(p), "size": size, "encoding": "base64",
                    "content": base64.b64encode(p.read_bytes()).decode()}

        with p.open("rb") as f:
            head = f.read(8192)
        if _is_binary(head):
            return {"path": sb.rel(p), "size": size, "binary": True,
                    "content": "", "hint": "Binary file. Call again with as_base64=true to get bytes."}

        offset = max(1, offset)
        limit = max(1, limit)
        with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
            selected = list(itertools.islice(f, offset - 1, offset - 1 + limit))
            more = f.readline() != ""
            total = None
            if size <= settings.max_read_bytes:
                # cheap: we're already positioned; count the rest
                total = offset - 1 + len(selected) + (1 if more else 0) + sum(1 for _ in f)

        if line_numbers:
            width = len(str(offset + len(selected)))
            text = "".join(f"{str(offset + i).rjust(width)}\t{line}" for i, line in enumerate(selected))
        else:
            text = "".join(selected)

        truncated_chars = False
        if len(text) > max_out:
            text = text[:max_out]
            truncated_chars = True
        end = offset + len(selected) - 1
        return {
            "path": sb.rel(p),
            "size": size,
            "start_line": offset,
            "end_line": end,
            "total_lines": total,
            "has_more": more or truncated_chars,
            "next_offset": end + 1 if more else None,
            "content": text,
        }

    @mcp.tool(annotations=RO, tags={"fs", "read"})
    def read_many_files(
        paths: Annotated[list[str], "Up to 50 file paths to read in one call."],
        max_chars_each: Annotated[int, "Cut each file after this many characters."] = 20000,
    ) -> list[dict[str, Any]]:
        """Read several text files in one round-trip. Errors are reported per file, not raised."""
        if len(paths) > 50:
            raise ToolError("Max 50 files per call.")
        budget = max_out
        results: list[dict[str, Any]] = []
        for raw in paths:
            try:
                p = sb.resolve(raw, must_exist=True)
                if p.is_dir():
                    raise ToolError("is a directory")
                with p.open("rb") as f:
                    data = f.read(min(max_chars_each * 4, settings.max_read_bytes))
                if _is_binary(data[:8192]):
                    results.append({"path": sb.rel(p), "binary": True})
                    continue
                text = data.decode("utf-8", errors="replace")
                cap = max(0, min(max_chars_each, budget))
                truncated = len(text) > cap or p.stat().st_size > len(data)
                text = text[:cap]
                budget -= len(text)
                results.append({"path": sb.rel(p), "content": text, "truncated": truncated})
            except ToolError as e:
                results.append({"path": raw, "error": str(e)})
            except OSError as e:
                results.append({"path": raw, "error": e.strerror or str(e)})
        return results

    @mcp.tool(annotations=RO, tags={"fs", "read"})
    def get_file_info(path: Annotated[str, "File or folder path."]) -> dict[str, Any]:
        """Size, type, permissions and timestamps for a path."""
        p = sb.resolve(path, must_exist=True)
        st = p.stat()
        info: dict[str, Any] = {
            "path": sb.rel(p),
            "type": "directory" if p.is_dir() else "file",
            "size": st.st_size,
            "permissions": stat.filemode(st.st_mode),
            "modified": _iso(st.st_mtime),
            "created": _iso(getattr(st, "st_birthtime", st.st_ctime)),
            "is_symlink": Path(os.path.abspath(p)).is_symlink(),
        }
        if p.is_dir():
            try:
                info["entries"] = sum(1 for _ in os.scandir(p))
            except OSError:
                info["entries"] = None
        return info

    # ------------------------------------------------------------- listing
    @mcp.tool(annotations=RO, tags={"fs", "read"})
    def list_directory(
        path: Annotated[str, "Folder to list. Empty or '.' = server root."] = ".",
        depth: Annotated[int, "1 = only this folder; 2+ = recurse that many levels (max 10)."] = 1,
        pattern: Annotated[str | None, "Optional name filter glob, e.g. '*.py'. Folders are always shown when recursing."] = None,
        show_hidden: Annotated[bool, "Include dotfiles."] = True,
        skip_heavy: Annotated[bool, "When recursing, don't descend into node_modules, .git, venvs, build dirs."] = True,
        max_entries: Annotated[int, "Stop after this many entries."] = 1000,
    ) -> dict[str, Any]:
        """List a folder (optionally recursive). Returns compact lines: 'd name/' for folders, 'f name  size' for files."""
        base = sb.resolve(path, must_exist=True)
        if not base.is_dir():
            raise ToolError(f"'{sb.rel(base)}' is not a directory.")
        depth = max(1, min(depth, 10))
        lines: list[str] = []
        n_files = n_dirs = 0
        truncated = False

        def walk(d: Path, level: int, prefix: str) -> None:
            nonlocal n_files, n_dirs, truncated
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError as e:
                lines.append(f"{prefix}! {e.strerror}")
                return
            for e in entries:
                if len(lines) >= max_entries:
                    truncated = True
                    return
                if not show_hidden and e.name.startswith("."):
                    continue
                rel = f"{prefix}{e.name}"
                if not sb.visible(sb.rel(Path(d) / e.name)):
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                if is_dir:
                    n_dirs += 1
                    heavy = skip_heavy and e.name in HEAVY_DIRS
                    lines.append(f"d {rel}/" + ("  (skipped)" if heavy and level < depth else ""))
                    if level < depth and not heavy:
                        walk(Path(e.path), level + 1, rel + "/")
                else:
                    if pattern and not fnmatch.fnmatch(e.name, pattern):
                        continue
                    n_files += 1
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = -1
                    link = " -> link" if e.is_symlink() else ""
                    lines.append(f"f {rel}  {size}{link}")

        walk(base, 1, "")
        return {
            "path": sb.rel(base),
            "files": n_files,
            "directories": n_dirs,
            "truncated": truncated,
            "entries": "\n".join(lines),
        }

    @mcp.tool(annotations=RO, tags={"fs", "search"})
    def find_files(
        pattern: Annotated[str, "Glob on file name or relative path, e.g. '*.tsx', 'src/**/test_*.py', 'README*'."],
        path: Annotated[str, "Folder to search in."] = ".",
        include_ignored: Annotated[bool, "Also search files ignored by .gitignore and heavy dirs."] = False,
        max_results: Annotated[int, "Max paths to return."] = 500,
    ) -> dict[str, Any]:
        """Find files by name/glob, fast (uses ripgrep when installed)."""
        base = sb.resolve(path, must_exist=True)
        matches: list[str] = []
        glob_pat = pattern if ("/" in pattern or "**" in pattern) else f"**/{pattern}"

        if _RG:
            cmd = [_RG, "--files", "--hidden", "--glob", glob_pat, "--glob", "!.git/"]
            if include_ignored:
                cmd.insert(2, "--no-ignore")
            proc = subprocess.run(cmd, cwd=base, capture_output=True, text=True, timeout=60)
            for line in proc.stdout.splitlines():
                rel = sb.rel(base / line)
                if sb.visible(rel):
                    matches.append(rel)
                    if len(matches) >= max_results:
                        break
        else:
            for dirpath, dirnames, filenames in os.walk(base):
                if not include_ignored:
                    dirnames[:] = [d for d in dirnames if d not in HEAVY_DIRS]
                for name in filenames:
                    full = Path(dirpath) / name
                    rel_to_base = os.path.relpath(full, base).replace(os.sep, "/")
                    if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_to_base, pattern):
                        rel = sb.rel(full)
                        if sb.visible(rel):
                            matches.append(rel)
                if len(matches) >= max_results:
                    break
        matches.sort()
        return {"count": len(matches[:max_results]), "truncated": len(matches) >= max_results,
                "paths": matches[:max_results]}

    @mcp.tool(annotations=RO, tags={"fs", "search"})
    def search_text(
        query: Annotated[str, "Text or regex to search for inside files."],
        path: Annotated[str, "File or folder to search."] = ".",
        regex: Annotated[bool, "Treat query as a regular expression."] = False,
        case_sensitive: Annotated[bool, "Match case."] = False,
        file_glob: Annotated[str | None, "Only search files matching this glob, e.g. '*.py'."] = None,
        context_lines: Annotated[int, "Lines of context before/after each match (0-5)."] = 0,
        max_results: Annotated[int, "Max matching lines to return."] = 200,
    ) -> dict[str, Any]:
        """Grep across files (ripgrep speed when installed). Returns 'path:line: text' lines."""
        base = sb.resolve(path, must_exist=True)
        context_lines = max(0, min(context_lines, 5))
        out: list[str] = []
        count = 0

        if _RG:
            cmd = [_RG, "--json", "--hidden", "--glob", "!.git/", "--max-columns", "400",
                   "--max-columns-preview", "-C", str(context_lines)]
            if not regex:
                cmd.append("--fixed-strings")
            if not case_sensitive:
                cmd.append("--ignore-case")
            if file_glob:
                cmd += ["--glob", file_glob]
            cmd += ["--", query, str(base)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                assert proc.stdout
                for raw in proc.stdout:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind not in ("match", "context"):
                        continue
                    d = msg["data"]
                    fpath = d["path"].get("text", "")
                    rel = sb.rel(Path(fpath))
                    if not sb.visible(rel):
                        continue
                    text = d["lines"].get("text", "").rstrip("\n")
                    sep = ":" if kind == "match" else "-"
                    out.append(f"{rel}{sep}{d['line_number']}{sep} {text}")
                    if kind == "match":
                        count += 1
                        if count >= max_results:
                            break
            finally:
                proc.kill()
                proc.wait()
        else:
            flags = 0 if case_sensitive else re.IGNORECASE
            rx = re.compile(query if regex else re.escape(query), flags)
            for fp in _iter_files(base):
                if file_glob and not fnmatch.fnmatch(fp.name, file_glob):
                    continue
                rel = sb.rel(fp)
                if not sb.visible(rel):
                    continue
                try:
                    with fp.open("rb") as fh:
                        if _is_binary(fh.read(4096)):
                            continue
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for i, line in enumerate(lines, 1):
                    if rx.search(line):
                        lo, hi = max(1, i - context_lines), min(len(lines), i + context_lines)
                        for j in range(lo, hi + 1):
                            sep = ":" if j == i else "-"
                            out.append(f"{rel}{sep}{j}{sep} {lines[j - 1][:400]}")
                        count += 1
                        if count >= max_results:
                            break
                if count >= max_results:
                    break

        text = "\n".join(out)
        if len(text) > max_out:
            text = text[:max_out] + "\n...[truncated]"
        return {"matches": count, "truncated": count >= max_results, "results": text}

    # --------------------------------------------------------------- write
    @mcp.tool(annotations=WRITE, tags={"fs", "write"})
    def write_file(
        path: Annotated[str, "File to write. Parent folders are created automatically."],
        content: Annotated[str, "Full file content (or base64 if is_base64=true)."],
        mode: Annotated[Literal["overwrite", "append", "create_only"], "overwrite (default), append to end, or create_only (fail if it exists)."] = "overwrite",
        is_base64: Annotated[bool, "content is base64 bytes (for binary files)."] = False,
    ) -> dict[str, Any]:
        """Create or replace a file atomically (never leaves a half-written file)."""
        sb.require_write()
        p = sb.resolve(path)
        if p.is_dir():
            raise ToolError(f"'{sb.rel(p)}' is a directory.")
        data = base64.b64decode(content) if is_base64 else content.encode("utf-8")
        if len(data) > settings.max_write_bytes:
            raise ToolError(f"Content is {len(data)} bytes; limit is {settings.max_write_bytes}.")
        existed = p.exists()
        if mode == "create_only" and existed:
            raise ToolError(f"'{sb.rel(p)}' already exists (mode=create_only).")
        if mode == "append":
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("ab") as f:
                f.write(data)
        else:
            _atomic_write(p, data)
        return {"path": sb.rel(p), "bytes_written": len(data),
                "action": "appended" if mode == "append" else ("overwritten" if existed else "created"),
                "size": p.stat().st_size}

    @mcp.tool(annotations=WRITE, tags={"fs", "write"})
    def edit_file(
        path: Annotated[str, "File to edit."],
        edits: Annotated[list[TextEdit], "One or more find/replace edits, applied in order."],
        dry_run: Annotated[bool, "Only show the diff, don't save."] = False,
    ) -> dict[str, Any]:
        """Surgical edit: replace exact text blocks and return a unified diff. Much cheaper than rewriting a whole file."""
        sb.require_write()
        p = sb.resolve(path, must_exist=True)
        raw = p.read_bytes()
        if _is_binary(raw[:8192]):
            raise ToolError("Cannot text-edit a binary file.")
        original = raw.decode("utf-8", errors="surrogateescape")
        crlf = "\r\n" in original
        text = original.replace("\r\n", "\n") if crlf else original
        total = 0
        for n, e in enumerate(edits, 1):
            old = e.old_text.replace("\r\n", "\n")
            new = e.new_text.replace("\r\n", "\n")
            if not old:
                raise ToolError(f"Edit {n}: old_text is empty.")
            hits = text.count(old)
            if hits == 0:
                raise ToolError(f"Edit {n}: old_text not found. Read the file again and copy the text exactly.")
            if hits > 1 and not e.replace_all:
                raise ToolError(f"Edit {n}: old_text found {hits} times. Add more surrounding lines or set replace_all=true.")
            text = text.replace(old, new) if e.replace_all else text.replace(old, new, 1)
            total += hits if e.replace_all else 1
        final = text.replace("\n", "\r\n") if crlf else text
        diff = "".join(difflib.unified_diff(
            original.replace("\r\n", "\n").splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=f"a/{sb.rel(p)}", tofile=f"b/{sb.rel(p)}", n=2))
        if len(diff) > max_out:
            diff = diff[:max_out] + "\n...[diff truncated]"
        if not dry_run:
            _atomic_write(p, final.encode("utf-8", errors="surrogateescape"))
        return {"path": sb.rel(p), "replacements": total, "saved": not dry_run, "diff": diff}

    @mcp.tool(annotations=WRITE_IDEMPOTENT, tags={"fs", "write"})
    def create_directory(path: Annotated[str, "Folder to create (parents included). No error if it exists."]) -> dict[str, Any]:
        """Create a folder and any missing parents."""
        sb.require_write()
        p = sb.resolve(path)
        existed = p.is_dir()
        p.mkdir(parents=True, exist_ok=True)
        return {"path": sb.rel(p), "created": not existed}

    @mcp.tool(annotations=WRITE, tags={"fs", "write"})
    def move_path(
        source: Annotated[str, "File or folder to move/rename."],
        destination: Annotated[str, "New path. If it is an existing folder, source goes inside it."],
        overwrite: Annotated[bool, "Replace an existing destination file."] = False,
    ) -> dict[str, Any]:
        """Move or rename a file or folder."""
        sb.require_write()
        src = sb.resolve(source, must_exist=True)
        if src == sb.root:
            raise ToolError("Cannot move the server root.")
        dst = sb.resolve(destination)
        if dst.is_dir():
            dst = sb.resolve(str(dst / src.name))
        if dst.exists():
            if not overwrite:
                raise ToolError(f"'{sb.rel(dst)}' exists. Set overwrite=true to replace it.")
            if dst.is_dir():
                raise ToolError("Refusing to overwrite a folder; delete it first.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"from": sb.rel(src), "to": sb.rel(dst)}

    @mcp.tool(annotations=WRITE, tags={"fs", "write"})
    def copy_path(
        source: Annotated[str, "File or folder to copy."],
        destination: Annotated[str, "Target path. If it is an existing folder, source goes inside it."],
        overwrite: Annotated[bool, "Replace existing files at the destination."] = False,
    ) -> dict[str, Any]:
        """Copy a file or a whole folder."""
        sb.require_write()
        src = sb.resolve(source, must_exist=True)
        dst = sb.resolve(destination)
        if dst.is_dir() and src.is_file():
            dst = sb.resolve(str(dst / src.name))
        if src.is_dir():
            if sb.inside(dst) and str(dst).startswith(str(src) + os.sep):
                raise ToolError("Cannot copy a folder into itself.")
            if dst.exists() and not overwrite:
                raise ToolError(f"'{sb.rel(dst)}' exists. Set overwrite=true to merge into it.")
            shutil.copytree(src, dst, dirs_exist_ok=overwrite, symlinks=True)
        else:
            if dst.exists() and not overwrite:
                raise ToolError(f"'{sb.rel(dst)}' exists. Set overwrite=true to replace it.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return {"from": sb.rel(src), "to": sb.rel(dst)}

    @mcp.tool(annotations=DESTRUCTIVE, tags={"fs", "delete"})
    def delete_path(
        path: Annotated[str, "File or folder to delete."],
        recursive: Annotated[bool, "Required to delete a non-empty folder."] = False,
    ) -> dict[str, Any]:
        """Permanently delete a file or folder."""
        sb.require_delete()
        raw = Path(path).expanduser()
        link = raw if raw.is_absolute() else sb.root / raw
        if link.is_symlink():
            # Remove the link itself (never its target), as long as the link lives inside root.
            if not sb.inside(Path(os.path.realpath(link.parent))):
                raise ToolError(f"Access denied: '{path}' is outside the allowed root.")
            link.unlink()
            return {"deleted": path, "type": "symlink"}
        p = sb.resolve(path)
        if p == sb.root:
            raise ToolError("Refusing to delete the server root.")
        if not p.exists():
            raise ToolError(f"Not found: '{sb.rel(p)}'.")
        if p.is_dir():
            if recursive:
                shutil.rmtree(p)
            else:
                try:
                    p.rmdir()
                except OSError:
                    raise ToolError("Folder is not empty. Set recursive=true to delete it and everything inside.")
            return {"deleted": sb.rel(p), "type": "directory"}
        p.unlink()
        return {"deleted": sb.rel(p), "type": "file"}
