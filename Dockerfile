FROM python:3.12-slim

# ripgrep = fast search, git/curl/build tools = useful for the AI's commands
RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY remote_fs_mcp ./remote_fs_mcp
RUN pip install --no-cache-dir .

# Run as a normal user, not root
RUN useradd -m -u 1000 agent && mkdir -p /workspace && chown agent:agent /workspace
USER agent

ENV MCP_FS_ROOT=/workspace \
    MCP_FS_HOST=0.0.0.0 \
    MCP_FS_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health')" || exit 1

CMD ["remote-fs-mcp"]
