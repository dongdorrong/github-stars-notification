# Python dependencies and PR CI

The supported interpreter is **Python 3.12** (`.python-version`). Both the notifier workflow and secret-free PR CI use the same minor version. A 3.x floating selector is not supported. The notifier installs only the runtime lock; PR CI installs the larger development lock.

| File | Purpose |
| --- | --- |
| `.github/scripts/requirements.in` | Exactly pinned direct runtime dependencies |
| `.github/scripts/requirements.txt` | Full runtime dependency closure with SHA-256 distribution hashes |
| `.github/scripts/requirements-dev.in` | Exactly pinned CI tools plus runtime input |
| `.github/scripts/requirements-dev.txt` | Full CI dependency closure with hashes |

PyGithub is deliberately pinned to **2.2.0**. The Release collector reads PyGithub's private `_rawData` field to avoid a per-Release HTTP detail request. This is a fragile SDK boundary; do not silently upgrade it. Before changing PyGithub, re-run the SDK HTTP-count/normalization regression fixtures and a read-only feature-branch preview. PyYAML is pinned to 6.0.3; transitive dependencies are also fixed by the lock. The lock checker enforces PyGithub 2.2.0 until that explicit compatibility review is completed.

## Install and verify

```bash
python3.12 -m venv .omx/venvs/p1p2
.omx/venvs/p1p2/bin/python -m pip install --require-hashes -r .github/scripts/requirements-dev.txt
.omx/venvs/p1p2/bin/python scripts/check_contracts.py
.omx/venvs/p1p2/bin/python -m py_compile .github/scripts/check_release.py
.omx/venvs/p1p2/bin/python -m compileall -q .github/scripts starwatch scripts
.omx/venvs/p1p2/bin/ruff check --select E4,E7,E9,F .github/scripts starwatch scripts tests
.omx/venvs/p1p2/bin/ruff format --check starwatch/routing.py tests/test_routing.py scripts/check_contracts.py tests/test_ci_contracts.py
.omx/venvs/p1p2/bin/python -m unittest discover -s tests -v
.omx/venvs/p1p2/bin/pip-audit --disable-pip --progress-spinner off -r .github/scripts/requirements.txt
.omx/venvs/p1p2/bin/pip-audit --disable-pip --progress-spinner off -r .github/scripts/requirements-dev.txt
```

The test suite is fixture/token-free and needs no network, Slack, or LLM. Dependency installation and live vulnerability database audit need PyPI/network access; do not confuse an unavailable audit service with a green security scan. `scripts/check_contracts.py` validates the registry against the JSON Schema and the stricter runtime validator, checks both schema documents against Draft 2020-12, verifies hash/direct-pin lock consistency and Python version alignment, and rejects embedded secret-shaped values without printing them. Hashed pip installation checks every installed distribution. The CI workflow has `contents: read`, no production secrets, no commit mode, and no Slack transport.

## Deliberate dependency update

1. Review upstream release notes and advisories; adjust only the intended direct pin in `requirements.in` or `requirements-dev.in`. PyGithub requires an explicit `_rawData` compatibility decision.
2. Under Python 3.12, install the pinned `pip-tools` version from the current development lock in an isolated environment.
3. Regenerate each affected lock in a network-enabled maintenance environment:

   ```bash
   pip-compile --quiet --generate-hashes --resolver=backtracking --strip-extras --allow-unsafe \
     --no-header --no-annotate --no-emit-index-url \
     --output-file .github/scripts/requirements.txt .github/scripts/requirements.in
   pip-compile --quiet --generate-hashes --resolver=backtracking --strip-extras --allow-unsafe \
     --no-header --no-annotate --no-emit-index-url \
     --output-file .github/scripts/requirements-dev.txt .github/scripts/requirements-dev.in
   ```

4. Perform a fresh `--require-hashes` install, run contract tests and `pip-audit` for **both** locks, review the diff and any added transitive package/license, then run the full fixture suite and safe remote preview if runtime behavior changed.

The lock includes all published distribution hashes for each chosen version, so it remains installable across supported Python 3.12 runners while requiring exact versions and approved hashes. The lock is not an offline vendored dependency cache. If the index removes a distribution, the install must fail rather than silently substitute an unpinned build.

## GitHub Actions supply-chain references

PR CI pins `actions/checkout` v4 and `actions/setup-python` v5 to full reviewed commit SHAs recorded in `docs/SECURITY_LAYERING_NOTES.md` / the API evidence. The notifier workflow pins its own checkout/setup/cache references separately. Action tags move; review official action release notes and the resolved Git tag/commit, update all references deliberately, and rerun CI. Do not grant `pull_request_target` privileges or expose notifier secrets to PR code.
