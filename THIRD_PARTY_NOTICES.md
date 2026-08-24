# Third-party notices

## Playwright

The public project pins `playwright==1.60.0`. Playwright is distributed by
Microsoft under Apache-2.0. Playwright's own notice also identifies code
derived from Puppeteer and its Apache-2.0 licensing.

The exact installed package, browser downloads, and transitive dependencies
must be inventoried for the release being published. This project does not
bundle or redistribute a Chrome/Chromium binary.

## Browser executable

The user supplies a local Chrome/Chromium executable. Its source, build,
license, update channel, and redistribution rights are outside this repository
and must be checked by the user before redistribution.

## Release gate

Do not publish this file as a complete legal inventory until the exact wheel,
browser package, transitive dependency, and externally supplied executable
sources have been recorded.
