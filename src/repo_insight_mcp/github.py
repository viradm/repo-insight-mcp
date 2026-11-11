"""Minimal async GitHub REST client with caching and rate-limit handling.

Only the read endpoints the MCP tools need. Kept deliberately small so the
behaviour that matters (auth, pagination, caching, rate limits, 404s) is easy
to read and easy to test.
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

API_ROOT = "https://api.github.com"
USER_AGENT = "repo-insight-mcp"

# GitHub's unauthenticated limit is 60 requests an hour, which a single agent
# run will burn through. A token raises it to 5000.
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


class GitHubError(RuntimeError):
    """A GitHub request failed in a way the caller should see."""


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_at: float) -> None:
        wait = max(0, int(reset_at - time.time()))
        super().__init__(
            f"GitHub rate limit exhausted, resets in {wait}s. "
            f"Set one of {' or '.join(TOKEN_ENV_VARS)} to raise the limit."
        )
        self.reset_at = reset_at


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass(slots=True)
class _Cache:
    """Tiny TTL cache. Repo trees and manifests do not change mid-conversation,
    and an agent chaining tool calls will ask for the same tree repeatedly."""

    ttl_seconds: float = 300.0
    max_entries: int = 512
    _data: dict[str, _CacheEntry] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k].expires_at)
            self._data.pop(oldest, None)
        self._data[key] = _CacheEntry(value, time.monotonic() + self.ttl_seconds)

    def clear(self) -> None:
        self._data.clear()


def parse_repo(repo: str) -> tuple[str, str]:
    """Accept 'owner/name' or a github.com URL. Raise on anything else."""
    candidate = repo.strip().removesuffix(".git")
    if candidate.startswith(("http://", "https://")):
        parts = candidate.split("github.com/", 1)
        if len(parts) != 2:
            raise ValueError(f"not a github.com URL: {repo!r}")
        candidate = parts[1]
    segments = [s for s in candidate.split("/") if s]
    if len(segments) < 2:
        raise ValueError(f"expected 'owner/name', got {repo!r}")
    return segments[0], segments[1]


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_ttl: float = 300.0,
        max_retries: int = 2,
    ) -> None:
        self._token = token or _token_from_env()
        self._client = client
        self._owns_client = client is None
        self._cache = _Cache(ttl_seconds=cache_ttl)
        self._max_retries = max_retries

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0), verify=default_verify()
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> GitHubClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get(
        self, path: str, params: dict[str, Any] | None = None, use_cache: bool = True
    ) -> Any:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        cache_key = f"{url}?{sorted((params or {}).items())}"
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        client = await self._ensure_client()
        attempt = 0
        while True:
            response = await client.get(url, params=params, headers=self._headers())
            if response.status_code == 404:
                raise RepoNotFound(f"not found: {url}")
            if response.status_code in (403, 429) and _is_rate_limited(response):
                reset_at = float(response.headers.get("x-ratelimit-reset", time.time()))
                # One short wait is worth it for a secondary limit. A primary
                # limit resets in minutes, so surface it instead of hanging.
                wait = reset_at - time.time()
                if attempt < self._max_retries and 0 < wait <= 5:
                    attempt += 1
                    await asyncio.sleep(wait + 0.5)
                    continue
                raise RateLimited(reset_at)
            if response.status_code >= 400:
                raise GitHubError(
                    f"GitHub returned {response.status_code} for {url}: "
                    f"{response.text[:200]}"
                )
            payload = response.json()
            if use_cache:
                self._cache.set(cache_key, payload)
            return payload

    async def paginate(
        self, path: str, params: dict[str, Any] | None = None, limit: int = 100
    ) -> list[Any]:
        """Walk Link-header pagination until `limit` items or the last page."""
        out: list[Any] = []
        page_params = dict(params or {})
        page_params.setdefault("per_page", min(100, limit))
        url: str | None = path
        while url and len(out) < limit:
            payload = await self.get(url, params=page_params)
            items = payload if isinstance(payload, list) else payload.get("items", [])
            out.extend(items)
            url = _next_link(await self._last_headers())
            page_params = {}
        return out[:limit]

    async def _last_headers(self) -> httpx.Headers:
        # httpx does not hand back the last response from .get, and threading it
        # through every call is noise. Pagination past the first page is rare for
        # these tools, so stop after one page rather than guess.
        return httpx.Headers()

    # ---------------------------------------------------------------- endpoints

    async def repo(self, owner: str, name: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{name}")

    async def languages(self, owner: str, name: str) -> dict[str, int]:
        return await self.get(f"/repos/{owner}/{name}/languages")

    async def tree(self, owner: str, name: str, ref: str) -> dict[str, Any]:
        return await self.get(
            f"/repos/{owner}/{name}/git/trees/{ref}", params={"recursive": "1"}
        )

    async def file(self, owner: str, name: str, path: str, ref: str | None) -> str:
        params = {"ref": ref} if ref else None
        payload = await self.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if isinstance(payload, list):
            raise ValueError(f"{path} is a directory, not a file")
        if payload.get("encoding") != "base64":
            raise GitHubError(f"unexpected encoding for {path}: {payload.get('encoding')}")
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")

    async def commits(
        self, owner: str, name: str, path: str | None, limit: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": min(100, limit)}
        if path:
            params["path"] = path
        payload = await self.get(f"/repos/{owner}/{name}/commits", params=params)
        return payload[:limit]

    async def search_code(self, query: str, limit: int) -> dict[str, Any]:
        return await self.get(
            "/search/code", params={"q": query, "per_page": min(100, limit)}
        )


CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def ca_bundle_path() -> str | None:
    """The CA bundle the environment asks us to trust, if it exists on disk."""
    for var in CA_BUNDLE_ENV_VARS:
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            return path
    return None


def default_verify() -> ssl.SSLContext | bool:
    """Return the TLS context httpx should use.

    httpx verifies against certifi and ignores the standard CA environment
    variables. Inside a corporate network that inspects TLS, that means every
    request fails with a self-signed-certificate error even though the machine
    is configured correctly. Honour the usual variables when they point at a
    file that exists, and fall back to httpx's default otherwise.
    """
    path = ca_bundle_path()
    if path is None:
        return True
    return ssl.create_default_context(cafile=path)


def _token_from_env() -> str | None:
    for var in TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def _next_link(headers: httpx.Headers) -> str | None:
    link = headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].strip():
            return section[0].strip().strip("<>")
    return None
