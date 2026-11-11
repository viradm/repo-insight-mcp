"""Tool-level tests. These call the tools the way an MCP client does, through
the server's call_tool path, so the registered schema is exercised too."""

import base64
import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from repo_insight_mcp.github import GitHubClient
from repo_insight_mcp.server import mcp, set_client

API = "https://api.github.com"


@pytest.fixture(autouse=True)
async def client():
    c = GitHubClient(token="test-token", cache_ttl=0)
    set_client(c)
    yield c
    await c.aclose()
    set_client(None)


async def call(name: str, **arguments):
    """Invoke a tool through the server the way an MCP client does, and return
    its structured content. Raises if the server reported a tool error."""
    result = await mcp.call_tool(name, arguments)
    if result.is_error:
        raise ToolError(
            " ".join(getattr(c, "text", "") for c in (result.content or []))
        )
    return result.structured_content


def tree_response(paths):
    return {
        "tree": [{"path": p, "type": "blob", "size": len(p)} for p in paths],
        "truncated": False,
    }


def contents(text: str):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


async def test_every_tool_is_registered_with_a_description():
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "repo_profile",
        "list_tree",
        "read_file",
        "list_dependencies",
        "file_history",
        "search_code",
    }
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert "repo" in tool.input_schema["properties"]


@respx.mock
async def test_repo_profile_ranks_languages_by_share():
    respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "o/n",
                "default_branch": "main",
                "size": 120,
                "license": {"spdx_id": "MIT"},
            },
        )
    )
    respx.get(f"{API}/repos/o/n/languages").mock(
        return_value=httpx.Response(200, json={"Python": 7500, "Shell": 2500})
    )
    out = await call("repo_profile", repo="o/n")
    assert out["default_branch"] == "main"
    assert out["license"] == "MIT"
    assert out["languages"][0] == {"name": "Python", "bytes": 7500, "percent": 75.0}


@respx.mock
async def test_list_tree_resolves_head_to_the_default_branch():
    respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={"default_branch": "develop"})
    )
    route = respx.get(f"{API}/repos/o/n/git/trees/develop").mock(
        return_value=httpx.Response(200, json=tree_response(["a.py"]))
    )
    out = await call("list_tree", repo="o/n")
    assert out["ref"] == "develop"
    assert route.called


@respx.mock
async def test_list_tree_filters_by_prefix_and_extension():
    paths = ["src/a.py", "src/b.js", "docs/c.py", "src/nested/d.py"]
    respx.get(f"{API}/repos/o/n/git/trees/main").mock(
        return_value=httpx.Response(200, json=tree_response(paths))
    )
    out = await call("list_tree", repo="o/n", ref="main", path_prefix="src/", extension="py")
    assert [f["path"] for f in out["files"]] == ["src/a.py", "src/nested/d.py"]
    assert out["matched"] == 2


@respx.mock
async def test_list_tree_reports_how_many_it_withheld():
    respx.get(f"{API}/repos/o/n/git/trees/main").mock(
        return_value=httpx.Response(200, json=tree_response([f"f{i}.py" for i in range(50)]))
    )
    out = await call("list_tree", repo="o/n", ref="main", limit=5)
    assert out["matched"] == 50
    assert out["returned"] == 5
    assert len(out["files"]) == 5


@respx.mock
async def test_read_file_truncates_and_says_so():
    respx.get(f"{API}/repos/o/n/contents/big.py").mock(
        return_value=httpx.Response(200, json=contents("x" * 5000))
    )
    out = await call("read_file", repo="o/n", path="big.py", max_bytes=1000)
    assert out["truncated"] is True
    assert out["bytes"] == 5000
    assert len(out["content"]) == 1000


@respx.mock
async def test_read_file_leaves_small_files_intact():
    respx.get(f"{API}/repos/o/n/contents/small.py").mock(
        return_value=httpx.Response(200, json=contents("a\nb\n"))
    )
    out = await call("read_file", repo="o/n", path="small.py")
    assert out["truncated"] is False
    assert out["content"] == "a\nb\n"


@respx.mock
async def test_list_dependencies_merges_ecosystems_and_skips_vendored_trees():
    paths = [
        "requirements.txt",
        "frontend/package.json",
        "node_modules/left-pad/package.json",
        "README.md",
    ]
    respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get(f"{API}/repos/o/n/git/trees/main").mock(
        return_value=httpx.Response(200, json=tree_response(paths))
    )
    respx.get(f"{API}/repos/o/n/contents/requirements.txt").mock(
        return_value=httpx.Response(200, json=contents("httpx>=0.27\n"))
    )
    respx.get(f"{API}/repos/o/n/contents/frontend/package.json").mock(
        return_value=httpx.Response(
            200, json=contents(json.dumps({"dependencies": {"react": "^18.3.1"}}))
        )
    )
    out = await call("list_dependencies", repo="o/n")
    assert out["manifests"] == ["requirements.txt", "frontend/package.json"]
    assert out["ecosystems"] == ["npm", "pypi"]
    assert {d["name"] for d in out["dependencies"]} == {"httpx", "react"}
    assert out["problems"] == []


@respx.mock
async def test_list_dependencies_reports_a_bad_manifest_without_losing_the_good_ones():
    respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get(f"{API}/repos/o/n/git/trees/main").mock(
        return_value=httpx.Response(
            200, json=tree_response(["requirements.txt", "package.json"])
        )
    )
    respx.get(f"{API}/repos/o/n/contents/requirements.txt").mock(
        return_value=httpx.Response(200, json=contents("httpx\n"))
    )
    respx.get(f"{API}/repos/o/n/contents/package.json").mock(
        return_value=httpx.Response(200, json=contents("{broken"))
    )
    out = await call("list_dependencies", repo="o/n")
    assert {d["name"] for d in out["dependencies"]} == {"httpx"}
    assert len(out["problems"]) == 1
    assert "package.json" in out["problems"][0]


@respx.mock
async def test_file_history_flattens_commit_metadata():
    respx.get(f"{API}/repos/o/n/commits").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "sha": "abcdef1234567890" + "0" * 24,
                    "commit": {
                        "author": {"name": "Ada", "date": "2026-01-02T03:04:05Z"},
                        "message": "fix the thing\n\nlonger body",
                    },
                }
            ],
        )
    )
    out = await call("file_history", repo="o/n", path="src/app.py", limit=5)
    commit = out["commits"][0]
    assert commit["sha"] == "abcdef123456"
    assert commit["message"] == "fix the thing"
    assert commit["author"] == "Ada"


@respx.mock
async def test_search_code_scopes_the_query_to_the_repo():
    route = respx.get(f"{API}/search/code").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 1, "items": [{"path": "a.py", "html_url": "u"}]},
        )
    )
    out = await call("search_code", repo="o/n", query="TODO", extension="py")
    assert "repo:o/n" in out["query"]
    assert "extension:py" in out["query"]
    assert out["matches"][0]["path"] == "a.py"
    assert route.called


async def test_search_code_refuses_without_a_token():
    unauth = GitHubClient(token=None)
    unauth._token = None
    set_client(unauth)
    with pytest.raises(ToolError, match="GITHUB_TOKEN"):
        await call("search_code", repo="o/n", query="TODO")
    await unauth.aclose()


async def test_a_malformed_repo_argument_is_a_tool_error_not_a_traceback():
    with pytest.raises(ToolError, match="owner/name"):
        await call("repo_profile", repo="not-a-repo")
