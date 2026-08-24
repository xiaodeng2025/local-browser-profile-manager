# Security model

## Trust boundary

The manager is a local Windows application. Its HTTP API binds to loopback and
does not provide user authentication. Loopback binding is not equivalent to
authentication: another process running under the same user or on the same
machine may be able to call the API.

Do not expose the service through LAN forwarding, reverse proxies, tunnels, or
port forwarding.

## Sensitive data

Profile directories can contain cookies, login sessions, saved passwords,
history, local storage, IndexedDB, downloads, extensions, and site content.
Keep all runtime data outside the repository. Never attach it to an issue or
pull request.

Proxy credentials, if enabled, must remain in the platform-protected secret
store. They must never be placed in registry records, URLs, logs, screenshots,
test fixtures, or source code.

## Browser capabilities

The local API can navigate a profile to user-supplied HTTP(S) URLs, read
structured page results, upload a user-supplied local file, download content,
and capture screenshots. These operations are intentionally local-user
capabilities, not a multi-user security boundary.

Only use a disposable profile for untrusted sites or experiments.

## Reporting

Report security issues privately to the repository maintainer. Do not include
cookies, credentials, profile databases, screenshots containing personal data,
or service logs in the initial report. Include a minimal reproduction that uses
synthetic data and a fresh temporary profile.
