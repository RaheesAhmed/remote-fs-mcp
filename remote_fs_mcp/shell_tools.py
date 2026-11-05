"""Terminal tools: run commands and manage long-running background processes."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .config import Settings
from .sandbox import Sandbox

IS_WINDOWS = os.name == "nt"
SECRET_ENV_KEYS = ("MCP_FS_TOKEN", "MCP_FS_TOKENS")

CMD = {"read_only_hint": False, "destructive_hint": True, "idempotent_hint": False, "open_world_hint": True}
RO = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True, "open_world_hint": False}


# ---------------------------------------------------------------- helpers
def _shell_argv(settings: Settings, command: str) -> list[str] | None:
    """Return argv for exec, or None to use the platform default shell."""
    shell = settings.shell
    if IS_WINDOWS:
        if shell and ("powershell" in shell.lower() or "pwsh" in shell.lower()):
            return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
        if shell:
            return [shell, "/c", command]
        return None  # cmd.exe via create_subprocess_shell
    shell = shell or shutil.which("bash") or "/bin/sh"
    return [shell, "-c", command]


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_KEYS}
    env.setdefault("TERM", "dumb")
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("CI", "1")  # many CLIs skip interactive prompts/spinners
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


async def _spawn(settings: Settings, command: str, cwd: str, env: dict[str, str], stdin_pipe: bool,
                 merge_stderr: bool) -> asyncio.subprocess.Process:
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": asyncio.subprocess.PIPE if stdin_pipe else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # own process group -> we can kill children too
    argv = _shell_argv(settings, command)
    if argv is None:
        return await asyncio.create_subprocess_shell(command, **kwargs)
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


async def _kill_tree(proc: asyncio.subprocess.Process, force: bool = True) -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class _Capture:
    """Keeps the first and last N chars of a stream; memory stays bounded."""

    def __init__(self, limit: int) -> None:
        self.half = max(1000, limit // 2)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.half:
                del self.tail[: len(self.tail) - self.half]

    def text(self) -> tuple[str, bool]:
        dropped = self.total - len(self.head) - len(self.tail)
        h = self.head.decode("utf-8", errors="replace")
        if dropped <= 0:
            return h + self.tail.decode("utf-8", errors="replace"), False
        t = self.tail.decode("utf-8", errors="replace")
        return f"{h}\n\n...[{dropped} bytes omitted]...\n\n{t}", True


async def _pump(stream: asyncio.StreamReader | None, sink) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink(chunk)


# ------------------------------------------------------ background procs
@dataclass
class BgProcess:
    id: str
    command: str
    cwd: str
    proc: asyncio.subprocess.Process
    started: float
    buf: bytearray = field(default_factory=bytearray)
    base_offset: int = 0  # absolute offset of buf[0]
    reader: asyncio.Task | None = None
    new_data: asyncio.Event = field(default_factory=asyncio.Event)

    MAX_BUF = 2_000_000

    def append(self, chunk: bytes) -> None:
        self.buf += chunk
        if len(self.buf) > self.MAX_BUF:
            cut = len(self.buf) - self.MAX_BUF
            del self.buf[:cut]
            self.base_offset += cut
        self.new_data.set()

    @property
    def end_offset(self) -> int:
        return self.base_offset + len(self.buf)


class ProcessManager:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.procs: dict[str, BgProcess] = {}

    def get(self, pid: str) -> BgProcess:
        bp = self.procs.get(pid)
        if not bp:
            raise ToolError(f"No process '{pid}'. Use list_processes.")
        return bp

    async def start(self, command: str, cwd: str, env: dict[str, str]) -> BgProcess:
        running = [p for p in self.procs.values() if p.proc.returncode is None]
        if len(running) >= self.s.max_processes:
            raise ToolError(f"Too many running processes ({len(running)}). Stop one first.")
        proc = await _spawn(self.s, command, cwd, env, stdin_pipe=True, merge_stderr=True)
        bp = BgProcess(id=uuid.uuid4().hex[:8], command=command, cwd=cwd, proc=proc, started=time.time())

        async def read_all() -> None:
            await _pump(proc.stdout, bp.append)
            await proc.wait()
            bp.new_data.set()

        bp.reader = asyncio.create_task(read_all())
        self.procs[bp.id] = bp
        return bp

    async def shutdown(self) -> None:
        for bp in self.procs.values():
            await _kill_tree(bp.proc)


# ------------------------------------------------------------- register
def register_shell_tools(mcp: FastMCP, settings: Settings, sb: Sandbox) -> ProcessManager:
    pm = ProcessManager(settings)
    max_out = settings.max_output_chars

    def _cwd(cwd: str | None) -> str:
        p = sb.resolve(cwd or ".", must_exist=True)
        if not p.is_dir():
            raise ToolError(f"cwd '{sb.rel(p)}' is not a directory.")
        return str(p)

    @mcp.tool(annotations=CMD, tags={"shell"})
    async def run_command(
        command: Annotated[str, "Shell command to run, e.g. 'npm install', 'pytest -q', 'git status && git diff'."],
        cwd: Annotated[str | None, "Working folder, relative to the server root. Default = root."] = None,
        timeout: Annotated[int | None, "Seconds before the command (and its children) are killed."] = None,
        env: Annotated[dict[str, str] | None, "Extra environment variables."] = None,
        stdin: Annotated[str | None, "Text to pipe into the command's stdin."] = None,
    ) -> dict[str, Any]:
        """Run a shell command and wait for it to finish. Returns exit code, stdout and stderr.
        For servers/watchers that never exit, use start_process instead."""
        sb.require_commands()
        t = min(timeout or settings.default_command_timeout, settings.max_command_timeout)
        workdir = _cwd(cwd)
        started = time.perf_counter()
        proc = await _spawn(settings, command, workdir, _child_env(env), stdin is not None, merge_stderr=False)
        out, err = _Capture(max_out), _Capture(max_out // 4)

        async def feed_stdin() -> None:
            if stdin is not None and proc.stdin:
                try:
                    proc.stdin.write(stdin.encode())
                    await proc.stdin.drain()
                finally:
                    proc.stdin.close()

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(feed_stdin(), _pump(proc.stdout, out.feed), _pump(proc.stderr, err.feed), proc.wait()),
                timeout=t,
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            await _kill_tree(proc)  # client cancelled the request
            raise

        stdout, t1 = out.text()
        stderr, t2 = err.text()
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "truncated": t1 or t2,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "cwd": sb.rel(sb.resolve(workdir)),
        }

    @mcp.tool(annotations=CMD, tags={"shell", "process"})
    async def start_process(
        command: Annotated[str, "Long-running command, e.g. 'npm run dev', 'python -m http.server 8000'."],
        cwd: Annotated[str | None, "Working folder. Default = root."] = None,
        env: Annotated[dict[str, str] | None, "Extra environment variables."] = None,
        wait_ms: Annotated[int, "Wait this long for first output before returning (max 30000)."] = 1500,
    ) -> dict[str, Any]:
        """Start a background process and return its id. Poll it with process_output."""
        sb.require_commands()
        bp = await pm.start(command, _cwd(cwd), _child_env(env))
        await _wait_output(bp, 0, min(max(wait_ms, 0), 30000))
        return {"process_id": bp.id, "pid": bp.proc.pid, **_snapshot(bp, 0)}

    async def _wait_output(bp: BgProcess, since: int, wait_ms: int) -> None:
        deadline = time.monotonic() + wait_ms / 1000
        while bp.end_offset <= since and bp.proc.returncode is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            bp.new_data.clear()
            try:
                await asyncio.wait_for(bp.new_data.wait(), remaining)
            except asyncio.TimeoutError:
                return

    def _snapshot(bp: BgProcess, since: int) -> dict[str, Any]:
        start = max(since, bp.base_offset)
        chunk = bytes(bp.buf[start - bp.base_offset:])
        text = chunk.decode("utf-8", errors="replace")
        truncated = False
        if len(text) > max_out:
            text = "...[earlier output omitted]...\n" + text[-max_out:]
            truncated = True
        return {
            "running": bp.proc.returncode is None,
            "exit_code": bp.proc.returncode,
            "output": text,
            "next_offset": bp.end_offset,
            "lost_bytes": max(0, bp.base_offset - since),
            "truncated": truncated,
        }

    @mcp.tool(annotations=RO, tags={"shell", "process"})
    async def process_output(
        process_id: Annotated[str, "Id returned by start_process."],
        since: Annotated[int, "Byte offset to read from: pass the previous next_offset to get only new output. 0 = everything kept."] = 0,
        wait_ms: Annotated[int, "If there is no new output yet, wait up to this long (max 30000)."] = 0,
    ) -> dict[str, Any]:
        """Read output from a background process (incrementally with `since`)."""
        bp = pm.get(process_id)
        await _wait_output(bp, since, min(max(wait_ms, 0), 30000))
        return {"process_id": bp.id, **_snapshot(bp, since)}

    @mcp.tool(annotations=CMD, tags={"shell", "process"})
    async def send_process_input(
        process_id: Annotated[str, "Id returned by start_process."],
        text: Annotated[str, "Text to send to stdin."],
        newline: Annotated[bool, "Append a newline (press Enter)."] = True,
    ) -> dict[str, Any]:
        """Type into a running process's stdin (answer prompts, REPLs)."""
        sb.require_commands()
        bp = pm.get(process_id)
        if bp.proc.returncode is not None or bp.proc.stdin is None:
            raise ToolError("Process is not running.")
        bp.proc.stdin.write((text + ("\n" if newline else "")).encode())
        await bp.proc.stdin.drain()
        return {"process_id": bp.id, "sent_chars": len(text) + (1 if newline else 0)}

    @mcp.tool(annotations=CMD, tags={"shell", "process"})
    async def stop_process(
        process_id: Annotated[str, "Id returned by start_process."],
        force: Annotated[bool, "SIGKILL immediately instead of SIGTERM first."] = False,
        forget: Annotated[bool, "Also remove it from list_processes."] = True,
    ) -> dict[str, Any]:
        """Stop a background process and all its children."""
        bp = pm.get(process_id)
        if bp.proc.returncode is None:
            await _kill_tree(bp.proc, force=force or IS_WINDOWS)
            try:
                await asyncio.wait_for(bp.proc.wait(), 5)
            except asyncio.TimeoutError:
                await _kill_tree(bp.proc, force=True)
                await bp.proc.wait()
        snap = _snapshot(bp, max(bp.base_offset, bp.end_offset - 4000))
        if forget:
            pm.procs.pop(bp.id, None)
        return {"process_id": bp.id, "exit_code": bp.proc.returncode, "last_output": snap["output"]}

    @mcp.tool(annotations=RO, tags={"shell", "process"})
    def list_processes() -> list[dict[str, Any]]:
        """List background processes started by this server."""
        now = time.time()
        return [
            {"process_id": bp.id, "pid": bp.proc.pid, "command": bp.command, "cwd": sb.rel(sb.resolve(bp.cwd)),
             "running": bp.proc.returncode is None, "exit_code": bp.proc.returncode,
             "uptime_s": round(now - bp.started), "output_bytes": bp.end_offset}
            for bp in pm.procs.values()
        ]

    return pm


def platform_info(settings: Settings) -> dict[str, Any]:
    argv = _shell_argv(settings, "<cmd>")
    return {
        "os": sys.platform,
        "shell": argv[0] if argv else ("cmd.exe" if IS_WINDOWS else "/bin/sh"),
        "python": sys.version.split()[0],
        "ripgrep": bool(shutil.which("rg")),
        "git": bool(shutil.which("git")),
        "node": bool(shutil.which("node")),
    }
