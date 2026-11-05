# remote-fs-mcp

**Give any AI access to your computer's files and terminal, from anywhere.**

Run this MCP server on your PC, a VPS, or in Docker. Then connect Claude, Cursor, VS Code, or your own agent to it. The AI can read, write, edit and search files, and run terminal commands on that machine. That works even when you're only using a browser or your phone.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![MCP](https://img.shields.io/badge/MCP-2026--07--28-green) ![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

- **Files and folders:** read, write, edit, list, search, copy, move and delete
- **Terminal:** run commands, plus start long-running processes like dev servers and watch their output
- **Fast:** stateless HTTP; uses ripgrep for search when it's installed
- **Secure:** token auth, every file locked to one folder, read-only and no-commands modes
- **Works everywhere:** Windows, macOS and Linux, Docker, VPS, or your PC through a tunnel
- **Any MCP client:** Claude (web, desktop and Code), Cursor, VS Code, or your own code

---

## Quick start

### 1. Install

```bash
git clone https://github.com/RaheesAhmed/remote-fs-mcp.git
cd remote-fs-mcp
```

Then install:

```bash
uv sync
```

### 2. Configure

Copy the example config:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and set at least these two values:

```env
MCP_FS_TOKEN=put-a-long-random-string-here
MCP_FS_ROOT=D:\projects            # the folder the AI is allowed to work in
```

Generate a strong token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Run

```bash
uv run remote-fs-mcp
```

The server is now running at `http://localhost:8765/mcp`.
Check that it's up by opening `http://localhost:8765/health`.

---

## Use it from anywhere (browser or phone)

Your PC has to be reachable from the internet. Pick one of these options.

### Option A: Tunnel from your PC (easiest)

Use [ngrok](https://ngrok.com) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/). Add these lines to `.env`:

```env
MCP_FS_ALLOWED_HOSTS=*.ngrok-free.app       # cloudflared: *.trycloudflare.com
MCP_FS_ALLOWED_ORIGINS=https://claude.ai
MCP_FS_URL_TOKEN=true
```

Start the server, then start the tunnel in a second terminal:

```bash
remote-fs-mcp
ngrok http 8765          # or: cloudflared tunnel --url http://localhost:8765
```

Your server URL is the address ngrok gives you, for example `https://abc123.ngrok-free.app`.

### Option B: VPS with your own domain

1. Run the server on the VPS with `MCP_FS_HOST=127.0.0.1`.
2. Put Caddy in front of it for free HTTPS. A ready config is in `deploy/Caddyfile`.
3. Set `MCP_FS_ALLOWED_HOSTS=mcp.yourdomain.com`.
4. Optional: run it as a background service with `deploy/remote-fs-mcp.service`.

### Option C: Docker (safest)

Commands run inside the container, not directly on your machine.

```bash
cp .env.example .env
mkdir workspace
docker compose up -d --build
```

Put the files you want the AI to work on in `./workspace`.

---

## Connect your AI

In every example below:
- Replace `https://YOUR-URL` with your server address (local: `http://localhost:8765`).
- Replace `YOUR_TOKEN` with your `MCP_FS_TOKEN`.

### Claude (web and mobile)

1. Go to **Settings → Connectors → Add custom connector**.
2. Paste this URL: `https://YOUR-URL/YOUR_TOKEN`
3. Leave the auth settings empty.

This needs `MCP_FS_URL_TOKEN=true` in `.env`, as shown in Option A.

### Claude Code

```bash
claude mcp add --transport http remote-fs https://YOUR-URL/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

### Cursor

Add this to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "remote-fs": {
      "url": "https://YOUR-URL/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

### VS Code / GitHub Copilot

Add this to `.vscode/mcp.json`:

```json
{
  "servers": {
    "remote-fs": {
      "type": "http",
      "url": "https://YOUR-URL/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

### Claude Desktop (and other apps that only support local commands)

```json
{
  "mcpServers": {
    "remote-fs": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-URL/mcp",
               "--header", "Authorization: Bearer YOUR_TOKEN"]
    }
  }
}
```

### Same computer, no network

```json
{
  "mcpServers": {
    "remote-fs": {
      "command": "remote-fs-mcp",
      "args": ["--stdio", "--root", "/path/to/project"]
    }
  }
}
```

### Your own Python code

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("https://YOUR-URL/mcp", auth="YOUR_TOKEN") as ai:
        result = await ai.call_tool("run_command", {"command": "dir"})
        print(result.structured_content["stdout"])

asyncio.run(main())
```

### Any language (plain HTTP)

```bash
curl -s https://YOUR-URL/mcp \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_directory","arguments":{"path":"."}}}'
```

---

## Usage

Once connected, talk to your AI normally. It picks the right tools on its own.

**Example prompts:**

- "Show me what's in my project folder."
- "Find every file that uses `fetchUser` and rename it to `getUser`."
- "Read `src/app.py` and fix the bug on line 40."
- "Create a new Next.js app in a folder called `my-site`."
- "Run `npm install`, then start the dev server and tell me when it's ready."
- "Run the tests and fix whatever fails."
- "Check git status and commit my changes with a good message."
- "Zip the `reports` folder."

All paths are relative to `MCP_FS_ROOT`, so `src/app.py` means `<root>/src/app.py`.

### Tools

**Files and folders**

| Tool | What it does |
|---|---|
| `read_file` | Read a file, or a range of lines (`offset`, `limit`). Binary files can be read as base64. |
| `read_many_files` | Read up to 50 files in one call. |
| `write_file` | Create or overwrite a file. Modes: `overwrite`, `append`, `create_only`. Missing folders are created. |
| `edit_file` | Find and replace exact text in a file and get back a diff. Supports `dry_run`. |
| `list_directory` | List a folder. Use `depth` to go deeper; `node_modules`, `.git` and similar are skipped. |
| `get_file_info` | Size, type, permissions and dates. |
| `create_directory` | Create a folder, including any missing parents. |
| `copy_path` | Copy a file or a whole folder. |
| `move_path` | Move or rename a file or folder. |
| `delete_path` | Delete a file or folder. A folder that isn't empty needs `recursive: true`. |

**Search**

| Tool | What it does |
|---|---|
| `find_files` | Find files by name, e.g. `*.py` or `src/**/test_*.js`. |
| `search_text` | Search inside files, as plain text or regex, with optional context lines. |

**Terminal**

| Tool | What it does |
|---|---|
| `run_command` | Run a command and wait for it to finish. Returns output and exit code. Has a timeout. |
| `start_process` | Start something that keeps running (dev server, watcher) and return its id. |
| `process_output` | Read new output from a running process. |
| `send_process_input` | Type into a running process, e.g. to answer a prompt. |
| `stop_process` | Stop a process and everything it started. |
| `list_processes` | Show the processes that are running. |

**Info**

| Tool | What it does |
|---|---|
| `server_info` | Root folder, OS, shell, and which features are enabled. |

---

## Configuration

All settings go in `.env`.

| Setting | Default | What it does |
|---|---|---|
| `MCP_FS_TOKEN` | auto-generated | Access token. |
| `MCP_FS_TOKENS` | – | Several tokens, comma separated, so each AI gets its own. |
| `MCP_FS_ROOT` | current folder | The only folder the file tools can access. |
| `MCP_FS_READ_ONLY` | `false` | Allow reading only. Hides all write, delete and command tools. |
| `MCP_FS_ALLOW_COMMANDS` | `true` | Turn the terminal tools on or off. |
| `MCP_FS_ALLOW_DELETE` | `true` | Turn deleting on or off. |
| `MCP_FS_DENY_GLOBS` | – | Files the AI can never touch, e.g. `.env,*.pem,*.key`. |
| `MCP_FS_ALLOW_HIDDEN` | `true` | Allow access to dotfiles. |
| `MCP_FS_ALLOWED_HOSTS` | – | Your public domain. Wildcards work, e.g. `*.ngrok-free.app`. |
| `MCP_FS_ALLOWED_ORIGINS` | – | Allowed web origins, e.g. `https://claude.ai`. |
| `MCP_FS_URL_TOKEN` | `false` | Accept the token in the URL: `https://host/<token>`. |
| `MCP_FS_HOST` | `0.0.0.0` | Address to listen on. |
| `MCP_FS_PORT` | `8765` | Port. |
| `MCP_FS_SHELL` | bash / cmd.exe | Shell used for commands, e.g. `pwsh`. |
| `MCP_FS_COMMAND_TIMEOUT` | `120` | Default command timeout in seconds. |
| `MCP_FS_MAX_COMMAND_TIMEOUT` | `1800` | Longest timeout a command can ask for. |
| `MCP_FS_MAX_OUTPUT_CHARS` | `100000` | Maximum size of each tool response. |
| `MCP_FS_MAX_PROCESSES` | `16` | Maximum background processes at once. |

**Command-line flags** (these override `.env`):

```bash
remote-fs-mcp --root D:\projects --port 9000
remote-fs-mcp --read-only          # AI can only look, not change anything
remote-fs-mcp --no-commands        # files only, no terminal
remote-fs-mcp --stdio              # local use, no network
```

---

## Security

> ⚠️ **This gives an AI real control over a computer. Treat your token like a password.**

- Every request needs your token. Requests without it are rejected.
- File tools only work inside `MCP_FS_ROOT`. Paths using `../`, other drives, and symlinks that point outside are all blocked.
- **Terminal commands are not limited to the root folder.** They can do anything your user account can do. If others might use the server, run it in **Docker** or a VM, or use `--no-commands`.
- Always use **HTTPS** on the internet. Tunnels and Caddy give you HTTPS automatically.
- If you use `MCP_FS_URL_TOKEN`, the URL itself is the password. Don't share it. If it leaks, change `MCP_FS_TOKEN` and restart.
- Your token is hidden from the commands the AI runs.
- Use `MCP_FS_DENY_GLOBS` to protect secrets like `.env` files and keys.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `421 Misdirected Request` | Add your domain to `MCP_FS_ALLOWED_HOSTS`. |
| `403 Forbidden Origin` | Add the site to `MCP_FS_ALLOWED_ORIGINS`, e.g. `https://claude.ai`. |
| `401 Unauthorized` | The token is missing or wrong. |
| `404` with the token in the URL | Set `MCP_FS_URL_TOKEN=true` and check the token. |
| Search is slow | Install ripgrep: `winget install BurntSushi.ripgrep.MSVC`, `brew install ripgrep`, or `apt install ripgrep`. |
| AI can't find my files | Check `MCP_FS_ROOT`. Ask the AI to call `server_info` to see the current root. |

---

## Development

```bash
pip install -e ".[dev]"
python tests/test_e2e.py
```

The tests start a real server and check every tool, the auth, and the sandbox.

---

## License

MIT. Use it, change it, ship it.