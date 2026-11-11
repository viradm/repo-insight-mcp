# repo-insight-mcp

An MCP server that gives an AI agent read-only, structured access to a GitHub repository.

Built for the case where an agent has to reason about a codebase nobody on the
team wrote: a legacy service being migrated, a dependency audit, a repo you have
been handed on day one. The agent gets six tools instead of a shell and a prayer.

Written against the Model Context Protocol Python SDK v2 (`MCPServer`).

## Why these six tools

An agent exploring an unfamiliar repository asks the same questions in the same
order, so the tools are shaped around that sequence rather than around GitHub's
API surface.

| Tool | Answers |
|---|---|
| `repo_profile` | What is this, how big, what languages, still maintained? |
| `list_tree` | What files exist, filtered by directory or extension |
| `read_file` | What does this specific file say |
| `list_dependencies` | What does it depend on, across every ecosystem at once |
| `file_history` | Is this code churning or has it sat untouched for years |
| `search_code` | Where is this symbol or string used |

`list_dependencies` is the one that earns its place. It walks the tree, finds
every `requirements.txt`, `pyproject.toml`, `package.json`, `pom.xml`, `go.mod`
and `Gemfile`, parses each one and returns a single normalised list:

```json
{
  "ecosystems": ["maven", "npm", "pypi"],
  "count": 47,
  "dependencies": [
    {"name": "httpx", "version": ">=0.27", "ecosystem": "pypi",
     "source_file": "requirements.txt", "scope": "runtime"},
    {"name": "org.springframework:spring-core", "version": "5.3.39",
     "ecosystem": "maven", "source_file": "service/pom.xml", "scope": "runtime"}
  ],
  "problems": []
}
```

The agent does not need to know that Maven nests coordinates in XML and npm uses
a flat object. A manifest that fails to parse lands in `problems` and the rest of
the results still come back, because one broken file in a monorepo should not
fail the whole call.

## Install

```bash
git clone https://github.com/viradm/repo-insight-mcp
cd repo-insight-mcp
pip install -e ".[dev]"
```

Python 3.11 or newer.

## Configure

Add it to any MCP client. For Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "repo-insight": {
      "command": "repo-insight-mcp",
      "env": { "GITHUB_TOKEN": "ghp_your_token_here" }
    }
  }
}
```

For Claude Code:

```bash
claude mcp add repo-insight --env GITHUB_TOKEN=ghp_... -- repo-insight-mcp
```

Or run it directly over stdio:

```bash
GITHUB_TOKEN=ghp_... python -m repo_insight_mcp
```

### The token

Everything except `search_code` works unauthenticated, but GitHub allows only 60
requests an hour that way and a single agent run will spend them. A classic token
with the `public_repo` scope raises the limit to 5000 and enables code search.
Set `GITHUB_TOKEN` or `GH_TOKEN`.

## Design notes

**Caching.** An agent chaining tool calls asks for the same tree three times in a
row. Responses are cached in memory with a 300 second TTL, which is long enough
for a conversation and short enough that a push mid-session is picked up. Override
with `REPO_INSIGHT_TTL`.

**Rate limits.** A secondary limit that resets within five seconds is waited out
and retried once. A primary limit resets in minutes, so it is raised immediately
as a `RateLimited` error naming the token variable, rather than hanging the agent
on a call that cannot succeed.

**Truncation is explicit.** `read_file` caps at 40 KB by default and returns
`truncated: true` with the real byte count. `list_tree` returns `matched` next to
`returned`, so an agent can tell "there are 12 Python files" from "there are 400
and you are looking at 300 of them". Silently returning a partial answer that
looks complete is how agents reach confident wrong conclusions.

**Vendored code is skipped.** `node_modules` and `vendor` directories are
excluded from the dependency walk. Other people's dependencies are not this
repository's dependencies.

**Errors are typed.** `RepoNotFound`, `RateLimited` and `GitHubError` are
distinct, and tool-facing failures are raised as `ToolError` so the client gets
a usable message instead of a traceback.

## Tests

```bash
pytest
```

51 tests, no network. HTTP is mocked with `respx`, so the suite covers the paths
that are awkward to reach against the live API: a 404, an exhausted primary rate
limit, a secondary limit that retries and then succeeds, a cache hit, a cache
expiry, invalid UTF-8 in a file, a directory passed where a file was expected,
and a malformed manifest sitting next to a valid one.

The server tests call tools through `mcp.call_tool`, the same path an MCP client
takes, so the registered schemas are exercised rather than just the Python
functions underneath them.

## Licence

MIT
