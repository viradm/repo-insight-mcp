import pytest

from repo_insight_mcp.dependencies import parse_manifest


def names(deps):
    return {d.name for d in deps}


def test_requirements_txt_drops_comments_flags_and_markers():
    content = """
# a comment
httpx>=0.27
pydantic == 2.8.0
-r other.txt
uvicorn ; python_version >= "3.11"

    """
    deps = parse_manifest("requirements.txt", content)
    assert names(deps) == {"httpx", "pydantic", "uvicorn"}
    by_name = {d.name: d for d in deps}
    assert by_name["httpx"].version == ">=0.27"
    assert by_name["uvicorn"].version is None
    assert all(d.ecosystem == "pypi" for d in deps)


def test_pyproject_pep621_and_optional_groups():
    content = """
[project]
name = "x"
dependencies = ["httpx>=0.27", "mcp"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
"""
    deps = parse_manifest("pyproject.toml", content)
    assert names(deps) == {"httpx", "mcp", "pytest"}
    assert {d.name: d.scope for d in deps}["pytest"] == "dev"


def test_pyproject_poetry_skips_python_itself():
    content = """
[tool.poetry.dependencies]
python = "^3.11"
requests = "^2.31"
django = { version = "5.0", extras = ["bcrypt"] }
"""
    deps = parse_manifest("pyproject.toml", content)
    assert names(deps) == {"requests", "django"}
    assert {d.name: d.version for d in deps}["django"] == "5.0"


def test_package_json_separates_dev_dependencies():
    content = """
{"dependencies": {"express": "^4.19.2"},
 "devDependencies": {"jest": "^29.7.0"}}
"""
    deps = parse_manifest("package.json", content)
    assert {d.name: d.scope for d in deps} == {"express": "runtime", "jest": "dev"}
    assert all(d.ecosystem == "npm" for d in deps)


def test_pom_xml_with_namespace_builds_group_artifact_coordinates():
    content = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>5.3.39</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""
    deps = parse_manifest("pom.xml", content)
    by_name = {d.name: d for d in deps}
    assert by_name["org.springframework:spring-core"].version == "5.3.39"
    assert by_name["junit:junit"].scope == "test"


def test_pom_xml_without_namespace():
    content = """<project>
  <dependencies>
    <dependency><groupId>g</groupId><artifactId>a</artifactId><version>1</version></dependency>
  </dependencies>
</project>"""
    assert names(parse_manifest("pom.xml", content)) == {"g:a"}


def test_go_mod_handles_block_and_single_line_requires():
    content = """
module example.com/x

go 1.22

require github.com/stretchr/testify v1.9.0

require (
    golang.org/x/text v0.16.0 // indirect
    github.com/google/uuid v1.6.0
)
"""
    deps = parse_manifest("go.mod", content)
    assert names(deps) == {
        "github.com/stretchr/testify",
        "golang.org/x/text",
        "github.com/google/uuid",
    }


def test_gemfile_with_and_without_versions():
    content = """
source 'https://rubygems.org'
gem 'rails', '7.1.3'
gem 'puma'
# gem 'commented-out'
"""
    deps = parse_manifest("Gemfile", content)
    assert {d.name: d.version for d in deps} == {"rails": "7.1.3", "puma": None}


def test_nested_path_still_resolves_by_basename():
    deps = parse_manifest("services/api/requirements.txt", "httpx")
    assert deps[0].source_file == "services/api/requirements.txt"


def test_unknown_manifest_returns_empty_rather_than_raising():
    assert parse_manifest("Makefile", "all:\n\techo hi") == []


def test_malformed_manifest_raises_with_the_filename():
    with pytest.raises(ValueError, match="package.json"):
        parse_manifest("package.json", "{not json")
