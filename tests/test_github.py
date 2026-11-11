import base64
import time

import httpx
import pytest
import respx

from repo_insight_mcp.github import (
    GitHubClient,
    GitHubError,
    RateLimited,
    RepoNotFound,
    ca_bundle_path,
    default_verify,
    parse_repo,
)

API = "https://api.github.com"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("viradm/repo-insight-mcp", ("viradm", "repo-insight-mcp")),
        ("  viradm/repo-insight-mcp  ", ("viradm", "repo-insight-mcp")),
        ("https://github.com/viradm/repo-insight-mcp", ("viradm", "repo-insight-mcp")),
        ("https://github.com/viradm/repo-insight-mcp.git", ("viradm", "repo-insight-mcp")),
        ("https://github.com/viradm/repo/tree/main", ("viradm", "repo")),
    ],
)
def test_parse_repo_accepts_the_shapes_people_actually_paste(value, expected):
    assert parse_repo(value) == expected


@pytest.mark.parametrize("value", ["viradm", "", "https://gitlab.com/a/b", "/"])
def test_parse_repo_rejects_everything_else(value):
    with pytest.raises(ValueError):
        parse_repo(value)


@respx.mock
async def test_get_sends_token_when_present():
    route = respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={"full_name": "o/n"})
    )
    async with GitHubClient(token="secret") as client:
        await client.repo("o", "n")
    assert route.calls.last.request.headers["authorization"] == "Bearer secret"


@respx.mock
async def test_get_omits_authorization_when_no_token():
    route = respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={})
    )
    async with GitHubClient(token=None) as client:
        client._token = None  # ignore any ambient GITHUB_TOKEN
        await client.repo("o", "n")
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_404_raises_repo_not_found():
    respx.get(f"{API}/repos/o/missing").mock(return_value=httpx.Response(404, json={}))
    async with GitHubClient(token="t") as client:
        with pytest.raises(RepoNotFound):
            await client.repo("o", "missing")


@respx.mock
async def test_primary_rate_limit_surfaces_rather_than_hanging():
    respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 3600),
            },
            json={"message": "API rate limit exceeded"},
        )
    )
    async with GitHubClient(token="t", max_retries=0) as client:
        with pytest.raises(RateLimited) as exc:
            await client.repo("o", "n")
    assert "GITHUB_TOKEN" in str(exc.value)


@respx.mock
async def test_short_secondary_limit_is_retried_once_then_succeeds():
    reset = str(int(time.time()) + 1)
    route = respx.get(f"{API}/repos/o/n").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
                json={"message": "secondary rate limit"},
            ),
            httpx.Response(200, json={"full_name": "o/n"}),
        ]
    )
    async with GitHubClient(token="t", max_retries=2) as client:
        assert (await client.repo("o", "n"))["full_name"] == "o/n"
    assert route.call_count == 2


@respx.mock
async def test_other_errors_include_status_and_body():
    respx.get(f"{API}/repos/o/n").mock(return_value=httpx.Response(500, text="boom"))
    async with GitHubClient(token="t") as client:
        with pytest.raises(GitHubError, match="500"):
            await client.repo("o", "n")


@respx.mock
async def test_identical_requests_hit_the_network_once():
    route = respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={"full_name": "o/n"})
    )
    async with GitHubClient(token="t", cache_ttl=60) as client:
        await client.repo("o", "n")
        await client.repo("o", "n")
    assert route.call_count == 1


@respx.mock
async def test_cache_expires():
    route = respx.get(f"{API}/repos/o/n").mock(
        return_value=httpx.Response(200, json={})
    )
    async with GitHubClient(token="t", cache_ttl=0) as client:
        await client.repo("o", "n")
        await client.repo("o", "n")
    assert route.call_count == 2


@respx.mock
async def test_file_decodes_base64_contents():
    body = base64.b64encode(b"print('hi')\n").decode()
    respx.get(f"{API}/repos/o/n/contents/main.py").mock(
        return_value=httpx.Response(200, json={"encoding": "base64", "content": body})
    )
    async with GitHubClient(token="t") as client:
        assert await client.file("o", "n", "main.py", "main") == "print('hi')\n"


@respx.mock
async def test_file_rejects_a_directory():
    respx.get(f"{API}/repos/o/n/contents/src").mock(
        return_value=httpx.Response(200, json=[{"name": "a.py"}])
    )
    async with GitHubClient(token="t") as client:
        with pytest.raises(ValueError, match="directory"):
            await client.file("o", "n", "src", None)


@respx.mock
async def test_file_tolerates_invalid_utf8_without_crashing():
    body = base64.b64encode(b"ok \xff\xfe bytes").decode()
    respx.get(f"{API}/repos/o/n/contents/blob.bin").mock(
        return_value=httpx.Response(200, json={"encoding": "base64", "content": body})
    )
    async with GitHubClient(token="t") as client:
        assert "ok" in await client.file("o", "n", "blob.bin", None)


@respx.mock
async def test_tree_requests_recursive_listing():
    route = respx.get(f"{API}/repos/o/n/git/trees/main").mock(
        return_value=httpx.Response(200, json={"tree": []})
    )
    async with GitHubClient(token="t") as client:
        await client.tree("o", "n", "main")
    assert route.calls.last.request.url.params["recursive"] == "1"


@respx.mock
async def test_commits_applies_path_filter_and_limit():
    payload = [{"sha": f"{i}" * 40} for i in range(10)]
    route = respx.get(f"{API}/repos/o/n/commits").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with GitHubClient(token="t") as client:
        result = await client.commits("o", "n", "src/app.py", 3)
    assert len(result) == 3
    assert route.calls.last.request.url.params["path"] == "src/app.py"


def test_ca_bundle_prefers_ssl_cert_file(tmp_path, monkeypatch):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert ca_bundle_path() == str(bundle)


def test_ca_bundle_falls_back_to_requests_ca_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    assert ca_bundle_path() == str(bundle)


def test_ca_bundle_ignores_a_path_that_does_not_exist(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/nope/missing.pem")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert ca_bundle_path() is None
    assert default_verify() is True


def test_default_verify_builds_a_context_from_a_real_bundle(monkeypatch):
    import ssl as _ssl

    monkeypatch.setenv("SSL_CERT_FILE", _ssl.get_default_verify_paths().cafile or "")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    result = default_verify()
    assert isinstance(result, _ssl.SSLContext) or result is True


def test_default_verify_with_nothing_set(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert default_verify() is True
