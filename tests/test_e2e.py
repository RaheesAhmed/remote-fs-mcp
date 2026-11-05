"""End-to-end test: boots the real HTTP server and calls every tool through an MCP client.

Run:  python tests/test_e2e.py      (or: pytest -q tests/)
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import json
import urllib.error
import urllib.request
from fastmcp import Client
from fastmcp.exceptions import ToolError

TOKEN = "e2e-test-token-" + os.urandom(6).hex()
PROJECT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(root: Path, port: int, **extra_env: str) -> subprocess.Popen:
    env = {**os.environ, "MCP_FS_ROOT": str(root), "MCP_FS_TOKEN": TOKEN,
           "MCP_FS_HOST": "127.0.0.1", "MCP_FS_PORT": str(port), **extra_env}
    proc = subprocess.Popen([sys.executable, "-m", "remote_fs_mcp"], cwd=PROJECT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start: " + proc.stderr.read().decode())


async def expect_error(coro, contains: str) -> None:
    try:
        await coro
    except ToolError as e:
        assert contains.lower() in str(e).lower(), f"expected '{contains}' in '{e}'"
        return
    raise AssertionError(f"expected ToolError containing '{contains}'")


def raw_post(url: str, headers: dict[str, str] | None = None) -> int:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    # urllib honours an explicit Host header, which lets us simulate a tunnel domain
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def data(res):
    return res.structured_content.get("result", res.structured_content) if res.structured_content else res.data


async def run_suite(url: str, root: Path) -> list[str]:
    passed: list[str] = []

    def ok(name: str) -> None:
        passed.append(name)
        print(f"  PASS  {name}")

    # --- auth ---------------------------------------------------------
    assert raw_post(url) == 401
    assert raw_post(url, {"Authorization": "Bearer wrong"}) == 401
    ok("auth: missing and wrong token rejected (401)")

    async with Client(url, auth=TOKEN) as c:
        tools = {t.name for t in await c.list_tools()}
        assert len(tools) == 19, tools
        ok(f"list_tools: {len(tools)} tools")

        info = data(await c.call_tool("server_info", {}))
        assert info["root"] == str(root)
        ok("server_info")

        # --- write / read ------------------------------------------
        w = data(await c.call_tool("write_file", {"path": "src/app.py", "content": "def hello():\n    return 'hi'\n\nprint(hello())\n"}))
        assert w["action"] == "created"
        ok("write_file (creates parent folders)")

        await expect_error(c.call_tool("write_file", {"path": "src/app.py", "content": "x", "mode": "create_only"}), "already exists")
        ok("write_file create_only guard")

        await c.call_tool("write_file", {"path": "notes.txt", "content": "line1\n"})
        await c.call_tool("write_file", {"path": "notes.txt", "content": "line2\n", "mode": "append"})
        assert (root / "notes.txt").read_text() == "line1\nline2\n"
        ok("write_file append")

        r = data(await c.call_tool("read_file", {"path": "src/app.py", "line_numbers": True}))
        assert r["total_lines"] == 4 and "1\tdef hello" in r["content"]
        ok("read_file + line numbers")

        big = "".join(f"row {i}\n" for i in range(1, 5001))
        await c.call_tool("write_file", {"path": "big.txt", "content": big})
        r = data(await c.call_tool("read_file", {"path": "big.txt", "offset": 4000, "limit": 3}))
        assert r["content"] == "row 4000\nrow 4001\nrow 4002\n" and r["total_lines"] == 5000 and r["next_offset"] == 4003
        ok("read_file ranged (offset/limit) on 5000-line file")

        png = bytes([0x89, 0x50, 0x4E, 0x47, 0, 1, 2, 3, 255])
        await c.call_tool("write_file", {"path": "img/x.png", "content": base64.b64encode(png).decode(), "is_base64": True})
        r = data(await c.call_tool("read_file", {"path": "img/x.png"}))
        assert r["binary"] is True
        r = data(await c.call_tool("read_file", {"path": "img/x.png", "as_base64": True}))
        assert base64.b64decode(r["content"]) == png
        ok("binary write/read via base64")

        rm = data(await c.call_tool("read_many_files", {"paths": ["src/app.py", "notes.txt", "missing.txt"]}))
        assert rm[0]["content"].startswith("def hello") and "error" in rm[2]
        ok("read_many_files (per-file errors)")

        # --- edit --------------------------------------------------
        e = data(await c.call_tool("edit_file", {"path": "src/app.py", "edits": [{"old_text": "return 'hi'", "new_text": "return 'hello world'"}]}))
        assert e["replacements"] == 1 and "+    return 'hello world'" in e["diff"]
        assert "hello world" in (root / "src/app.py").read_text()
        ok("edit_file + diff")

        await expect_error(c.call_tool("edit_file", {"path": "src/app.py", "edits": [{"old_text": "nope", "new_text": "x"}]}), "not found")
        await c.call_tool("write_file", {"path": "dup.txt", "content": "a\na\n"})
        await expect_error(c.call_tool("edit_file", {"path": "dup.txt", "edits": [{"old_text": "a", "new_text": "b"}]}), "2 times")
        e = data(await c.call_tool("edit_file", {"path": "dup.txt", "edits": [{"old_text": "a", "new_text": "b", "replace_all": True}], "dry_run": True}))
        assert e["replacements"] == 2 and (root / "dup.txt").read_text() == "a\na\n"
        ok("edit_file guards (not found / ambiguous / dry_run)")

        # --- folders / list / info ---------------------------------
        d = data(await c.call_tool("create_directory", {"path": "a/b/c"}))
        assert d["created"] and (root / "a/b/c").is_dir()
        ok("create_directory (nested)")

        (root / "node_modules/pkg").mkdir(parents=True)
        (root / "node_modules/pkg/index.js").write_text("x")
        ls = data(await c.call_tool("list_directory", {"path": ".", "depth": 3}))
        assert "f src/app.py" in ls["entries"] and "d node_modules/  (skipped)" in ls["entries"]
        assert "index.js" not in ls["entries"]
        ok("list_directory recursive (skips heavy dirs)")

        fi = data(await c.call_tool("get_file_info", {"path": "src/app.py"}))
        assert fi["type"] == "file" and fi["size"] > 0
        ok("get_file_info")

        # --- search ------------------------------------------------
        f = data(await c.call_tool("find_files", {"pattern": "*.py"}))
        assert "src/app.py" in f["paths"]
        ok("find_files glob")

        s = data(await c.call_tool("search_text", {"query": "HELLO", "file_glob": "*.py"}))
        assert s["matches"] >= 1 and "src/app.py:" in s["results"]
        s = data(await c.call_tool("search_text", {"query": r"row 49\d\d", "regex": True, "path": "big.txt", "max_results": 5}))
        assert s["matches"] == 5 and s["truncated"]
        ok("search_text (case-insensitive + regex + limits)")

        # --- move / copy / delete ----------------------------------
        await c.call_tool("copy_path", {"source": "src", "destination": "src_copy"})
        assert (root / "src_copy/app.py").exists()
        await c.call_tool("move_path", {"source": "notes.txt", "destination": "a"})
        assert (root / "a/notes.txt").exists() and not (root / "notes.txt").exists()
        await c.call_tool("move_path", {"source": "a/notes.txt", "destination": "a/renamed.txt"})
        assert (root / "a/renamed.txt").exists()
        ok("copy_path folder, move_path into folder + rename")

        await expect_error(c.call_tool("delete_path", {"path": "a"}), "not empty")
        await c.call_tool("delete_path", {"path": "a", "recursive": True})
        assert not (root / "a").exists()
        await expect_error(c.call_tool("delete_path", {"path": "."}), "root")
        ok("delete_path (empty guard, recursive, root protected)")

        # --- sandbox escapes ---------------------------------------
        await expect_error(c.call_tool("read_file", {"path": "../../etc/passwd"}), "outside")
        await expect_error(c.call_tool("read_file", {"path": "/etc/passwd"}), "outside")
        await expect_error(c.call_tool("write_file", {"path": "../escape.txt", "content": "x"}), "outside")
        os.symlink("/etc", root / "evil_link")
        await expect_error(c.call_tool("read_file", {"path": "evil_link/passwd"}), "outside")
        await expect_error(c.call_tool("list_directory", {"path": "evil_link"}), "outside")
        await c.call_tool("delete_path", {"path": "evil_link"})  # removes the link, not /etc
        assert not (root / "evil_link").is_symlink() and Path("/etc/passwd").exists()
        ok("sandbox: ../, absolute and symlink escapes all blocked")

        # --- commands ----------------------------------------------
        r = data(await c.call_tool("run_command", {"command": "python3 src/app.py && echo err >&2; exit 3"}))
        assert r["stdout"].strip() == "hello world" and r["stderr"].strip() == "err" and r["exit_code"] == 3
        ok("run_command (stdout, stderr, exit code)")

        r = data(await c.call_tool("run_command", {"command": "pwd", "cwd": "src"}))
        assert r["stdout"].strip() == str(root / "src")
        r = data(await c.call_tool("run_command", {"command": "cat", "stdin": "piped!"}))
        assert r["stdout"] == "piped!"
        r = data(await c.call_tool("run_command", {"command": "echo ${MCP_FS_TOKEN:-hidden}"}))
        assert r["stdout"].strip() == "hidden"
        ok("run_command cwd, stdin, auth token hidden from child env")

        t0 = time.perf_counter()
        r = data(await c.call_tool("run_command", {"command": "sleep 30 & sleep 30; echo never", "timeout": 1}))
        assert r["timed_out"] and time.perf_counter() - t0 < 8
        ok("run_command timeout kills whole process tree")

        r = data(await c.call_tool("run_command", {"command": "python3 -c \"print('x'*500000)\""}))
        assert r["truncated"] and "bytes omitted" in r["stdout"]
        ok("run_command huge output capped (head + tail kept)")

        # --- background processes ----------------------------------
        p = data(await c.call_tool("start_process", {"command": "python3 -u -c \"import time\nfor i in range(3):\n  print('tick', i); time.sleep(0.3)\nprint(input('name? '))\"", "wait_ms": 2000}))
        pid = p["process_id"]
        assert "tick 0" in p["output"]
        o = data(await c.call_tool("process_output", {"process_id": pid, "since": p["next_offset"], "wait_ms": 3000}))
        assert o["output"] and o["next_offset"] > p["next_offset"]  # only NEW output returned
        await c.call_tool("send_process_input", {"process_id": pid, "text": "rahees"})
        seen, offset = "", 0
        for _ in range(20):  # poll incrementally until exit, like an agent would
            o2 = data(await c.call_tool("process_output", {"process_id": pid, "since": offset, "wait_ms": 1000}))
            seen += o2["output"]
            offset = o2["next_offset"]
            if not o2["running"]:
                break
        assert "rahees" in seen and o2["exit_code"] == 0, o2
        ok("start_process + incremental process_output + send_process_input")

        port2 = free_port()
        srv = data(await c.call_tool("start_process", {"command": f"python3 -m http.server {port2} --bind 127.0.0.1", "wait_ms": 1500}))
        lst = data(await c.call_tool("list_processes", {}))
        assert any(x["process_id"] == srv["process_id"] and x["running"] for x in lst)
        st = data(await c.call_tool("stop_process", {"process_id": srv["process_id"]}))
        assert st["exit_code"] is not None
        ok("long-running server: list_processes + stop_process")

    return passed


async def run_readonly(url: str) -> None:
    async with Client(url, auth=TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
        assert "write_file" not in names and "run_command" not in names and "read_file" in names, names
    print(f"  PASS  read-only mode exposes only {len(names)} safe tools")


def run_tunnel(base: str) -> None:
    tunnel = {"Host": "abc123.ngrok-free.app"}
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert raw_post(base + "/mcp", {"Host": "abc123.ngrok-free.app", **auth}) == 200
    assert raw_post(base + "/", {"Host": "abc123.ngrok-free.app", **auth}) == 200       # root alias
    assert raw_post(base + f"/{TOKEN}", tunnel) == 200                                   # URL token
    assert raw_post(base + f"/{TOKEN}/mcp", tunnel) == 200
    assert raw_post(base + "/mcp", tunnel) == 401                                        # still needs a token
    assert raw_post(base + "/not-the-right-token-xx", tunnel) == 404
    assert raw_post(base + "/mcp", {"Host": "evil.com", **auth}) == 421                  # unknown host
    assert raw_post(base + f"/{TOKEN}", {**tunnel, "Origin": "https://claude.ai"}) == 200
    assert raw_post(base + f"/{TOKEN}", {**tunnel, "Origin": "https://evil.com"}) == 403
    print("  PASS  tunnel host allowed, root alias, URL token, Host/Origin guard")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve() / "workspace"
        root.mkdir()
        port = free_port()
        server = start_server(root, port)
        try:
            print("Full-access server:")
            t0 = time.perf_counter()
            passed = asyncio.run(run_suite(f"http://127.0.0.1:{port}/mcp", root))
            print(f"  {len(passed)} checks in {time.perf_counter() - t0:.1f}s")
        finally:
            server.terminate()
            server.wait(10)

        port = free_port()
        server = start_server(root, port, MCP_FS_READ_ONLY="true")
        try:
            print("Read-only server:")
            asyncio.run(run_readonly(f"http://127.0.0.1:{port}/mcp"))
        finally:
            server.terminate()
            server.wait(10)
        port = free_port()
        server = start_server(root, port, MCP_FS_ALLOWED_HOSTS="*.ngrok-free.app",
                              MCP_FS_ALLOWED_ORIGINS="https://claude.ai", MCP_FS_URL_TOKEN="true")
        try:
            print("Behind a tunnel:")
            run_tunnel(f"http://127.0.0.1:{port}")
        finally:
            server.terminate()
            server.wait(10)
    print("\nALL TESTS PASSED")


def test_e2e() -> None:  # pytest entry
    main()


if __name__ == "__main__":
    main()
