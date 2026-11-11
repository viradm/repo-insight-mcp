"""Parse dependency manifests into one normalised shape.

A migration agent should not have to know that Maven nests coordinates in XML
and npm uses a flat object. It asks for dependencies and gets a list.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from xml.etree import ElementTree

MANIFESTS = (
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "pom.xml",
    "go.mod",
    "Gemfile",
)

_PY_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<spec>[<>=!~^].*)?$"
)
_GO_REQUIRE = re.compile(r"^\s*(?P<name>[^\s]+)\s+(?P<version>v[^\s/]+)")
_GEM = re.compile(
    r"""^\s*gem\s+['"](?P<name>[^'"]+)['"](?:\s*,\s*['"](?P<version>[^'"]+)['"])?"""
)


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    version: str | None
    ecosystem: str
    source_file: str
    scope: str = "runtime"

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def parse_manifest(filename: str, content: str) -> list[Dependency]:
    """Dispatch on filename. Unknown filenames return an empty list rather than
    raising, so a caller can hand us every candidate path it found."""
    base = filename.rsplit("/", 1)[-1]
    parser = {
        "requirements.txt": _parse_requirements,
        "pyproject.toml": _parse_pyproject,
        "package.json": _parse_package_json,
        "pom.xml": _parse_pom,
        "go.mod": _parse_go_mod,
        "Gemfile": _parse_gemfile,
    }.get(base)
    if parser is None:
        return []
    try:
        return parser(content, filename)
    except Exception as exc:  # a malformed manifest is data, not a crash
        raise ValueError(f"could not parse {filename}: {exc}") from exc


def _parse_requirements(content: str, source: str) -> list[Dependency]:
    out: list[Dependency] = []
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        line = line.split(";", 1)[0].strip()  # drop environment markers
        match = _PY_REQUIREMENT.match(line)
        if not match:
            continue
        spec = (match.group("spec") or "").strip() or None
        out.append(Dependency(match.group("name"), spec, "pypi", source))
    return out


def _parse_pyproject(content: str, source: str) -> list[Dependency]:
    data = tomllib.loads(content)
    out: list[Dependency] = []
    for raw in data.get("project", {}).get("dependencies", []) or []:
        out.extend(_parse_requirements(raw, source))
    optional = data.get("project", {}).get("optional-dependencies", {}) or {}
    for group, entries in optional.items():
        for raw in entries:
            for dep in _parse_requirements(raw, source):
                out.append(
                    Dependency(dep.name, dep.version, "pypi", source, scope=group)
                )
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        version = spec if isinstance(spec, str) else spec.get("version")
        out.append(Dependency(name, version, "pypi", source))
    return out


def _parse_package_json(content: str, source: str) -> list[Dependency]:
    data = json.loads(content)
    out: list[Dependency] = []
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev")):
        for name, version in (data.get(key) or {}).items():
            out.append(Dependency(name, version, "npm", source, scope=scope))
    return out


def _parse_pom(content: str, source: str) -> list[Dependency]:
    root = ElementTree.fromstring(content)
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    prefix = "m:" if ns else ""
    out: list[Dependency] = []
    for node in root.iterfind(f".//{prefix}dependency", ns):
        group = node.findtext(f"{prefix}groupId", default="", namespaces=ns)
        artifact = node.findtext(f"{prefix}artifactId", default="", namespaces=ns)
        version = node.findtext(f"{prefix}version", default=None, namespaces=ns)
        scope = node.findtext(f"{prefix}scope", default="runtime", namespaces=ns)
        if not artifact:
            continue
        name = f"{group}:{artifact}" if group else artifact
        out.append(Dependency(name, version, "maven", source, scope=scope or "runtime"))
    return out


def _parse_go_mod(content: str, source: str) -> list[Dependency]:
    out: list[Dependency] = []
    in_block = False
    for raw in content.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        target = line
        if line.startswith("require "):
            target = line[len("require ") :]
        elif not in_block:
            continue
        match = _GO_REQUIRE.match(target)
        if match:
            out.append(
                Dependency(match.group("name"), match.group("version"), "go", source)
            )
    return out


def _parse_gemfile(content: str, source: str) -> list[Dependency]:
    out: list[Dependency] = []
    for raw in content.splitlines():
        match = _GEM.match(raw.split("#", 1)[0])
        if match:
            out.append(
                Dependency(
                    match.group("name"), match.group("version"), "rubygems", source
                )
            )
    return out
