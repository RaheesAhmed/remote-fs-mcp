"""Static bearer-token auth with constant-time comparison.

Clients send:  Authorization: Bearer <token>
Multiple tokens (comma separated in MCP_FS_TOKENS) let you give each AI/client
its own key and revoke one without touching the others.

For clients that cannot set headers (some web connectors), MCP_FS_URL_TOKEN=true
also accepts the token as the first URL segment:  https://host/<token>  or  https://host/<token>/mcp
"""

from __future__ import annotations

import hashlib
import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class TokenSet:
    def __init__(self, tokens: list[str]) -> None:
        # Keep only hashes in memory; compare digests in constant time.
        self._digests = [hashlib.sha256(t.encode()).digest() for t in tokens]

    def match(self, token: str) -> int | None:
        """Index of the matching token, or None. Checks every token (no early exit)."""
        candidate = hashlib.sha256(token.encode()).digest()
        found = None
        for i, d in enumerate(self._digests):
            if hmac.compare_digest(candidate, d):
                found = i
        return found


class StaticBearerVerifier(TokenVerifier):
    def __init__(self, tokens: TokenSet) -> None:
        super().__init__()
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        idx = self._tokens.match(token)
        if idx is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"client-{idx + 1}",
            scopes=["fs", "shell"],
            expires_at=None,
        )


class EntryMiddleware:
    """Outermost ASGI layer.

    - URL token: /<token>/rest -> /rest with an Authorization header injected (opt-in).
    - Root alias: a request to "/" is served by the MCP endpoint, so URLs without /mcp work.
    """

    def __init__(self, app, tokens: TokenSet | None, url_token: bool, mcp_path: str) -> None:
        self.app = app
        self.tokens = tokens
        self.url_token = url_token and tokens is not None
        self.mcp_path = mcp_path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path: str = scope["path"]
            if self.url_token and len(path) > 1:
                first, _, rest = path[1:].partition("/")
                if len(first) >= 16 and self.tokens.match(first) is not None:
                    headers = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
                    headers.append((b"authorization", b"Bearer " + first.encode()))
                    path = "/" + rest
                    scope = {**scope, "headers": headers, "path": path, "raw_path": path.encode()}
            if path in ("", "/") and self.mcp_path != "/":
                scope = {**scope, "path": self.mcp_path, "raw_path": self.mcp_path.encode()}
        await self.app(scope, receive, send)
