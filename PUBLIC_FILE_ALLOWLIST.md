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

Never copy:

- `.git/` or private history;
- `validation/` output or `VALIDATION_REPORT.md`;
- any browser profile, registry, log, screenshot, download, secret store,
  configuration, environment file, or browser binary;
- real-site, proxy, fingerprint, Camoufox, hidden-window, focus, or desktop
  coexistence evidence.

This allowlist is a release-control document, not proof that the source has
already passed a legal, security, or reproducibility review.
