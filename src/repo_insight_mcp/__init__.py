"""repo-insight-mcp: read-only GitHub repository tools over MCP."""

__version__ = "0.1.0"

from .dependencies import Dependency, parse_manifest
from .github import GitHubClient, GitHubError, RateLimited, RepoNotFound, parse_repo

__all__ = [
    "Dependency",
    "GitHubClient",
    "GitHubError",
    "RateLimited",
    "RepoNotFound",
    "parse_manifest",
    "parse_repo",
    "__version__",
]
