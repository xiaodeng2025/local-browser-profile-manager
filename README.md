# Local Browser Profile Manager

An AI-agent-friendly local manager for manually operated Chromium browser profiles.
Each profile has an independent `user-data-dir`, lifecycle, pages, optional fixed
proxy route, and local management page.

**Status: Public-R1 candidate · local review branch**

This release candidate keeps the v0.1 local, manual-operation foundation: run it
locally, keep browser profiles isolated, and open and operate them yourself. It is
not an unattended browser runtime, autonomous Agent system, remote service, task
queue, or long-term automation platform.

## Scope

This is a local, manual v0.1 foundation. It is not a remote browser service,
multi-tenant control plane, account farm, fingerprint-spoofing product, proxy
pool, task queue, or autonomous website agent.

The service binds to `127.0.0.1` and requires the user to provide a local
Chrome/Chromium executable. Browser binaries and profile data are not bundled.

Public-R1 adds two narrowly scoped local capabilities:

- a stopped Profile may use one fixed HTTP or HTTPS proxy with Basic
  authentication. Credentials remain in the existing local Windows-protected
  secret store and are supplied at persistent browser launch; they are not put
  in browser command-line arguments or returned by the management API;
- every Profile receives one persisted fixed fingerprint seed. The management
  page exposes only whether the configuration is fixed and whether the browser
  executable version has been locked. It never returns the seed or executable
  digest.

The fixed seed is only a stable launch input for a compatible browser build. It
does not prove that a browser consumes the argument, that a target website will
allow the Profile, or that the Profile is resistant to detection. It also does
not provide geographic or network isolation.

The v0.1 startup and data-safety path also provides:

- manual Profiles start as ordinary visible Chromium processes by default;
  the service does not attach over CDP or expose Playwright pages for that
  runtime. Automation requests fail with `automation_not_attached`;
- Chromium's native download preferences are set per Profile. Completed
  downloads are accepted only when Chromium reports a final file inside that
  Profile's own download directory;
- the default product ports accept only the canonical `profile-data` root with
  its reviewed non-sensitive marker. Random ports remain available for
  isolated local tests.

## Important limitations

- Profile data may contain cookies, login sessions, saved passwords, history,
  local storage, downloads, and other sensitive browser state.
- The local API has no user authentication. Treat the machine and local user
  account as the trust boundary.
- Hidden-window operation is experimental and disabled by default. No guarantee
  is made that a headed browser will never take foreground focus.
- Real-site compatibility, target-site acceptance, proxy exit separation,
  geographic routing, and authenticated proxy operation are not guaranteed.
- SOCKS5 username/password authentication is not supported.
- A browser executable change after the first fingerprint-enabled start is
  rejected until the local Profile state is deliberately migrated; Public-R1
  does not provide an automatic migration workflow.
- Native manual startup intentionally has no Playwright page automation or CDP
  attachment; it is a user-operated browser mode.

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
