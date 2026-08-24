# Local Browser Profile Manager

An AI-agent-friendly local manager for manually operated Chromium browser profiles.
Each profile has an independent `user-data-dir`, lifecycle, pages, optional fixed
proxy route, and local management page.

**Status: v0.1.0 · maintenance / frozen**

This release is a local, manual-operation foundation: run it locally, keep browser
profiles isolated, and open and operate them yourself. It is not an unattended
browser runtime, autonomous Agent system, remote service, task queue, or long-term
automation platform.

## Scope

This is a local, manual v0.1 foundation. It is not a remote browser service,
multi-tenant control plane, account farm, fingerprint-spoofing product, proxy
pool, task queue, or autonomous website agent.

The service binds to `127.0.0.1` and requires the user to provide a local
Chrome/Chromium executable. Browser binaries and profile data are not bundled.

## Important limitations

- Profile data may contain cookies, login sessions, saved passwords, history,
  local storage, downloads, and other sensitive browser state.
- The local API has no user authentication. Treat the machine and local user
  account as the trust boundary.
- Hidden-window operation is experimental and disabled by default. No guarantee
  is made that a headed browser will never take foreground focus.
- Real-site compatibility, proxy exit separation, geographic routing, and
  authenticated proxy operation are not guaranteed by v0.1.

See [`SECURITY.md`](SECURITY.md), [`LIMITATIONS.md`](LIMITATIONS.md), and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before running it with a
real profile.

## Local development

The public repository will provide a pinned Python dependency set and local-only
tests. The first public acceptance must use a new empty data directory and a
local fixture page; it must not use a personal profile, account, cookie, or
real website.

## License

The project is intended to be distributed under MPL-2.0. See [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE).
