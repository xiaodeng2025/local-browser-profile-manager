# Public file allowlist

The future repository must be assembled from an explicit allowlist. The
current private repository is not a release bundle.

Allowed source categories:

- `browser_manager/**`
- `tests/unit/**`
- `tests/integration/local_only/**`
- public documentation and license/notice files
- pinned dependency metadata
- CI configuration that performs local-only checks

Public-R1 explicitly permits hand-audited implementation changes in:

- `browser_manager/network_config.py`
- `browser_manager/profile_manager.py`
- `browser_manager/fingerprint_config.py`
- `browser_manager/ui/app.js`
- `browser_manager/ui/index.html`
- local-only unit tests under `tests/unit/**`
- `README.md`, `LIMITATIONS.md`, `PROJECT_STATE.md`, and this allowlist

These files must be committed on this repository's own history. Private commits
must not be merged, cherry-picked, or pushed. Generic behavior may be manually
rewritten, but proxy endpoints, credentials, Profile state, executable paths,
private documentation, validation evidence, and runtime artifacts remain
excluded.

Never copy:

- `.git/` or private history;
- `validation/` output or `VALIDATION_REPORT.md`;
- any browser profile, registry, log, screenshot, download, secret store,
  configuration, environment file, or browser binary;
- real-site, proxy, fingerprint, Camoufox, hidden-window, focus, or desktop
  coexistence evidence. This exclusion covers evidence and runtime data; it
  does not authorize copying them merely because generic Public-R1 source code
  is allowed above.

This allowlist is a release-control document, not proof that the source has
already passed a legal, security, or reproducibility review.
