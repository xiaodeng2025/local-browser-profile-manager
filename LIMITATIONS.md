# v0.1 limitations

The public v0.1 scope is a local manual multi-profile manager.

Not included or guaranteed:

- remote access, LAN exposure, multi-user authentication, or multi-tenancy;
- autonomous tasks, Agent/MCP integration, arbitrary code execution, or script
  injection;
- proxy pools, automatic IP rotation, geographic routing, PAC, DNS, WebRTC,
  or proof that a proxy exit differs from direct routing;
- fingerprint spoofing or identity persistence across arbitrary browser builds;
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
- Directly calling `stop()` on an orphan after Manager restart is not supported
  in v0.1. The lifecycle API may return HTTP 200 while the returned Profile
  remains `status=error` with `profile_processes_remain`; this API semantic is
  deferred and is not part of Fix Unit 1.

Any future expansion requires a new threat model, explicit acceptance criteria,
and separate approval.
