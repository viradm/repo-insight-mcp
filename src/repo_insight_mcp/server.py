"""MCP server exposing a GitHub repository to an agent as callable tools.

Built for the case where an agent has to understand a codebase it has never
seen: what is in it, what it depends on, which files change most, and what a
given file actually says. Each tool returns structured data so results can be
chained without re-parsing prose.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .dependencies import MANIFESTS, parse_manifest
from .github import GitHubClient, GitHubError, parse_repo

mcp = MCPServer(
    name="repo-insight",
    version="0.1.0",
    instructions=(
        "Read-only tools for understanding a GitHub repository: structure, "
        "file contents, dependency manifests, change history and code search. "
        "Call repo_profile first to orient, then list_tree to find paths, then "
        "read_file on the paths that matter. Set GITHUB_TOKEN to raise the "
        "GitHub rate limit from 60 to 5000 requests an hour."
    ),
)

_client: GitHubClient | None = None

# Reading a whole vendored bundle into a context window helps nobody.
DEFAULT_MAX_BYTES = 40_000
HARD_MAX_BYTES = 200_000


def get_client() -> GitHubClient:
    global _client
    if _client is None:
        _client = GitHubClient(cache_ttl=float(os.environ.get("REPO_INSIGHT_TTL", 300)))
    return _client


def set_client(client: GitHubClient | None) -> None:
    """Injection point for tests."""
    global _client
    _client = client


def _split(repo: str) -> tuple[str, str]:
    try:
        return parse_repo(repo)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def repo_profile(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
) -> dict[str, Any]:
    """Summarise a repository: description, default branch, size, languages,
    last push and open issue count. Start here before any other tool."""
    owner, name = _split(repo)
    client = get_client()
    try:
        meta = await client.repo(owner, name)
        languages = await client.languages(owner, name)
    except GitHubError as exc:
        raise ToolError(str(exc)) from exc

    total = sum(languages.values()) or 1
    return {
        "full_name": meta.get("full_name"),
        "description": meta.get("description"),
        "default_branch": meta.get("default_branch"),
        "size_kb": meta.get("size"),
        "stars": meta.get("stargazers_count"),
        "open_issues": meta.get("open_issues_count"),
        "archived": meta.get("archived"),
        "pushed_at": meta.get("pushed_at"),
        "created_at": meta.get("created_at"),
        "license": (meta.get("license") or {}).get("spdx_id"),
        "topics": meta.get("topics", []),
        "languages": [
            {"name": lang, "bytes": size, "percent": round(100 * size / total, 1)}
            for lang, size in sorted(languages.items(), key=lambda kv: -kv[1])
        ],
    }


@mcp.tool()
async def list_tree(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
    ref: Annotated[str, Field(description="branch, tag or commit SHA")] = "HEAD",
    path_prefix: Annotated[
        str, Field(description="only return paths starting with this prefix")
    ] = "",
    extension: Annotated[
        str, Field(description="only return files with this extension, e.g. 'py'")
    ] = "",
    limit: Annotated[int, Field(ge=1, le=2000)] = 300,
) -> dict[str, Any]:
    """List files in a repository with their sizes. Filter by directory prefix
    or extension to find the parts of a codebase worth reading."""
    owner, name = _split(repo)
    client = get_client()
    if ref == "HEAD":
        meta = await client.repo(owner, name)
        ref = meta.get("default_branch", "main")
    try:
        payload = await client.tree(owner, name, ref)
    except GitHubError as exc:
        raise ToolError(str(exc)) from exc

    suffix = f".{extension.lstrip('.')}" if extension else ""
    files = [
        {"path": node["path"], "size": node.get("size", 0)}
        for node in payload.get("tree", [])
        if node.get("type") == "blob"
        and node["path"].startswith(path_prefix)
        and (not suffix or node["path"].endswith(suffix))
    ]
    files.sort(key=lambda f: f["path"])
    return {
        "ref": ref,
        "truncated_by_github": payload.get("truncated", False),
        "matched": len(files),
        "returned": min(len(files), limit),
        "files": files[:limit],
    }


@mcp.tool()
async def read_file(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
    path: Annotated[str, Field(description="path within the repository")],
    ref: Annotated[str, Field(description="branch, tag or commit SHA")] = "",
    max_bytes: Annotated[int, Field(ge=100, le=HARD_MAX_BYTES)] = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Read one text file. Truncates at max_bytes and says so, rather than
    returning a file too large to be useful."""
    owner, name = _split(repo)
    try:
        text = await get_client().file(owner, name, path, ref or None)
    except (GitHubError, ValueError) as exc:
        raise ToolError(str(exc)) from exc

    encoded = text.encode("utf-8")
    truncated = len(encoded) > max_bytes
    if truncated:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {
        "path": path,
        "ref": ref or "default branch",
        "bytes": len(encoded),
        "truncated": truncated,
        "lines": text.count("\n") + 1,
        "content": text,
    }


@mcp.tool()
async def list_dependencies(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
    ref: Annotated[str, Field(description="branch, tag or commit SHA")] = "",
) -> dict[str, Any]:
    """Find and parse every dependency manifest in the repository, returning one
    normalised list across pypi, npm, maven, go and rubygems."""
    owner, name = _split(repo)
    client = get_client()
    resolved = ref
    if not resolved:
        meta = await client.repo(owner, name)
        resolved = meta.get("default_branch", "main")

    try:
        payload = await client.tree(owner, name, resolved)
    except GitHubError as exc:
        raise ToolError(str(exc)) from exc

    candidates = [
        node["path"]
        for node in payload.get("tree", [])
        if node.get("type") == "blob"
        and node["path"].rsplit("/", 1)[-1] in MANIFESTS
        # Other people's dependencies are not this repo's dependencies.
        and not any(part in node["path"].split("/") for part in ("node_modules", "vendor"))
    ]

    found: list[dict[str, Any]] = []
    problems: list[str] = []
    for manifest_path in candidates:
        try:
            content = await client.file(owner, name, manifest_path, resolved)
            for dep in parse_manifest(manifest_path, content):
                found.append(dep.to_dict())
        except (GitHubError, ValueError) as exc:
            problems.append(str(exc))

    ecosystems = sorted({d["ecosystem"] for d in found})
    return {
        "ref": resolved,
        "manifests": candidates,
        "ecosystems": ecosystems,
        "count": len(found),
        "dependencies": found,
        "problems": problems,
    }


@mcp.tool()
async def file_history(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
    path: Annotated[
        str, Field(description="path to trace, or empty for the whole repository")
    ] = "",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List recent commits, optionally for one path. Use it to see whether code
    is actively maintained or has sat untouched for years."""
    owner, name = _split(repo)
    try:
        commits = await get_client().commits(owner, name, path or None, limit)
    except GitHubError as exc:
        raise ToolError(str(exc)) from exc

    return {
        "path": path or "(repository)",
        "count": len(commits),
        "commits": [
            {
                "sha": c.get("sha", "")[:12],
                "date": (c.get("commit", {}).get("author") or {}).get("date"),
                "author": (c.get("commit", {}).get("author") or {}).get("name"),
                "message": (c.get("commit", {}).get("message") or "").split("\n", 1)[0],
            }
            for c in commits
        ],
    }


@mcp.tool()
async def search_code(
    repo: Annotated[str, Field(description="owner/name or a github.com URL")],
    query: Annotated[str, Field(description="text to search for in the code")],
    extension: Annotated[str, Field(description="restrict to one extension")] = "",
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Search code inside one repository. GitHub's code search requires
    authentication, so this needs GITHUB_TOKEN set."""
    owner, name = _split(repo)
    client = get_client()
    if not client.authenticated:
        raise ToolError(
            "GitHub code search requires authentication. Set GITHUB_TOKEN and retry."
        )
    q = f"{query} repo:{owner}/{name}"
    if extension:
        q += f" extension:{extension.lstrip('.')}"
    try:
        payload = await client.search_code(q, limit)
    except GitHubError as exc:
        raise ToolError(str(exc)) from exc

    return {
        "query": q,
        "total": payload.get("total_count", 0),
        "matches": [
            {"path": item.get("path"), "url": item.get("html_url")}
            for item in payload.get("items", [])[:limit]
        ],
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
