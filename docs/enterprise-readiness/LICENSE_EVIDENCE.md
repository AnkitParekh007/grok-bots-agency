# Third-party license evidence

The owner selected MIT for this repository. That decision does not choose licenses or approve terms for Python dependencies, bundled native software, model weights, source data or hosted services. ER-23 still requires those decisions and operational owners.

`scripts/license_inventory.py` collects review material from the installed packages selected by one exact dependency lock on the current platform. It verifies each observed package name/version against the applicable lock entries and records the lock SHA-256. It retains declared license expressions, legacy license text, license classifiers, declared notice filenames, and bounded copies/hashes of observed packaged notices. It includes declared filenames even when they are not named LICENSE, and notices observed in bundled package directories, such as the Playwright driver. No model, source-system, browser or license-server call is made; no terms are accepted.

The report deliberately says `owner_review=NOT_REVIEWED`, `release_eligible=false`, and `redistribution_approved=false` for every package. A declaration is `DECLARED_UNVERIFIED`, not an automatic approval. Missing/UNKNOWN declarations, missing packaged notices and unresolved declared files stay explicit. Unresolved declared files make the CLI/CI step fail; a missing declaration is recorded for owner review rather than replaced with the project's MIT license.

## Reproduce and inspect

Run each collector with the interpreter whose installed environment is being inventoried. An advisory scanner resolving a different lock is not evidence of what is installed in its own environment.

```powershell
.\.venv\Scripts\python.exe scripts/license_inventory.py requirements.lock reports/license-review/runtime
.\.venv-security\Scripts\python.exe scripts/license_inventory.py requirements-security.lock reports/license-review/tooling
```

Output must be a new directory. Existing reviews are never overwritten. Each output contains `inventory.json` and `notices/` copies with relative source/output paths, observed byte counts and SHA-256 values. No absolute home-directory paths, environment secrets or package author/contact metadata are collected. Published license notices can themselves contain their authors' attribution, which is preserved unchanged.

Collection bounds are 256 applicable distributions, 50,000 recorded paths per distribution, 128 notice files per distribution, 1 MB per file, 20 MB combined notice bytes and 2 MB for the JSON manifest. Unsafe/traversing/device/case-aliased paths, missing selected files, empty/oversized evidence and installed-version drift fail collection. Distribution metadata is interpreted by the installed Python metadata library; these output/file limits do not sandbox a malicious Python environment or prove package integrity.

CI runs runtime collection in the application/test environment and tooling collection in its separately locked environment. Runtime evidence has a dedicated `runtime-license-evidence` artifact and is also in test reports. Tooling evidence accompanies the supply-chain reports. All have the existing 14-day artifact retention. The prepared default-branch provenance bundle downloads both sets after the functional and supply-chain jobs pass; feature-branch signing is skipped. Its signing/verification path remains unqualified until actually exercised on the trusted branch.

## Observed coverage and remaining decisions

Local Windows collection observed **37 application/test packages with 89 notice files**, and **33 tooling packages with 91 notice files**. All declared files were located and no declaration was absent/UNKNOWN in these inventories. The application count differs from Linux because the lock has a Windows-only colorama entry. These are observations about those exact environments, not approval of any license expression or redistribution.

The first tooling collection exposed three legacy declared filenames that the initial filename heuristic had missed. Selection now also follows the explicit declarations. A test then caught an uppercase path alias evading candidate selection; case-insensitive discovery and ambiguity rejection fixed it. The expanded license/supply-chain selection passed 33 tests. Both corrected local inventories located their declared files.

Checkpoint `6176c4d` also passed [Linux CI](https://github.com/AnkitParekh007/grok-bots-agency/actions/runs/39044124105): 666 unit and 61 integration tests, both license collectors and the remaining functional/security gates. Its runtime inventory contained 36 packages / 88 notices and tooling contained 33 packages / 91 notices. Both reported complete declared files and no unknown declarations; all owner decisions remained NOT_REVIEWED and release eligibility false.

Owners must review the actual notices and obligations for the intended distribution and operating model, choose any license alternatives themselves, determine attribution/source-offer requirements where relevant, and record an approval reference. This collector does not infer obligations from an expression or certify that a wheel's notices are exhaustive or correct. Hashes record observed contents; they are not a separate legal or authenticity decision.

Python metadata does not complete the native inventory. Chromium/browser dependencies, OS libraries, Node/Playwright native components, cryptographic native libraries, model weights/digests/chat templates, datasets, Jira/Bitbucket/MCP services and hosting/data agreements require their own versioned evidence and owner decisions. Model-data approvals, retention/training/region choices and domain oracle independence remain separate gates. Support, incident/on-call responsibilities and release sign-off remain open in the [implementation ledger](IMPLEMENTATION_STATUS.md).
