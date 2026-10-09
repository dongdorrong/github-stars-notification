"""Versioned, deterministic project policy and Kubernetes classification.

The bundled registry is JSON (a YAML 1.2 subset), so validation and Actions do
not depend on an additional schema package. Ordinary YAML is accepted when
PyYAML is installed. Public reports contain counts, never repository names.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "project-registry/v1"
RESULTS = frozenset({"CONFIRMED_KUBERNETES", "NOT_KUBERNETES", "AMBIGUOUS"})
TIERS = frozenset({"critical", "high", "standard", "ignore"})
CATEGORIES = frozenset({
    "kubernetes", "gitops", "autoscaling", "networking", "storage",
    "observability", "security", "service-mesh", "runtime", "package-management",
})
SIGNALS = frozenset({"release", "prerelease", "advisory", "announcement", "discussions", "issues", "rss"})
VISIBILITIES = frozenset({"public", "private", "internal", "unknown"})
OWNERS = frozenset({
    "kubernetes", "kubernetes-sigs", "cncf", "etcd-io", "argoproj", "cilium",
    "istio", "prometheus", "prometheus-operator", "helm", "containerd", "cri-o",
})
TERMS = frozenset({
    "kubernetes", "k8s", "cloud-native", "cncf", "operator", "cni", "csi",
    "gitops", "service-mesh", "container-runtime", "helm", "observability",
})
_REPO = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$")
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _has_signal(text: str) -> bool:
    words = _WORD.findall(text.lower())
    exact = set(words)
    components = {part for word in words for part in word.split("-")}
    return any(term in exact or term in components for term in TERMS)


def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("repository name must be a string")  # noqa: TRY004 - uniform config validation error
    normalized = re.sub(r"\s*/\s*", "/", name.strip().lower())
    if not _REPO.fullmatch(normalized):
        raise ValueError("repository name must be owner/repo")
    return normalized


def _mapping(value: Any, path: str, allowed: frozenset[str], required: frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{path} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        raise ValueError(f"{path} has unknown or missing fields")
    return value


def _strings(value: Any, path: str, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f"{path} must be a string list")
    if allowed is not None and any(v not in allowed for v in value):
        raise ValueError(f"{path} contains an unsupported value")
    if len(value) != len(set(value)):
        raise ValueError(f"{path} contains duplicate values")
    return tuple(value)


@dataclass(frozen=True)
class ProjectPolicy:
    canonical_name: str
    enabled: bool
    tier: str
    classification: str
    categories: tuple[str, ...]
    aliases: tuple[str, ...]
    packages: tuple[dict[str, str], ...]
    signals: dict[str, bool]
    routing: dict[str, str]
    sources: dict[str, Any]
    visibility: str | None = None
    legacy_special: bool = False

    @property
    def immediate_release(self) -> bool:
        return self.enabled and self.signals["release"] and self.routing["release_floor"] == "high"


@dataclass(frozen=True)
class Classification:
    result: str
    source: str
    project: str | None
    tier: str
    categories: tuple[str, ...]
    ignored: bool = False


class ProjectRegistry:
    def __init__(self, projects: Mapping[str, ProjectPolicy], aliases: Mapping[str, str], legacy_special_projects: Iterable[str] = ()):
        self.projects = dict(projects)
        self.aliases = dict(aliases)
        self.legacy_special_projects = frozenset(legacy_special_projects)

    def canonical_name(self, name: str) -> str:
        normalized = normalize_name(name)
        return self.aliases.get(normalized, normalized)

    def resolve(self, name: str) -> ProjectPolicy | None:
        return self.projects.get(self.canonical_name(name))

    def classify(self, item: Mapping[str, Any] | str) -> Classification:
        if isinstance(item, str):
            item = {"full_name": item}
        name = item.get("full_name") or item.get("repository") or item.get("name")
        if not isinstance(name, str):
            raise ValueError("inventory row lacks full_name")  # noqa: TRY004 - uniform input validation error
        canonical = self.canonical_name(name)
        policy = self.projects.get(canonical)
        if policy is not None:
            if not policy.enabled or policy.tier == "ignore":
                return Classification("NOT_KUBERNETES", "explicit_ignore", canonical, "ignore", policy.categories, True)
            return Classification(policy.classification, "explicit", canonical, policy.tier, policy.categories)
        owner, repo = canonical.split("/", 1)
        if owner in OWNERS:
            return Classification("CONFIRMED_KUBERNETES", "owner", canonical, "standard", ("kubernetes",))
        topics = item.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        if any(isinstance(t, str) and t.strip().lower() in TERMS for t in topics):
            return Classification("CONFIRMED_KUBERNETES", "topic", canonical, "standard", ("kubernetes",))
        if _has_signal(repo):
            return Classification("CONFIRMED_KUBERNETES", "name", canonical, "standard", ("kubernetes",))
        description = item.get("description") or ""
        if isinstance(description, str):
            if _has_signal(description):
                return Classification("CONFIRMED_KUBERNETES", "description", canonical, "standard", ("kubernetes",))
            if description.strip():
                return Classification("NOT_KUBERNETES", "no_signal", canonical, "standard", ())
        return Classification("AMBIGUOUS", "insufficient_evidence", canonical, "standard", ())

    def classification_report(self, items: Iterable[Mapping[str, Any] | str]) -> dict[str, Any]:
        """Only aggregate metrics; no repository names or source text escape."""
        rows = list(items)
        visibility: Counter[str] = Counter()
        results: Counter[str] = Counter()
        tiers: Counter[str] = Counter()
        categories: Counter[str] = Counter()
        explicit = ignored = 0
        for row in rows:
            metadata = {"full_name": row} if isinstance(row, str) else row
            value = metadata.get("visibility")
            if not isinstance(value, str) or value not in VISIBILITIES:
                value = "private" if metadata.get("private") is True else "unknown"
            visibility[value] += 1
            classified = self.classify(metadata)
            results[classified.result] += 1
            tiers[classified.tier] += 1
            categories.update(classified.categories)
            explicit += classified.source in {"explicit", "explicit_ignore"}
            ignored += classified.ignored
        return {
            "total_starred_repositories": len(rows),
            "visibility_counts": {v: visibility[v] for v in sorted(VISIBILITIES)},
            "explicit_registry_count": explicit,
            "confirmed_kubernetes_count": results["CONFIRMED_KUBERNETES"],
            "non_kubernetes_count": results["NOT_KUBERNETES"],
            "ambiguous_count": results["AMBIGUOUS"],
            "tier_counts": {v: tiers[v] for v in sorted(TIERS)},
            "category_counts": {v: categories[v] for v in sorted(CATEGORIES)},
            "ignored_count": ignored,
        }


def validate_registry(data: Any) -> dict[str, ProjectPolicy]:
    root = _mapping(data, "registry", frozenset({"schema_version", "projects"}), frozenset({"schema_version", "projects"}))
    if root["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported project registry schema version")
    raw_projects = root["projects"]
    if not isinstance(raw_projects, dict):
        raise ValueError("projects must be a mapping")  # noqa: TRY004 - uniform config validation error
    projects: dict[str, ProjectPolicy] = {}
    for raw_name, raw_policy in raw_projects.items():
        name = normalize_name(raw_name)
        if name != raw_name or name in projects:
            raise ValueError("project key must be canonical lower-case owner/repo")
        # A configured key may identify a private repository. Validation errors
        # are safe to print in Actions and must not echo it.
        path = "projects[*]"
        obj = _mapping(raw_policy, path, frozenset({"enabled", "tier", "classification", "categories", "aliases", "packages", "signals", "routing", "sources", "visibility"}), frozenset({"enabled", "tier", "classification", "categories", "signals", "routing"}))
        if not isinstance(obj["enabled"], bool) or not isinstance(obj["tier"], str) or obj["tier"] not in TIERS:
            raise ValueError(f"{path} has invalid enabled/tier")
        classification = _mapping(obj["classification"], f"{path}.classification", frozenset({"result", "source"}), frozenset({"result", "source"}))
        if not isinstance(classification["result"], str) or classification["result"] not in RESULTS or classification["source"] != "explicit":
            raise ValueError(f"{path} has invalid explicit classification")
        categories = _strings(obj["categories"], f"{path}.categories", CATEGORIES)
        aliases = tuple(normalize_name(v) for v in _strings(obj.get("aliases", []), f"{path}.aliases"))
        packages_raw = obj.get("packages", [])
        if not isinstance(packages_raw, list):
            raise ValueError(f"{path}.packages must be a list")  # noqa: TRY004 - uniform config validation error
        packages: list[dict[str, str]] = []
        for package in packages_raw:
            package = _mapping(package, f"{path}.packages[]", frozenset({"ecosystem", "name"}), frozenset({"ecosystem", "name"}))
            if not all(isinstance(v, str) and v.strip() for v in package.values()):
                raise ValueError(f"{path}.packages[] has invalid value")
            packages.append(dict(package))
        signals = _mapping(obj["signals"], f"{path}.signals", SIGNALS, SIGNALS)
        if any(not isinstance(value, bool) for value in signals.values()):
            raise ValueError(f"{path}.signals values must be booleans")
        routing = _mapping(obj["routing"], f"{path}.routing", frozenset({"release_floor", "advisory_floor"}), frozenset({"release_floor", "advisory_floor"}))
        if routing["release_floor"] not in {"digest", "high"} or routing["advisory_floor"] not in {"high", "critical"}:
            raise ValueError(f"{path}.routing has invalid floor")
        sources = _mapping(obj.get("sources", {}), f"{path}.sources", frozenset({"discussions", "issues", "rss"}))
        for key, source in sources.items():
            if key == "rss":
                if not isinstance(source, list) or any(not isinstance(v, dict) or
                        not {"url", "official"}.issubset(v) or
                        set(v) - {"url", "official", "redirect_hosts", "visibility"} or
                        not isinstance(v["url"], str) or not v["url"].startswith("https://") or
                        bool(urllib.parse.urlsplit(v["url"]).query) or
                        v["official"] is not True or
                        not isinstance(v.get("visibility", "unknown"), str) or
                        v.get("visibility", "unknown") not in VISIBILITIES or
                        not isinstance(v.get("redirect_hosts", []), list) or
                        any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host)
                            for host in v.get("redirect_hosts", [])) for v in source):
                    raise ValueError(f"{path}.sources.rss has invalid source")
            else:
                fields = frozenset({"categories"}) if key == "discussions" else frozenset({"allow_labels", "deny_labels"})
                source = _mapping(source, f"{path}.sources.{key}", fields, fields)
                for values in source.values():
                    _strings(values, f"{path}.sources.{key}")
        visibility = obj.get("visibility")
        if visibility is not None and (not isinstance(visibility, str) or visibility not in VISIBILITIES):
            raise ValueError(f"{path}.visibility invalid")
        projects[name] = ProjectPolicy(name, obj["enabled"], obj["tier"], classification["result"], categories, aliases, tuple(packages), dict(signals), dict(routing), dict(sources), visibility)
    return projects


def load_registry(path: str | Path, legacy_special_projects: Iterable[str] = ()) -> ProjectRegistry:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise ValueError("YAML registry requires PyYAML; bundled JSON-compatible YAML needs no dependency") from exc
        data = yaml.safe_load(raw)
    projects = validate_registry(data)
    aliases: dict[str, str] = {}
    for name, policy in projects.items():
        for alias in policy.aliases:
            if alias == name or alias in projects or alias in aliases:
                raise ValueError("registry alias collides with a project or another alias")
            aliases[alias] = name
    legacy = frozenset(normalize_name(name) for name in legacy_special_projects)
    for special in legacy:
        canonical = aliases.get(special, special)
        policy = projects.get(canonical)
        if policy is None:
            # One-period translation: preserve immediate Release behavior for
            # old user configuration without silently enabling new signals.
            projects[canonical] = ProjectPolicy(canonical, True, "high", "CONFIRMED_KUBERNETES", ("kubernetes",), (), (), {s: s == "release" for s in SIGNALS}, {"release_floor": "high", "advisory_floor": "high"}, {}, None, True)
        elif not policy.immediate_release:
            raise ValueError("legacy special project conflicts with registry release policy")
    return ProjectRegistry(projects, aliases, legacy)
