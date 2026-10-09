"""OAuth 2.1 resource-server support for remote multi-user deployments.

This server never issues tokens. Point it at your own authorization server (Keycloak,
Auth0, Okta, Entra ID, Authentik...) and it validates each bearer token with RFC 7662
token introspection, so no JWT/JWKS dependency is needed. Per-user access:

* ``user_vaults``: token subject -> allowed vault ids/names (``"*"`` = all). Users not
  listed get no vault access.
* ``admins``: subjects allowed to connect/disconnect vaults and change server settings.
* ``oauth_write_scope``: when set, tokens without this scope are read-only.

Without OAuth configured (stdio, or HTTP with a shared bearer token) there is no
principal and these checks do not apply.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from .config import Config

CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]


def current_principal() -> Principal | None:
    token = get_access_token()
    if token is None:
        return None
    return Principal(token.subject or token.client_id, frozenset(token.scopes))


class IntrospectionVerifier:
    """RFC 7662 token introspection, with a short positive/negative cache."""

    def __init__(self, config: Config, timeout: float = 10.0) -> None:
        if not config.oauth_introspection_url:
            raise ValueError("oauth_introspection_url is required for OAuth.")
        self.url = config.oauth_introspection_url
        self.resource = config.oauth_resource_url
        self.client_id = os.environ.get(config.oauth_client_id_env or "", "") or None
        self.client_secret = os.environ.get(config.oauth_client_secret_env or "", "") or None
        self.timeout = timeout
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}
        self._lock = threading.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        import anyio

        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(token)
            if hit and hit[0] > now:
                return hit[1]
        result = await anyio.to_thread.run_sync(self._introspect, token)
        with self._lock:
            if len(self._cache) > 10_000:
                self._cache.clear()
            expiry = now + CACHE_SECONDS
            if result is not None and result.expires_at:
                expiry = min(expiry, now + max(0, result.expires_at - time.time()))
            self._cache[token] = (expiry, result)
        return result

    def _introspect(self, token: str) -> AccessToken | None:
        body = urllib.parse.urlencode({"token": token, "token_type_hint": "access_token"}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if self.client_id:
            raw = f"{urllib.parse.quote(self.client_id)}:{urllib.parse.quote(self.client_secret or '')}"
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            return None  # fail closed
        if not data.get("active"):
            return None
        exp = data.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            return None
        aud = data.get("aud")
        audiences = aud if isinstance(aud, list) else [aud] if aud else []
        if self.resource and audiences and self.resource not in audiences:
            return None
        scopes = data.get("scope", "")
        return AccessToken(token=token, client_id=str(data.get("client_id") or ""),
                           scopes=scopes.split() if isinstance(scopes, str) else list(scopes),
                           expires_at=int(exp) if isinstance(exp, (int, float)) else None,
                           resource=self.resource if (not audiences or self.resource in audiences) else None,
                           subject=data.get("sub"), claims={k: data[k] for k in ("iss", "username") if k in data})


def allowed_vaults(config: Config, principal: Principal) -> set[str] | None:
    """Vault ids/names the principal may use; None means all."""
    allowed = config.user_vaults.get(principal.subject, [])
    if "*" in allowed:
        return None
    return set(allowed)


def is_admin(config: Config, principal: Principal | None) -> bool:
    return principal is None or principal.subject in config.admins
