"""Minimal local Profile Manager backend entrypoint.

It exposes the existing loopback ``LocalProfileAPI`` and delegates every
browser/Profile lifecycle decision to the existing ``ProfileManager``.  This
is intentionally backend-only: no GUI, remote listener, authentication,
task queue, Site Adapter, or browser-core work is included.

Hidden window operation is disabled by default.  It remains experimental while
P0-1 foreground-focus stability is PENDING; this service never promises that a
headed browser cannot take foreground focus.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from playwright.async_api import async_playwright

from .api import LocalProfileAPI
from .local_management_ui import UISettingsStore, start_management_ui_server
from .permission_manager import PermissionManager
from .profile_manager import ProfileManager
from .proxy_secret_store import ProxyCredentialStore


READY_HTML = b"<!doctype html><title>Local Profile Manager Ready</title><main>ready</main>"


async def _http_json(host: str, port: int, method: str, path: str) -> tuple[int, dict[str, Any]]:
    """Query the local API for the non-browser startup check only."""
    def call() -> tuple[int, dict[str, Any]]:
        connection = HTTPConnection(host, port, timeout=45)
        try:
            connection.request(method, path, headers={"Connection": "close"})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    return await asyncio.to_thread(call)


class ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(READY_HTML)))
        self.end_headers()
        self.wfile.write(READY_HTML)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def start_ready_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadyHandler)
    Thread(target=server.serve_forever, name="profile-manager-ready-page", daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = Path(args.data_dir).resolve()
    chrome = Path(args.chrome).resolve()
    if not chrome.is_file():
        raise FileNotFoundError(f"chrome_not_found:{chrome}")
    data_dir.mkdir(parents=True, exist_ok=True)
    ready_server, ready_url = start_ready_server()
    api: LocalProfileAPI | None = None
    ui_server: ThreadingHTTPServer | None = None
    try:
        async with async_playwright() as playwright:
            proxy_credentials = ProxyCredentialStore(data_dir / "proxy-secrets.json")
            manager = ProfileManager(
                playwright,
                registry_path=data_dir / "registry.json",
                profiles_root=data_dir / "profiles",
                browser_executable=chrome,
                ready_url=ready_url,
                ready_title="Local Profile Manager Ready",
                soft_concurrency_limit=args.soft_concurrency_limit,
                max_retries=2,
                background_mode=True,
                permission_manager=PermissionManager(),
                proxy_credentials=proxy_credentials,
            )
            # Presentation preferences intentionally live outside registry.json.
            # Bootstrap creates only stopped records and only on the first
            # ever empty Registry; it never launches a browser.
            settings = UISettingsStore(data_dir / "ui-settings.json")
            settings.bootstrap_presets(manager.create, manager.list())
            # Bind the UI first so its exact ephemeral Origin is known before
            # the action API accepts a single cross-origin request.
            api_base_ref = {"value": ""}
            ui_server, _ui_thread, ui_url = start_management_ui_server(
                static_dir=Path(__file__).resolve().parent / "ui",
                settings=settings,
                records=manager.list,
                api_base_provider=lambda: api_base_ref["value"],
                port=args.ui_port,
            )
            ui_origin = ui_url.rstrip("/")
            api = LocalProfileAPI(
                manager,
                screenshot_root=data_dir / "screenshots",
                log_path=data_dir / "service-events.jsonl",
                allow_hidden_window_mode=args.enable_experimental_hidden,
                # Foreground-focus is deliberately not part of this minimal
                # product backend.  It is not enabled by any CLI option.
                allow_window_focus=False,
                # Only this exact local management page can cross-origin call
                # the API. Foreign Origin is rejected before dispatch.
                allowed_cors_origins={ui_origin},
                proxy_credentials=proxy_credentials,
            )
            host, port = await api.start(port=args.port)
            api_base_ref["value"] = f"http://{host}:{port}"
            startup = {
                "service_status": "started",
                "host": host,
                "port": port,
                "data_dir": str(data_dir),
                "browser_executable": str(chrome),
                "background_startup": "existing_formal_chain",
                "hidden_mode": "experimental_enabled" if args.enable_experimental_hidden else "disabled_by_default_p0_1_pending",
                "window_focus": "disabled",
                "ui": {
                    "url": ui_url,
                    "host": "127.0.0.1",
                    "port": ui_server.server_port,
                    "mode": "manual_only",
                    "hidden": "disabled_p0_1_pending",
                    "focus": "disabled",
                },
                "preset_profiles": 3,
            }
            print(json.dumps(startup, ensure_ascii=False), flush=True)
            if args.startup_check:
                status, health = await _http_json(host, port, "GET", "/api/health")
                result = {
                    **startup,
                    "startup_check": {
                        "status": status,
                        "health": health,
                        "profile_started": False,
                        "passed": status == 200 and health.get("window_mode", {}).get("hidden_enabled") is False and health.get("window_mode", {}).get("focus_enabled") is False,
                    },
                }
                (data_dir / "startup-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            # A long-running local service ends only with console interruption.
            await asyncio.Event().wait()
    finally:
        if ui_server is not None:
            ui_server.shutdown()
            ui_server.server_close()
        if api is not None:
            await api.close()
        ready_server.shutdown()
        ready_server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, help="Path to the local Chrome/Chromium executable")
    parser.add_argument("--data-dir", required=True, help="Persistent service data root; profiles and registry live below it")
    parser.add_argument("--port", type=int, default=17321, help="Loopback-only listening port (0 chooses an available port)")
    parser.add_argument("--ui-port", type=int, default=17322, help="Loopback-only Chinese management page port (0 chooses an available port)")
    parser.add_argument("--soft-concurrency-limit", type=int, default=24)
    parser.add_argument("--enable-experimental-hidden", action="store_true", help="Enable hidden window endpoint; P0-1 remains PENDING")
    parser.add_argument("--startup-check", action="store_true", help="Bind loopback API, query health, then stop without starting a browser Profile")
    args = parser.parse_args()
    if not 1 <= args.soft_concurrency_limit:
        raise ValueError("soft_concurrency_limit_must_be_positive")
    result = asyncio.run(run(args))
    if args.startup_check:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
