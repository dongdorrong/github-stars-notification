"""PR CI contracts: schema, registry, hashed locks, runtime and secret hygiene."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starwatch.registry import validate_registry  # noqa: E402


REQUIREMENTS = ROOT / ".github" / "scripts"
_PACKAGE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s\\]+)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
_SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(
        rb"https://hooks\.slack\.com/services/[A-Za-z0-9]{8,}/[A-Za-z0-9]{8,}/[A-Za-z0-9]{20,}"
    ),
    re.compile(rb"https?://[^/@\s]+:[^/@\s]+@"),
)


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = _PACKAGE.match(line)
        if not match:
            raise ValueError("direct dependency is not exactly pinned")
        name, version = match.groups()
        name = name.lower().replace("_", "-")
        if name in pins:
            raise ValueError("duplicate direct dependency")
        pins[name] = version
    return pins


def _lock(path: Path) -> dict[str, tuple[str, frozenset[str]]]:
    blocks: dict[str, tuple[str, frozenset[str]]] = {}
    current_name: str | None = None
    current_version = ""
    hashes: set[str] = set()

    def finish() -> None:
        if current_name is not None:
            if not hashes:
                raise ValueError("unhashed locked dependency")
            if current_name in blocks:
                raise ValueError("duplicate locked dependency")
            blocks[current_name] = (current_version, frozenset(hashes))

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PACKAGE.match(line)
        if match:
            finish()
            current_name = match.group(1).lower().replace("_", "-")
            current_version = match.group(2)
            hashes = set()
        elif current_name is None:
            raise ValueError("lock contains non-package directive")
        found = _HASH.search(stripped)
        if found:
            hashes.add(found.group(1))
        elif not match and stripped != "\\":
            raise ValueError("lock contains unrecognized requirement")
    finish()
    return blocks


def validate_locks(root: Path = ROOT) -> None:
    requirements = root / ".github" / "scripts"
    runtime_pins = _pins(requirements / "requirements.in")
    dev_pins = _pins(requirements / "requirements-dev.in")
    runtime_lock = _lock(requirements / "requirements.txt")
    dev_lock = _lock(requirements / "requirements-dev.txt")
    if runtime_pins.get("pygithub") != "2.2.0":
        raise ValueError("PyGithub version changed without compatibility review")
    for name, version in {**runtime_pins, **dev_pins}.items():
        target = dev_lock if name in dev_pins else runtime_lock
        if name not in target or target[name][0] != version:
            raise ValueError("direct dependency lock mismatch")
    for name, version in runtime_pins.items():
        if name not in runtime_lock or runtime_lock[name][0] != version:
            raise ValueError("runtime direct dependency lock mismatch")
    for name, locked in runtime_lock.items():
        if dev_lock.get(name) != locked:
            raise ValueError("runtime and development locks disagree")


def validate_schemas(root: Path = ROOT) -> None:
    schema_dir = root / "schemas"
    registry_schema = json.loads(
        (schema_dir / "project-registry.schema.json").read_text(encoding="utf-8")
    )
    analysis_schema = json.loads(
        (schema_dir / "ai-analysis-v1.json").read_text(encoding="utf-8")
    )
    for path in schema_dir.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(
            json.loads(path.read_text(encoding="utf-8"))
        )
    jsonschema.Draft202012Validator.check_schema(analysis_schema)
    registry = yaml.safe_load(
        (root / "config" / "projects.yaml").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(registry_schema).validate(registry)
    validate_registry(registry)


def validate_python_version(root: Path = ROOT) -> None:
    minor = (root / ".python-version").read_text(encoding="utf-8").strip()
    if minor != "3.12":
        raise ValueError("supported Python minor must be 3.12")
    for name in ("ci.yml", "notify-starred-releases.yml"):
        workflow = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        if "python-version: '3.12'" not in workflow:
            raise ValueError("workflow Python version mismatch")


def validate_no_embedded_secrets(root: Path = ROOT) -> None:
    paths = [
        root / "config.yaml",
        *(root / "config").glob("*.yaml"),
        *(root / "starwatch").glob("*.py"),
        *(root / "scripts").glob("*.py"),
        *(root / ".github" / "scripts").glob("*.py"),
        *(root / ".github" / "workflows").glob("*.yml"),
    ]
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
            raise ValueError("embedded secret-shaped value in source")


def main() -> None:
    validate_locks()
    validate_schemas()
    validate_python_version()
    validate_no_embedded_secrets()
    print("CI contracts valid: schemas, registry, locks, Python, secret shapes")


if __name__ == "__main__":
    main()
