# Contributing

## Before changing code

Read `README.md`, `AGENTS.md`, `SECURITY.md`, and `LIMITATIONS.md`. Keep
changes narrow and preserve the local-only trust model.

## Tests

Use only fresh temporary data directories and local fixture pages. Ordinary
CI must not access real websites, real proxies, personal accounts, or existing
browser profiles.

Tests must not commit generated screenshots, downloads, logs, registries,
Profile directories, or secret-store files.

## Pull requests

Describe the user-visible scope, security impact, test scope, and known
limitations. Do not claim hidden-window stability, real-site compatibility,
proxy exit separation, or account-session compatibility without a separately
reviewed and reproducible evidence package.

Contributors must confirm that they have the right to submit their changes
under the repository license.
