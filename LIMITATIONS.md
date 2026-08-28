# Public-R1 limitations

The Public-R1 scope remains a local manual multi-profile manager.

Not included or guaranteed:

- remote access, LAN exposure, multi-user authentication, or multi-tenancy;
- autonomous tasks, Agent/MCP integration, arbitrary code execution, or script
  injection;
- proxy pools, automatic IP rotation, geographic routing, PAC, DNS, WebRTC,
  or proof that a proxy exit differs from direct routing;
- SOCKS5 username/password authentication. Basic credentials are accepted only
  for a single fixed HTTP or HTTPS proxy;
- proof that an authenticated proxy works with a particular provider, browser
  build, network, or target website;
- fingerprint spoofing, anti-detection, target-site acceptance, or identity
  persistence across arbitrary browser builds. A fixed seed is only supplied
  as a stable launch input to a compatible browser executable;
- automatic migration after the locked browser executable changes. The start
  fails closed on an executable digest mismatch, and the seed or digest is not
  returned by the management API or displayed by the management page;
- real-site business workflows, CAPTCHA handling, or login compatibility across
  all platforms;
- background focus stability. Hidden mode remains experimental and disabled by
  default.
- If Manager exits while a Profile's Chrome remains alive, that Chrome is an
  orphan from the next Manager's perspective. The next Manager does not
  automatically reconnect to CDP, take ownership, or terminate it; the Profile
  enters `error` with `recovery_required`.
- An orphan must be closed manually. Only after strict `--user-data-dir`
  process detection confirms no matching Chrome remains will Manager reconcile
  the Profile to `stopped` and allow a normal start again.
- A process-probe failure is treated as an unknown/error state, not as proof that
  Chrome is absent. Affected lifecycle or reconciliation decisions remain
  fail-safe until a later probe succeeds.
- Directly calling `stop()` on an orphan after Manager restart is not supported
  in v0.1. The lifecycle API may return HTTP 200 while the returned Profile
  remains `status=error` with `profile_processes_remain`; this API semantic is
  deferred and is not part of Fix Unit 1.

Any future expansion requires a new threat model, explicit acceptance criteria,
and separate approval.
