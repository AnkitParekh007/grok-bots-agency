# Dependency and source assurance

The application/test environment and security tools now have separate universal, hashed locks: `requirements.lock` and `requirements-security.lock`. Their inputs remain `requirements.txt` / `requirements-security.in`; CI installs only the locks with exact versions, mandatory SHA-256 checks and binary wheels from the explicit PyPI index. The application/test lock includes pytest and pip; this prototype does not yet have a minimal production runtime image. Crypto is an explicit direct dependency.

`.gitattributes` fixes dependency-file line endings to LF. `config/dependency-locks.json` binds both inputs and outputs, and `scripts/verify_dependency_locks.py` rejects stale receipts. CI pins Python 3.12.15 and action commit SHAs. Local checks also use Python 3.13.14 on Windows. These observations do not certify every Python version, operating system or native browser build.

Execution envelopes now bind the lock hash, observed interpreter/platform and installed dependency versions. Shared startup refuses a mismatch against the lock; dependency drift after review invalidates execution approval. Durable queues also bind the observed dependency environment, preventing a different instance from claiming a job under another package set. Stop all instances before updating packages. File/version metadata does not prove the integrity of loaded code or an immutable deployment; independently protected deployment media and native binaries remain required.

## Gates and reports

The separate supply-chain CI job installs its own locked tooling, audits both complete pinned dependency sets against public advisories without invoking dependency resolution, and generates CycloneDX SBOMs. `verify_sbom.py` requires the exact applicable package/version set, including bootstrap packages; missing, extra, duplicate or changed components fail. A scanner/network failure fails the job. Known vulnerabilities are not ignored automatically, including advisories without fixes. The scan covers Python package advisories, not malicious package behavior or every linked OS/native library. See the [pip-audit security model](https://pypi.org/project/pip-audit/#security-model).

Bandit scans all of `qa_agent` and fails on unsuppressed findings. A timezone validation previously implemented with `assert` now uses an explicit exception, including under `python -O`. Dynamic run-update identifiers have a fixed column allowlist with bound values; session/restore maintenance use constant SQL. Browser context cleanup errors are recorded instead of silently swallowed, and model retry jitter uses SystemRandom.

Five narrow inline suppressions have source comments: the public synthetic mock token, operator-only hardware subprocess import/call, a tri-state score named `pass`, and SQL assembled only from the fixed validated run-column set. They do not authorize arbitrary subprocess input or SQL. Review these whenever their call sites/boundaries change.

`scripts/scan_secrets.py` scans tracked files with detect-secrets in bounded batches, with network verification explicitly disabled. The gate proves it detects a generated invalid canary. New detections fail with file/type/line only; values are not printed. Unused exceptions also fail. The exact exception policy contains detector/path/one-way-digest tuples in three reviewed categories: public revision/content digests, environment-variable references and explicit synthetic fixture values. Its schema permits fixed categories/types and digests, not arbitrary raw credentials. No directory-wide exclusion of tests or historical evidence is used. These 93 existing detections were inspected before proposing the exceptions; repository owners must review the policy during PR review. A pattern/entropy scan cannot prove the absence of secrets or replace rotation after an actual exposure.

Run the local equivalents from the repository root, after installing the security lock into a separate environment:

```powershell
.\.venv\Scripts\python.exe scripts/verify_dependency_locks.py
.\.venv-security\Scripts\python.exe -m pip_audit -r requirements.lock --require-hashes --disable-pip --format cyclonedx-json --output reports/security/runtime-sbom.json --progress-spinner off --timeout 20
.\.venv-security\Scripts\python.exe scripts/verify_sbom.py requirements.lock reports/security/runtime-sbom.json
.\.venv-security\Scripts\python.exe -m pip_audit -r requirements-security.lock --require-hashes --disable-pip --format cyclonedx-json --output reports/security/tooling-sbom.json --progress-spinner off --timeout 20
.\.venv-security\Scripts\python.exe scripts/verify_sbom.py requirements-security.lock reports/security/tooling-sbom.json
.\.venv-security\Scripts\python.exe -m bandit -r qa_agent -f json -o reports/security/bandit.json
.\.venv-security\Scripts\python.exe scripts/scan_secrets.py --self-test
.\.venv-security\Scripts\python.exe scripts/scan_secrets.py
```

Create `reports/security` before tools that require an existing output directory. The secret gate uses tracked working files; stage newly proposed source before scanning it locally. It never sends candidate secrets to a service to test whether they work.

## Updating dependencies

Use the recorded resolver (uv 0.12.5 for this checkpoint), review changed packages/owners/licenses/advisories, and regenerate both universal Python-3.12 locks with `uv pip compile <input> --universal --python-version 3.12 --generate-hashes --default-index https://pypi.org/simple --output-file <lock>`. Keep dependency files in LF form. Refresh the four exact hashes in `config/dependency-locks.json` after reviewing the diff, then review any changed public-digest entries in the secret exception policy. Do not copy unknown secret detections into the policy. [uv locking documentation](https://docs.astral.sh/uv/pip/compile/) describes resolution/update behavior.

Install using `python -m pip --isolated install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r <lock>`. This verifies each selected wheel against its lock; it does not make the package trustworthy solely because it has a hash. Both test and supply-chain jobs must pass before accepting a lock update. OS/Chromium components, model weights and external services require separate inventory and qualification.

## Provenance and promotion

After both jobs pass on a push to the repository's default branch, a separate least-privilege job packages the exact Git source, lock hashes and security receipts, then issues a GitHub build-provenance attestation. Pull requests and this feature branch cannot trigger that signing job. The action and artifact downloader are pinned to verified commit SHAs. The bundle is source/evidence, not a signed production image, release approval or automatic deployment.

Before promoting a future bundle, verify its repository, workflow, default-branch ref and reviewed source commit, for example:

```powershell
gh attestation verify qa-buddy-review-bundle.tar.gz --repo AnkitParekh007/grok-bots-agency --signer-workflow AnkitParekh007/grok-bots-agency/.github/workflows/qa-agent-ci.yml --source-ref refs/heads/main --source-digest <reviewed-commit-sha> --deny-self-hosted-runners
```

This signing path remains unexecuted until a trusted default-branch push passes its gates. Actual attestation verification, branch protection/code-owner policy, immutable production image/OS inventory, binary/model provenance, protected release promotion and operational ownership remain external acceptance work. Source scan success is not an enterprise GO.
