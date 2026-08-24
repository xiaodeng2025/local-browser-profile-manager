# Contribution and agent rules

- Keep the project local-only unless a change explicitly expands the threat
  model and is approved first.
- Never read, print, commit, upload, or package browser profiles, cookies,
  saved passwords, tokens, screenshots, downloads, logs, registries, or proxy
  secrets.
- Use a fresh temporary data directory for tests. Never use a personal browser
  profile or an existing project data directory.
- Do not run real-site, real-account, proxy, hidden-window, focus, or desktop
  automation tests in ordinary CI.
- Preserve the distinction between verified facts, local-only test results,
  historical evidence, and unsupported claims.
- Do not add remote listeners, authentication bypasses, arbitrary code
  execution, proxy rotation, fingerprint spoofing, or autonomous task logic
  without an explicit design and security review.
- Keep generated evidence outside the repository and fail closed if a test
  output path is not new and empty.
