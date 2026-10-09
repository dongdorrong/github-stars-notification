# Project registry and Kubernetes classifier

`config/projects.yaml` is the versioned `project-registry/v1` policy. It is
written in JSON-compatible YAML so stdlib JSON loading works in token-free
tests; conventional YAML is accepted when PyYAML is installed. The companion
contract is `schemas/project-registry.schema.json`. Runtime validation in
`starwatch.registry.validate_registry` mirrors the closed shape and fails
before collection if a field or enum is invalid. No invalid policy is silently
replaced with defaults.

## Policy and precedence

Keys are canonical lower-case `owner/repo`. `aliases` map moved names to one
policy row and may not collide. Each row explicitly records `enabled`, `tier`,
an explicit classification, category list, package mapping, per-signal booleans,
route floors, and optional configured announcement source allowlists.
`visibility`, when specified, is one of `public/private/internal/unknown`;
source inventory visibility remains authoritative for delivery/export. The
bundled registry has ten initial relevant policies; announcement sources are
all **off** until explicitly configured. Do not enable an RSS source merely
because a URL appears in README or a Release note.

Classification precedence is:

1. explicit policy (including `enabled: false` or `tier: ignore`),
2. known Kubernetes ecosystem owner,
3. exact known topic,
4. repository-name token,
5. description token,
6. `AMBIGUOUS` for insufficient evidence.

An informative description with no positive evidence yields
`NOT_KUBERNETES`. Name/description checks use word or hyphen-token boundaries,
not arbitrary substrings (`notkubernetes` is not positive). Only ambiguous
results may be offered to optional AI advisory classification. AI must never
change a registry decision, project tier, or enabled signal.

`classification_report` emits only counts: starred total, visibility,
explicit policies, confirmed/non-Kubernetes/ambiguous, tier, category and
ignored totals. It does not include names, descriptions, URLs or source bodies.
Missing visibility is counted as `unknown` unless `private: true`; public
export must independently fail closed on `unknown`.

## Legacy compatibility

Pass existing `config.yaml` `special_projects` to
`load_registry(path, legacy_special_projects=...)`. Registry policy wins, but a
listed special project **must** have enabled Release collection and
`routing.release_floor: high`; otherwise loading fails with a deterministic
conflict rather than silently dropping immediate notification. A legacy-only
project is translated in memory to a high-tier, immediate-Release policy, with
all new signal types disabled. This is a one-period read compatibility layer;
it does not rewrite or delete `config.yaml`, event DB, or legacy cache.

The initial eight legacy special projects have an explicit `high` Release
floor. The two additional explicit registry entries use a `digest` Release
floor. Advisory floors are `critical` for critical-tier policies and `high`
otherwise; downstream deterministic severity logic may raise them further.

## Safe editing and validation

After changing policy, run:

```bash
python3 -m unittest discover -s tests -p test_registry.py -v
python3 -m unittest discover -s tests -v
git diff --check
```

For a moved repository, update the canonical key only when ownership is
verified and add the old name to `aliases`. Never duplicate a policy row.
Changes to categories, signal names, source shapes or tiers require a schema
version and validator update together. Public Actions logs should print only
aggregate report values or an irreversible repository reference.
