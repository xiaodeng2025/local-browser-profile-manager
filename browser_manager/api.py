"""Loopback HTTP API for the local browser profile manager.

The API delegates profile lifecycle decisions to :class:`ProfileManager` and
keeps the local page/action surface separate from validation runners.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .network_config import normalize_network_config
from .downloads import DownloadTrackingError
from .profile_manager import ProfileManager, ProfileManagerError, query_profile_processes


class WindowControlError(RuntimeError):
    pass


class LocatorResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts or []


def profile_window_handles(profile_dir: Path) -> list[int]:
    """Enumerate top-level windows owned by this Profile's exact Chrome PIDs."""
    pids = {int(item["ProcessId"]) for item in query_profile_processes(profile_dir) if str(item.get("ProcessId", "")).isdigit()}
    if not pids:
        return []
    user32 = ctypes.windll.user32
    handles: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_proc
    def callback(hwnd: int, _lparam: int) -> bool:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids and user32.IsWindow(hwnd):
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return handles


def set_profile_windows(profile_dir: Path, state: str, known_handles: list[int] | None = None) -> tuple[list[int], int]:
    if state not in {"hidden", "visible", "minimized"}:
        raise WindowControlError(f"invalid window state: {state}")
    handles = [hwnd for hwnd in (known_handles or []) if ctypes.windll.user32.IsWindow(hwnd)]
    handles = list(dict.fromkeys(handles + profile_window_handles(profile_dir)))
    if not handles:
        raise WindowControlError("window_not_found")
    user32 = ctypes.windll.user32
    # SW_SHOWNOACTIVATE and SW_SHOWMINNOACTIVE deliberately keep ordinary
    # show/background transitions separate from an explicit foreground action.
    command = {"hidden": 0, "visible": 4, "minimized": 7}[state]
    for hwnd in handles:
        user32.ShowWindow(hwnd, command)
        if state == "visible":
            # Keep the observed window visible but behind the user's current
            # foreground window. HWND_BOTTOM plus SWP_NOACTIVATE avoids focus.
            user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
    # Give the desktop a moment to apply the state, then report actual visible count.
    time.sleep(0.1)
    visible = sum(bool(user32.IsWindowVisible(hwnd)) for hwnd in handles if user32.IsWindow(hwnd))
    return handles, visible


def focus_profile_windows(profile_dir: Path, known_handles: list[int] | None = None) -> tuple[list[int], int, bool]:
    handles = [hwnd for hwnd in (known_handles or []) if ctypes.windll.user32.IsWindow(hwnd)]
    handles = list(dict.fromkeys(handles + profile_window_handles(profile_dir)))
    if not handles:
        raise WindowControlError("window_not_found")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    target = handles[0]
    foreground = int(user32.GetForegroundWindow())
    current_thread = int(kernel32.GetCurrentThreadId())
    foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    target_thread = int(user32.GetWindowThreadProcessId(target, None))
    attached_foreground = bool(foreground_thread and foreground_thread != current_thread and user32.AttachThreadInput(current_thread, foreground_thread, True))
    attached_target = bool(target_thread and target_thread != current_thread and user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        user32.ShowWindow(target, 9)  # SW_RESTORE
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
        user32.SetActiveWindow(target)
        user32.SetFocus(target)
    finally:
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.1)
    focused = int(user32.GetForegroundWindow()) == int(target)
    visible = sum(bool(user32.IsWindowVisible(hwnd)) for hwnd in handles if user32.IsWindow(hwnd))
    return handles, visible, focused


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalProfileAPI:
    """Tiny HTTP/1.1 server bound explicitly to loopback.

    Historical callers may enable all verified window actions.
    A product-facing launcher may explicitly disable experimental hidden mode
    and foreground focus without duplicating ProfileManager lifecycle logic.
    """

    def __init__(self, manager: ProfileManager, *, screenshot_root: Path, log_path: Path, allow_hidden_window_mode: bool = True, allow_window_focus: bool = True, allow_loopback_cors: bool = False, allowed_cors_origins: set[str] | None = None, proxy_credentials: Any | None = None) -> None:
        self.manager = manager
        self.screenshot_root = Path(screenshot_root)
        self.download_root = self.screenshot_root.parent / "downloads"
        self.log_path = Path(log_path)
        self.allow_hidden_window_mode = bool(allow_hidden_window_mode)
        self.allow_window_focus = bool(allow_window_focus)
        # The product UI is a separate loopback-only static server.  Keep
        # CORS opt-in so validation callers retain their existing surface.
        self.allow_loopback_cors = bool(allow_loopback_cors)
        self.allowed_cors_origins = None if allowed_cors_origins is None else frozenset(
            self._normalize_loopback_origin(origin) for origin in allowed_cors_origins
        )
        self.proxy_credentials = proxy_credentials
        self.screenshot_root.mkdir(parents=True, exist_ok=True)
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0
        # Page IDs are API-layer handles for one browser run. They are never
        # inferred from URL/title and are discarded when a context restarts.
        self._page_registry: dict[str, dict[str, dict[str, Any]]] = {}
        self._page_next_number: dict[str, int] = {}
        self._default_page_id: dict[str, str] = {}
        self._closed_page_ids: dict[str, set[str]] = {}
        self._window_state: dict[str, str] = {}
        self._window_handles: dict[str, list[int]] = {}
        self._page_locks: dict[str, asyncio.Lock] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._event_next: dict[str, int] = {}
        self._dialogs: dict[str, dict[str, dict[str, Any]]] = {}
        self._dialog_next: dict[str, int] = {}
        self._attached_page_objects: set[int] = set()
        self._profile_id_re = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    async def start(self, *, port: int = 0) -> tuple[str, int]:
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port_must_be_between_0_and_65535")
        self.server = await asyncio.start_server(self._handle_client, self.host, port)
        sock = self.server.sockets[0].getsockname()
        self.port = int(sock[1])
        return self.host, self.port

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self._page_registry.clear()
        self._page_next_number.clear()
        self._default_page_id.clear()
        self._closed_page_ids.clear()
        self._window_state.clear()
        self._window_handles.clear()
        self._page_locks.clear()
        self._events.clear()
        self._event_next.clear()
        self._dialogs.clear()
        self._dialog_next.clear()
        self._attached_page_objects.clear()
        await self.manager.stop_all()

    def _log(self, *, method: str, path: str, profile_id: str | None, action: str, result: str, error: str | None = None, request_id: str | None = None, duration_ms: int | None = None, queued_at: str | None = None, started_at: str | None = None, finished_at: str | None = None) -> None:
        # Deliberately omit request bodies and page data: no Cookie/token/LS values.
        item = {"timestamp": utc_now(), "request": f"{method} {path}", "profile_id": profile_id, "action": action, "result": result}
        if request_id:
            item["request_id"] = request_id
        if duration_ms is not None:
            item["duration_ms"] = duration_ms
        if queued_at:
            item["queued_at"] = queued_at
        if started_at:
            item["started_at"] = started_at
        if finished_at:
            item["finished_at"] = finished_at
        if error:
            item["error"] = error
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(header_bytes) > 32 * 1024:
            raise ValueError("headers_too_large")
        lines = header_bytes[:-4].decode("iso-8859-1").split("\r\n")
        request_line = lines[0].split(" ")
        if len(request_line) != 3:
            raise ValueError("invalid_request_line")
        method, target, _ = request_line
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as exc:
            raise ValueError("invalid_content_length") from exc
        if content_length < 0 or content_length > 256 * 1024:
            raise ValueError("body_too_large")
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=10) if content_length else b""
        return method.upper(), target, headers, body

    @staticmethod
    def _normalize_loopback_origin(origin: str) -> str:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("allowed_cors_origin_must_be_exact_loopback_http_origin")
        return f"http://127.0.0.1:{parsed.port}"

    def _cors_origin(self, origin: str | None) -> str | None:
        if not origin:
            return None
        try:
            normalized = self._normalize_loopback_origin(origin)
        except ValueError:
            return None
        if self.allowed_cors_origins is not None:
            return normalized if normalized in self.allowed_cors_origins else None
        return normalized if self.allow_loopback_cors else None

    def _origin_is_rejected(self, origin: str | None) -> bool:
        """Reject foreign Origin before dispatch when product mode uses an allowlist."""
        return bool(origin and self.allowed_cors_origins is not None and self._cors_origin(origin) is None)

    async def _write_response(self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any], *, origin: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 422: "Unprocessable Entity", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}.get(status, "Error")
        cors = self._cors_origin(origin)
        cors_headers = ""
        if cors:
            cors_headers = (
                f"Access-Control-Allow-Origin: {cors}\r\n"
                "Access-Control-Allow-Methods: GET, POST, PUT, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type\r\n"
                "Access-Control-Max-Age: 300\r\nVary: Origin\r\n"
            )
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(body)}\r\n{cors_headers}Connection: close\r\n\r\n".encode("ascii")
        writer.write(head + body)
        await writer.drain()

    def _error(self, code: str, message: str, profile_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": code, "message": message}
        if profile_id is not None:
            payload["profile_id"] = profile_id
        return payload

    def _page_id_for_object(self, profile_id: str, page: Any) -> str | None:
        for page_id, entry in self._page_registry.get(profile_id, {}).items():
            if entry["page"] is page:
                return page_id
        return None

    def _record_event(self, profile_id: str, event_type: str, *, source_page_id: str | None = None, new_page_id: str | None = None, url: str | None = None) -> dict[str, Any]:
        number = self._event_next.get(profile_id, 1)
        self._event_next[profile_id] = number + 1
        event = {"event_id": f"Event-{number:04d}", "profile_id": profile_id, "source_page_id": source_page_id, "new_page_id": new_page_id, "event_type": event_type, "created_at": utc_now(), "url": url}
        self._events.setdefault(profile_id, []).append(event)
        return event

    async def _register_dialog(self, profile_id: str, page: Any, dialog: Any) -> None:
        number = self._dialog_next.get(profile_id, 1)
        self._dialog_next[profile_id] = number + 1
        dialog_id = f"Dialog-{number:04d}"
        self._dialogs.setdefault(profile_id, {})[dialog_id] = {"dialog": dialog, "page_id": self._page_id_for_object(profile_id, page), "type": dialog.type, "message_length": len(dialog.message), "created_at": utc_now()}

    async def _register_popup(self, profile_id: str, source_page: Any, popup: Any) -> None:
        # The actual Page object is registered by _sync_pages; this event keeps
        # the source Page relationship explicit without using URL/title identity.
        await asyncio.sleep(0)
        source_page_id = self._page_id_for_object(profile_id, source_page)
        registry = await self._sync_pages(profile_id)
        new_page_id = self._page_id_for_object(profile_id, popup)
        self._record_event(profile_id, "popup", source_page_id=source_page_id, new_page_id=new_page_id, url=popup.url)

    def _attach_page_events(self, profile_id: str, page: Any) -> None:
        if page not in [entry["page"] for entry in self._page_registry.get(profile_id, {}).values()]:
            return
        page_object_id = id(page)
        if page_object_id in self._attached_page_objects:
            return
        self._attached_page_objects.add(page_object_id)
        page.on("dialog", lambda dialog: asyncio.create_task(self._register_dialog(profile_id, page, dialog)))
        page.on("popup", lambda popup: asyncio.create_task(self._register_popup(profile_id, page, popup)))

    async def _resolve_locator(self, page: Any, data: dict[str, Any], *, timeout: int = 5_000) -> tuple[Any, dict[str, Any]]:
        candidates: list[Any] = []
        if isinstance(data.get("locators"), list):
            candidates.extend(data["locators"])
        elif data.get("locator") is not None:
            candidates.append(data["locator"])
        elif isinstance(data.get("selector"), str):
            candidates.append({"type": "css", "value": data["selector"]})
        else:
            raise LocatorResolutionError("locator_not_found", "a locator or selector is required")
        if not 1 <= len(candidates) <= 3:
            raise LocatorResolutionError("invalid_request", "at least one and at most three locators are allowed")
        strict = data.get("strict") is True
        attempts: list[dict[str, Any]] = []
        for index, spec in enumerate(candidates):
            if not isinstance(spec, dict):
                attempts.append({"index": index, "reason": "invalid_locator"}); continue
            kind = spec.get("type")
            value = spec.get("value")
            try:
                if kind == "css" and isinstance(value, str):
                    locator = page.locator(value)
                elif kind == "role" and isinstance(spec.get("role"), str):
                    kwargs = {"name": spec.get("name"), "exact": bool(spec.get("exact", False))}
                    if kwargs["name"] is None: kwargs.pop("name")
                    locator = page.get_by_role(spec["role"], **kwargs)
                elif kind == "text" and isinstance(value, str):
                    locator = page.get_by_text(value, exact=bool(spec.get("exact", False)))
                elif kind == "label" and isinstance(value, str):
                    locator = page.get_by_label(value, exact=bool(spec.get("exact", False)))
                elif kind == "placeholder" and isinstance(value, str):
                    locator = page.get_by_placeholder(value, exact=bool(spec.get("exact", False)))
                elif kind == "test_id" and isinstance(value, str):
                    locator = page.get_by_test_id(value)
                else:
                    attempts.append({"index": index, "type": kind, "reason": "invalid_locator"}); continue
                count = await locator.count()
                if count == 0:
                    attempts.append({"index": index, "type": kind, "reason": "not_found"}); continue
                if strict and count != 1:
                    attempts.append({"index": index, "type": kind, "reason": "ambiguous", "count": count}); continue
                selected = locator.first
                return selected, {"locator_used": spec, "fallback_attempts": attempts, "match_count": count}
            except Exception as exc:
                attempts.append({"index": index, "type": kind, "reason": "resolution_error", "message": str(exc)[:200]})
        code = "locator_ambiguous" if any(item.get("reason") == "ambiguous" for item in attempts) and all(item.get("reason") in {"ambiguous", "invalid_locator"} for item in attempts) else "locator_not_found"
        raise LocatorResolutionError(code, "no unique locator matched", attempts)

    async def _postcondition(self, profile_id: str, page: Any, baseline_url: str, baseline_title: str, after: Any, *, timeout: int, baseline_event_count: int = 0) -> dict[str, Any]:
        if after is None or after == "none":
            return {"status": "none", "type": "none"}
        if not isinstance(after, dict):
            return {"status": "not_met", "type": "invalid", "error": "postcondition_not_met"}
        kind = after.get("type")
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            try:
                if kind == "url_changed" and page.url != baseline_url: return {"status": "met", "type": kind, "url": page.url}
                if kind == "url_contains" and str(after.get("value", "")) in page.url: return {"status": "met", "type": kind, "url": page.url}
                if kind == "url_equals" and page.url == after.get("value"): return {"status": "met", "type": kind, "url": page.url}
                title = await page.title()
                if kind == "title_changed" and title != baseline_title: return {"status": "met", "type": kind, "title": title}
                if kind == "title_contains" and str(after.get("value", "")) in title: return {"status": "met", "type": kind, "title": title}
                if kind == "page_event" and len(self._events.get(profile_id, [])) > baseline_event_count: return {"status": "met", "type": kind, "event": self._events[profile_id][-1]}
                if kind == "page_close" and page.is_closed(): return {"status": "met", "type": kind}
                if kind == "locator":
                    locator, meta = await self._resolve_locator(page, after, timeout=min(timeout, 500))
                    state = after.get("state", "visible")
                    if state == "visible" and await locator.is_visible(): return {"status": "met", "type": kind, "state": state, **meta}
                    if state == "hidden" and not await locator.is_visible(): return {"status": "met", "type": kind, "state": state, **meta}
                    if state == "attached" and await locator.count() > 0: return {"status": "met", "type": kind, "state": state, **meta}
                    if state == "detached" and await locator.count() == 0: return {"status": "met", "type": kind, "state": state, **meta}
            except LocatorResolutionError:
                if after.get("state") == "detached": return {"status": "met", "type": kind, "state": "detached"}
            except Exception:
                pass
            await asyncio.sleep(0.05)
        return {"status": "not_met", "type": kind, "error": "postcondition_timeout"}

    async def _navigate(self, page: Any, url: str, *, timeout: int, retries: int = 1) -> dict[str, Any]:
        requested = url; first_error: str | None = None
        for attempt in range(retries + 1):
            started = time.monotonic(); committed = domcontentloaded = loaded = usable = False; response = None
            try:
                response = await page.goto(url, wait_until="commit", timeout=timeout)
                committed = True
                try: await page.wait_for_load_state("domcontentloaded", timeout=min(timeout, 8_000)); domcontentloaded = True
                except PlaywrightTimeoutError: pass
                try: await page.wait_for_load_state("load", timeout=min(timeout, 4_000)); loaded = True
                except PlaywrightTimeoutError: pass
                try: await page.title(); await page.locator("body").count(); usable = True
                except Exception: usable = False
                redirects = 0; req = response.request if response is not None else None
                while req is not None and req.redirected_from is not None: redirects += 1; req = req.redirected_from
                status = "success" if domcontentloaded else ("navigation_partial_success" if committed and usable else "navigation_timeout")
                return {"requested_url": requested, "final_url": page.url, "title": await page.title(), "committed": committed, "domcontentloaded": domcontentloaded, "load": loaded, "usable": usable, "redirect_count": redirects, "duration_ms": round((time.monotonic() - started) * 1000, 1), "status": status, "error": None if status != "navigation_timeout" else "domcontentloaded_timeout", "retry_count": attempt, "first_error": first_error}
            except Exception as exc:
                first_error = first_error or str(exc)[:300]
                if attempt >= retries: return {"requested_url": requested, "final_url": page.url, "title": await page.title(), "committed": committed, "domcontentloaded": domcontentloaded, "load": loaded, "usable": usable, "redirect_count": 0, "duration_ms": round((time.monotonic() - started) * 1000, 1), "status": "navigation_network_error", "error": first_error, "retry_count": attempt, "first_error": first_error}
        return {"requested_url": requested, "final_url": page.url, "title": await page.title(), "committed": False, "domcontentloaded": False, "load": False, "usable": False, "redirect_count": 0, "duration_ms": 0, "status": "navigation_network_error", "error": first_error, "retry_count": retries, "first_error": first_error}

    async def _sync_pages(self, profile_id: str) -> dict[str, dict[str, Any]]:
        # Action paths reuse Manager's already-bound runtime. Calling the
        # expensive OS process enumeration for every one of many page actions
        # creates avoidable contention; the Manager monitor remains responsible
        # for detecting a disappeared browser.
        record = self.manager.get(profile_id)
        if record.get("status") != "running":
            raise ProfileManagerError("profile_not_running")
        context = self.manager.context_for(profile_id)
        registry = self._page_registry.setdefault(profile_id, {})
        live_pages = list(context.pages)
        live_ids = {id(page) for page in live_pages}
        for page_id, entry in list(registry.items()):
            if id(entry["page"]) not in live_ids:
                self._closed_page_ids.setdefault(profile_id, set()).add(page_id)
                registry.pop(page_id, None)
        for page in live_pages:
            if not any(entry["page"] is page for entry in registry.values()):
                number = self._page_next_number.get(profile_id, 1)
                page_id = f"Page-{number:02d}"
                self._page_next_number[profile_id] = number + 1
                registry[page_id] = {"page": page, "created_at": utc_now()}
                self._record_event(profile_id, "new_page", new_page_id=page_id, url=page.url)
            self._attach_page_events(profile_id, page)
        if registry:
            current_default = self._default_page_id.get(profile_id)
            if current_default not in registry:
                self._default_page_id[profile_id] = next(iter(registry))
        else:
            self._default_page_id.pop(profile_id, None)
        return registry

    async def _page_for(self, profile_id: str, page_id: str | None = None) -> Any:
        registry = await self._sync_pages(profile_id)
        selected = page_id or self._default_page_id.get(profile_id)
        if selected is None:
            raise ProfileManagerError("no_pages")
        entry = registry.get(selected)
        if entry is None:
            if selected in self._closed_page_ids.get(profile_id, set()):
                raise ProfileManagerError("page_closed")
            raise ProfileManagerError("page_not_found")
        return entry["page"]

    async def _page_summary(self, profile_id: str, page_id: str, page: Any) -> dict[str, Any]:
        return {"page_id": page_id, "url": page.url, "title": await page.title(), "active": self._default_page_id.get(profile_id) == page_id, "state": "open" if not page.is_closed() else "closed"}

    async def _page_for_legacy(self, profile_id: str) -> Any:
        record = self.manager.get(profile_id)
        if record.get("status") != "running":
            raise ProfileManagerError("profile_not_running")
        return await self._page_for(profile_id)

    def _clear_profile_pages(self, profile_id: str) -> None:
        for entry in self._page_registry.get(profile_id, {}).values():
            self._attached_page_objects.discard(id(entry["page"]))
        self._page_registry.pop(profile_id, None)
        self._default_page_id.pop(profile_id, None)
        self._closed_page_ids.pop(profile_id, None)
        self._events.pop(profile_id, None)
        self._dialogs.pop(profile_id, None)

    def _clear_profile_window(self, profile_id: str) -> None:
        self._window_state.pop(profile_id, None)
        self._window_handles.pop(profile_id, None)

    async def _dispatch(self, method: str, target: str, body: bytes) -> tuple[int, dict[str, Any], str | None, str]:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        segments = [s for s in path.split("/") if s]
        data: dict[str, Any] = {}
        if body:
            try:
                value = json.loads(body.decode("utf-8"))
            except Exception as exc:
                return 400, self._error("invalid_json", str(exc)), None, "error"
            if not isinstance(value, dict):
                return 400, self._error("invalid_request", "JSON body must be an object"), None, "error"
            data = value

        if path == "/api/health" and method == "GET":
            records = [await self.manager.status(item["id"]) for item in self.manager.list()]
            return 200, {
                "service_status": "ok",
                "manager_status": "ready",
                "running_profile_count": sum(r["status"] == "running" for r in records),
                "starting_profile_count": sum(r["status"] == "starting" for r in records),
                "soft_concurrency_limit": self.manager.soft_concurrency_limit,
                "window_mode": {
                    "hidden_enabled": self.allow_hidden_window_mode,
                    "hidden_status": "experimental_p0_1_pending",
                    "focus_enabled": self.allow_window_focus,
                },
            }, None, "ok"
        if path == "/api/profiles" and method == "GET":
            return 200, {"profiles": [await self.manager.status(item["id"]) for item in self.manager.list()]}, None, "ok"
        if path == "/api/profiles" and method == "POST":
            name = data.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                return 400, self._error("invalid_request", "name must be a non-empty string"), None, "error"
            existing = {item["id"] for item in self.manager.list()}
            index = 1
            while f"Profile-{index:02d}" in existing:
                index += 1
            profile_id = f"Profile-{index:02d}"
            return 201, {"profile": self.manager.create(profile_id, name.strip() if isinstance(name, str) else profile_id)}, profile_id, "ok"

        if len(segments) < 3 or segments[0:2] != ["api", "profiles"]:
            return 404, self._error("not_found", "endpoint not found"), None, "error"
        profile_id = segments[2]
        if not self._profile_id_re.match(profile_id):
            return 400, self._error("invalid_request", "invalid profile id", profile_id), profile_id, "error"
        try:
            if len(segments) == 3 and method == "GET":
                return 200, {"profile": await self.manager.status(profile_id)}, profile_id, "ok"
            page_id: str | None = None
            action = segments[3] if len(segments) > 3 else ""
            if len(segments) == 4 and action == "network":
                if method == "GET":
                    record = await self.manager.status(profile_id)
                    network = record["network"]
                    return 200, {
                        "profile_id": profile_id,
                        "network": network,
                        "applies_on": "next_start",
                        "proxy_authentication": "http_https_basic_only",
                        "credentials_configured": bool(network.get("authentication") == "basic" and self.proxy_credentials is not None and self.proxy_credentials.has(profile_id)),
                    }, profile_id, "ok"
                if method == "PUT":
                    record = self.manager.get(profile_id)
                    if record.get("status") != "stopped":
                        raise ProfileManagerError("network_change_requires_stopped_profile")
                    username_present = "username" in data
                    password_present = "password" in data
                    network_data = {key: value for key, value in data.items() if key not in {"username", "password"}}
                    try:
                        network = normalize_network_config(network_data)
                    except ValueError as exc:
                        raise ProfileManagerError(str(exc)) from exc
                    if network.get("authentication") == "basic":
                        if self.proxy_credentials is None:
                            raise ProfileManagerError("proxy_credentials_store_unavailable")
                        if username_present != password_present:
                            raise ProfileManagerError("proxy_credentials_require_username_and_password_together")
                        route_changed = network != record.get("network")
                        if username_present:
                            try:
                                self.proxy_credentials.set(profile_id, data["username"], data["password"])
                            except (OSError, ValueError) as exc:
                                raise ProfileManagerError(str(exc)) from exc
                        elif route_changed or not self.proxy_credentials.has(profile_id):
                            raise ProfileManagerError("proxy_credentials_missing")
                    elif username_present or password_present:
                        raise ProfileManagerError("proxy_credentials_not_allowed_for_this_network")
                    elif self.proxy_credentials is not None:
                        self.proxy_credentials.clear(profile_id)
                    result = self.manager.configure_network(profile_id, network)
                    return 200, {
                        "profile_id": profile_id,
                        "network": result["network"],
                        "applies_on": "next_start",
                        "proxy_authentication": "http_https_basic_only",
                        "credentials_configured": bool(result["network"].get("authentication") == "basic" and self.proxy_credentials is not None and self.proxy_credentials.has(profile_id)),
                    }, profile_id, "ok"
                return 405, self._error("method_not_allowed", "network accepts GET or PUT", profile_id), profile_id, "error"
            if len(segments) >= 6 and segments[3] == "pages":
                page_id = segments[4]
                action = segments[5]
            if len(segments) == 4 and segments[3] == "window":
                record = await self.manager.status(profile_id)
                if record.get("status") != "running":
                    if record.get("last_error") == "browser_process_disappeared":
                        return 503, self._error("browser_process_disappeared", record["last_error"], profile_id), profile_id, "error"
                    return 409, self._error("profile_not_running", "profile is not running", profile_id), profile_id, "error"
                handles = self._window_handles.get(profile_id) or profile_window_handles(Path(record["user_data_dir"]))
                visible_count = sum(bool(ctypes.windll.user32.IsWindowVisible(hwnd)) for hwnd in handles if ctypes.windll.user32.IsWindow(hwnd))
                state = self._window_state.get(profile_id, "visible")
                return 200, {"profile_id": profile_id, "browser_status": record["status"], "window_state": state, "window_count": len(handles), "visible_window_count": visible_count, "success": True}, profile_id, "ok"
            if len(segments) == 5 and segments[3] == "window" and segments[4] in {"hide", "show", "minimize", "focus"} and method == "POST":
                record = await self.manager.status(profile_id)
                if record.get("status") != "running":
                    if record.get("last_error") == "browser_process_disappeared":
                        return 503, self._error("browser_process_disappeared", record["last_error"], profile_id), profile_id, "error"
                    return 409, self._error("profile_not_running", "profile is not running", profile_id), profile_id, "error"
                if segments[4] == "focus":
                    if not self.allow_window_focus:
                        return 409, self._error("window_focus_disabled", "foreground focus is disabled for this local service", profile_id), profile_id, "error"
                    handles, visible_count, focused = focus_profile_windows(Path(record["user_data_dir"]), self._window_handles.get(profile_id))
                    self._window_handles[profile_id] = handles
                    self._window_state[profile_id] = "visible"
                    return 200, {"profile_id": profile_id, "browser_status": record["status"], "window_state": "visible", "window_count": len(handles), "visible_window_count": visible_count, "focus_requested": True, "focused": focused, "success": True}, profile_id, "ok"
                requested = {"hide": "hidden", "show": "visible", "minimize": "minimized"}[segments[4]]
                if requested == "hidden" and not self.allow_hidden_window_mode:
                    return 409, self._error("hidden_mode_experimental_disabled", "hidden mode is disabled by default because P0-1 foreground stability is pending", profile_id), profile_id, "error"
                handles, visible_count = set_profile_windows(Path(record["user_data_dir"]), requested, self._window_handles.get(profile_id))
                self._window_handles[profile_id] = handles
                self._window_state[profile_id] = requested
                return 200, {"profile_id": profile_id, "browser_status": record["status"], "window_state": requested, "window_count": len(handles), "visible_window_count": visible_count, "success": True}, profile_id, "ok"
            # Page collection and page-handle lifecycle endpoints.
            if len(segments) == 4 and segments[3] == "pages":
                if method == "GET":
                    registry = await self._sync_pages(profile_id)
                    pages = [await self._page_summary(profile_id, page_id, entry["page"]) for page_id, entry in registry.items()]
                    return 200, {"profile_id": profile_id, "default_page_id": self._default_page_id.get(profile_id), "pages": pages}, profile_id, "ok"
                if method == "POST":
                    record = await self.manager.status(profile_id)
                    if record.get("status") != "running":
                        raise ProfileManagerError("profile_not_running")
                    context = self.manager.context_for(profile_id)
                    try:
                        page = await asyncio.wait_for(context.new_page(), timeout=30.0)
                    except asyncio.TimeoutError:
                        return 504, self._error("action_timeout", "new page creation timed out", profile_id), profile_id, "error"
                    registry = await self._sync_pages(profile_id)
                    page_id = next((candidate for candidate, entry in registry.items() if entry["page"] is page), None)
                    if page_id is None:
                        raise ProfileManagerError("page_registration_failed")
                    url = data.get("url")
                    if url is not None:
                        parsed_url = urlsplit(url) if isinstance(url, str) else None
                        if parsed_url is None or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                            await page.close()
                            await self._sync_pages(profile_id)
                            return 400, self._error("invalid_request", "url must be an absolute http(s) URL", profile_id), profile_id, "error"
                        navigation = await self._navigate(page, url, timeout=30_000, retries=1)
                        if navigation["status"] == "navigation_network_error":
                            return 502, self._error("navigation_network_error", navigation.get("error") or "navigation failed", profile_id), profile_id, "error"
                    else:
                        navigation = None
                    return 201, {"profile_id": profile_id, "page": await self._page_summary(profile_id, page_id, page), "navigation": navigation}, profile_id, "ok"
            if len(segments) >= 5 and segments[3] == "pages":
                page_id = segments[4]
                if len(segments) == 5 and method == "GET":
                    page = await self._page_for(profile_id, page_id)
                    return 200, {"profile_id": profile_id, "page": await self._page_summary(profile_id, page_id, page), "exists": True}, profile_id, "ok"
                if len(segments) == 6 and segments[5] == "close" and method == "POST":
                    page = await self._page_for(profile_id, page_id)
                    await page.close()
                    await self._sync_pages(profile_id)
                    return 200, {"profile_id": profile_id, "page_id": page_id, "closed": True}, profile_id, "ok"
            if len(segments) == 4 and segments[3] == "events" and method == "GET":
                await self._sync_pages(profile_id)
                return 200, {"profile_id": profile_id, "events": list(self._events.get(profile_id, []))}, profile_id, "ok"
            if len(segments) == 4 and segments[3] == "dialogs" and method == "GET":
                dialogs = []
                for dialog_id, entry in self._dialogs.get(profile_id, {}).items():
                    dialogs.append({"dialog_id": dialog_id, "profile_id": profile_id, "page_id": entry.get("page_id"), "type": entry["type"], "message_length": entry["message_length"], "created_at": entry["created_at"]})
                return 200, {"profile_id": profile_id, "dialogs": dialogs}, profile_id, "ok"
            if len(segments) == 6 and segments[3] == "dialogs" and segments[5] in {"accept", "dismiss"} and method == "POST":
                dialog_id = segments[4]
                entry = self._dialogs.get(profile_id, {}).get(dialog_id)
                if entry is None:
                    return 404, self._error("dialog_not_found", "dialog is not pending", profile_id), profile_id, "error"
                dialog = entry["dialog"]
                if segments[5] == "accept":
                    await dialog.accept(data.get("prompt_text") if entry["type"] == "prompt" else None)
                else:
                    await dialog.dismiss()
                self._dialogs[profile_id].pop(dialog_id, None)
                return 200, {"profile_id": profile_id, "dialog_id": dialog_id, "action": segments[5], "success": True}, profile_id, "ok"
            if action in {"start", "stop", "restart"} and method == "POST":
                if action == "start":
                    result = await self.manager.start(profile_id)
                    self._window_state[profile_id] = "minimized" if "--start-minimized" in self.manager.browser_args else "visible"
                elif action == "stop":
                    result = await self.manager.stop(profile_id)
                    self._clear_profile_pages(profile_id)
                    self._clear_profile_window(profile_id)
                else:
                    result = await self.manager.restart(profile_id)
                    self._clear_profile_pages(profile_id)
                    self._clear_profile_window(profile_id)
                    self._window_state[profile_id] = "minimized" if "--start-minimized" in self.manager.browser_args else "visible"
                return 200, {"profile": result}, profile_id, "ok"
            if action in {"navigate", "screenshot"} and method == "POST":
                page = await self._page_for(profile_id, page_id)
                if action == "navigate":
                    url = data.get("url")
                    parsed_url = urlsplit(url) if isinstance(url, str) else None
                    if parsed_url is None or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                        return 400, self._error("invalid_request", "url must be an absolute http(s) URL", profile_id), profile_id, "error"
                    navigation = await self._navigate(page, url, timeout=int(data.get("timeout", 30_000)), retries=min(int(data.get("retries", 1)), 2))
                    after = data.get("after")
                    if after is not None:
                        navigation["postcondition_result"] = await self._postcondition(profile_id, page, page.url, navigation.get("title", ""), after, timeout=min(int(data.get("timeout", 30_000)), 10_000))
                    if navigation["status"] == "navigation_network_error":
                        return 502, {"profile_id": profile_id, **navigation, "error": "navigation_network_error", "success": False}, profile_id, "error"
                    return 200, {"profile_id": profile_id, **navigation, "success": True}, profile_id, "ok"
                stamp = int(time.time() * 1000)
                output = self.screenshot_root / f"{profile_id}-{stamp}.png"
                try:
                    await page.screenshot(path=str(output), full_page=False)
                    return 200, {"profile_id": profile_id, "screenshot_path": str(output.resolve()), "success": True}, profile_id, "ok"
                except Exception as exc:
                    return 502, self._error("navigation_failed", f"screenshot: {exc}", profile_id), profile_id, "error"
            if action in {"check", "uncheck", "select", "keyboard", "hover", "scroll_into_view", "frame", "upload", "download", "back", "forward"} and method == "POST":
                page = await self._page_for(profile_id, page_id)
                timeout = data.get("timeout", 5_000)
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 100 or timeout > 60_000:
                    return 400, self._error("invalid_request", "timeout must be between 100 and 60000 milliseconds", profile_id), profile_id, "error"
                timeout = int(timeout)
                try:
                    if action in {"check", "uncheck", "select", "hover", "scroll_into_view", "upload"}:
                        try:
                            locator, locator_meta = await self._resolve_locator(page, data, timeout=timeout)
                        except LocatorResolutionError as exc:
                            code = exc.code
                            if "selector" in data and "locator" not in data and "locators" not in data and code in {"locator_not_found", "locator_ambiguous"}:
                                code = "selector_not_found" if code == "locator_not_found" else "selector_ambiguous"
                            status = 400 if code == "invalid_request" else 422
                            return status, {**self._error(code, str(exc), profile_id), "locator_attempts": exc.attempts}, profile_id, "error"
                        selector = data.get("selector") or data.get("locator") or data.get("locators")
                    if action == "check":
                        await locator.check(timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "checked": await locator.is_checked(), "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "uncheck":
                        await locator.uncheck(timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "checked": await locator.is_checked(), "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "select":
                        values = data.get("values")
                        value = data.get("value")
                        label = data.get("label")
                        if values is not None:
                            selected = await locator.select_option(value=values, timeout=timeout)
                        elif value is not None:
                            selected = await locator.select_option(value=value, timeout=timeout)
                        elif label is not None:
                            selected = await locator.select_option(label=label, timeout=timeout)
                        else:
                            return 400, self._error("invalid_request", "select requires value, label, or values", profile_id), profile_id, "error"
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "selected": selected, "value": await locator.input_value(), "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "hover":
                        await locator.hover(timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "scroll_into_view":
                        await locator.scroll_into_view_if_needed(timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "upload":
                        file_path = data.get("file_path")
                        if not isinstance(file_path, str) or not Path(file_path).is_absolute():
                            return 400, self._error("invalid_request", "file_path must be an absolute local path", profile_id), profile_id, "error"
                        file_obj = Path(file_path)
                        if not file_obj.exists() or not file_obj.is_file():
                            return 404, self._error("file_not_found", "file does not exist", profile_id), profile_id, "error"
                        await locator.set_input_files(str(file_obj), timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "filename": file_obj.name, "success": True, "action_result": "executed"}, profile_id, "ok"
                    if action == "keyboard":
                        key = data.get("key")
                        allowed = {"Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Control+A", "Control+C", "Control+V"}
                        if key not in allowed:
                            return 400, self._error("invalid_request", "unsupported structured key", profile_id), profile_id, "error"
                        selector = data.get("selector")
                        if selector is not None:
                            try:
                                target, locator_meta = await self._resolve_locator(page, data, timeout=timeout)
                            except LocatorResolutionError as exc:
                                return (400 if exc.code == "invalid_request" else 422), {**self._error(exc.code, str(exc), profile_id), "locator_attempts": exc.attempts}, profile_id, "error"
                            await target.focus(timeout=timeout)
                        await page.keyboard.press(key)
                        return 200, {"profile_id": profile_id, "key": key, "success": True, "action_result": "executed", **(locator_meta if selector is not None else {})}, profile_id, "ok"
                    if action in {"back", "forward"}:
                        response = await (page.go_back(timeout=timeout) if action == "back" else page.go_forward(timeout=timeout))
                        return 200, {"profile_id": profile_id, "current_url": page.url, "title": await page.title(), "response": bool(response), "success": True}, profile_id, "ok"
                    if action == "frame":
                        frame_selector = data.get("frame_selector")
                        selector = data.get("selector")
                        operation = data.get("operation", "element")
                        if not isinstance(frame_selector, str) or not isinstance(selector, str):
                            return 400, self._error("invalid_request", "frame_selector and selector are required", profile_id), profile_id, "error"
                        frame = page.frame_locator(frame_selector).locator(selector).first
                        if operation == "fill":
                            await frame.fill(data.get("value", ""), timeout=timeout)
                            return 200, {"profile_id": profile_id, "operation": operation, "success": True}, profile_id, "ok"
                        if operation == "click":
                            await frame.click(timeout=timeout)
                            return 200, {"profile_id": profile_id, "operation": operation, "success": True}, profile_id, "ok"
                        if operation == "element":
                            return 200, {"profile_id": profile_id, "operation": operation, "text": await frame.text_content(timeout=timeout), "visible": await frame.is_visible(timeout=timeout), "success": True}, profile_id, "ok"
                        return 400, self._error("invalid_request", "frame operation must be element, fill, or click", profile_id), profile_id, "error"
                    if action == "download":
                        try:
                            target, locator_meta = await self._resolve_locator(page, data, timeout=timeout)
                        except LocatorResolutionError as exc:
                            return (400 if exc.code == "invalid_request" else 422), {**self._error(exc.code, str(exc), profile_id), "locator_attempts": exc.attempts}, profile_id, "error"
                        selector = data.get("selector") or data.get("locator") or data.get("locators")
                        download_manager = self.manager.download_manager_for(profile_id)
                        marker = download_manager.observation_marker()
                        async with page.expect_download(timeout=timeout) as download_info:
                            await target.click(timeout=timeout)
                        download = await download_info.value
                        result = await download_manager.wait_for_download(download, marker, timeout / 1000)
                        destination = Path(result["path"])
                        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, **result, "sha256": digest, "action_result": "executed"}, profile_id, "ok"
                except DownloadTrackingError as exc:
                    return 502, self._error("download_failed", str(exc), profile_id), profile_id, "error"
                except PlaywrightTimeoutError:
                    if action == "frame":
                        return 422, self._error("frame_not_found", "frame or element not found", profile_id), profile_id, "error"
                    return 504, self._error("download_timeout" if action == "download" else "action_timeout", f"{action} timed out", profile_id), profile_id, "error"
                except Exception as exc:
                    if action == "frame":
                        return 422, self._error("frame_not_found", str(exc), profile_id), profile_id, "error"
                    return 502, self._error("action_failed", f"{action}: {exc}", profile_id), profile_id, "error"
            if action in {"click", "fill", "scroll", "element", "wait"} and method == "POST":
                page = await self._page_for(profile_id, page_id)
                timeout = data.get("timeout", 5_000)
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 100 or timeout > 60_000:
                    return 400, self._error("invalid_request", "timeout must be between 100 and 60000 milliseconds", profile_id), profile_id, "error"
                timeout = int(timeout)
                try:
                    if action == "scroll":
                        direction = data.get("direction")
                        amount = data.get("amount")
                        if direction not in {"up", "down"} or not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0 or amount > 100_000:
                            return 400, self._error("invalid_request", "direction must be up/down and amount must be 0<amount<=100000", profile_id), profile_id, "error"
                        await page.mouse.wheel(0, int(amount) if direction == "down" else -int(amount))
                        return 200, {"profile_id": profile_id, "direction": direction, "amount": int(amount), "success": True}, profile_id, "ok"
                    try:
                        locator, locator_meta = await self._resolve_locator(page, data, timeout=timeout)
                    except LocatorResolutionError as exc:
                        if action == "wait" and data.get("state") == "detached" and exc.code in {"locator_not_found", "selector_not_found"}:
                            return 200, {"profile_id": profile_id, "selector": data.get("selector") or data.get("locator") or data.get("locators"), "state": "detached", "success": True, "action_result": "executed"}, profile_id, "ok"
                        code = exc.code
                        if "selector" in data and "locator" not in data and "locators" not in data and code in {"locator_not_found", "locator_ambiguous"}:
                            code = "selector_not_found" if code == "locator_not_found" else "selector_ambiguous"
                        status = 400 if code == "invalid_request" else 422
                        return status, {**self._error(code, str(exc), profile_id), "locator_attempts": exc.attempts}, profile_id, "error"
                    selector = data.get("selector") or data.get("locator") or data.get("locators")
                    baseline_url = page.url
                    baseline_title = await page.title()
                    baseline_event_count = len(self._events.get(profile_id, []))
                    if action == "click":
                        if not await locator.is_visible(timeout=timeout):
                            return 422, self._error("locator_not_visible" if ("locator" in data or "locators" in data) else "element_not_visible", "element is not visible", profile_id), profile_id, "error"
                        if not await locator.is_enabled(timeout=timeout):
                            return 422, self._error("locator_not_enabled" if ("locator" in data or "locators" in data) else "element_not_enabled", "element is disabled", profile_id), profile_id, "error"
                        await locator.click(timeout=timeout)
                        post = await self._postcondition(profile_id, page, baseline_url, baseline_title, data.get("after"), timeout=min(timeout, 10_000), baseline_event_count=baseline_event_count)
                        payload = {"profile_id": profile_id, "selector": selector, **locator_meta, "success": True, "action_result": "executed", "postcondition_result": post}
                        if post.get("status") == "not_met":
                            return 504, {**payload, "error": "postcondition_timeout", "message": "click executed but postcondition was not met"}, profile_id, "error"
                        return 200, payload, profile_id, "ok"
                    if action == "fill":
                        value = data.get("value")
                        if not isinstance(value, str):
                            return 400, self._error("invalid_request", "value must be a string", profile_id), profile_id, "error"
                        if not await locator.is_visible(timeout=timeout):
                            return 422, self._error("locator_not_visible" if ("locator" in data or "locators" in data) else "element_not_visible", "element is not visible", profile_id), profile_id, "error"
                        if not await locator.is_enabled(timeout=timeout):
                            return 422, self._error("locator_not_enabled" if ("locator" in data or "locators" in data) else "element_not_enabled", "element is disabled", profile_id), profile_id, "error"
                        await locator.fill(value, timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "value_length": len(value), "success": True, "action_result": "executed", "postcondition_result": await self._postcondition(profile_id, page, baseline_url, baseline_title, data.get("after"), timeout=min(timeout, 10_000), baseline_event_count=baseline_event_count)}, profile_id, "ok"
                    if action == "element":
                        visible = await locator.is_visible(timeout=timeout)
                        text = await locator.text_content(timeout=timeout)
                        tag = None
                        handle = await locator.element_handle(timeout=timeout)
                        if handle is not None:
                            tag_handle = await handle.get_property("tagName")
                            tag = await tag_handle.json_value()
                            await handle.dispose()
                        # input_value reads the live form-control value; get_attribute
                        # would only return the initial HTML attribute.
                        value = await locator.input_value(timeout=timeout) if tag in {"INPUT", "TEXTAREA", "SELECT"} else await locator.get_attribute("value", timeout=timeout)
                        href = await locator.get_attribute("href", timeout=timeout)
                        return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "exists": True, "text": text, "tag": tag, "visible": visible, "value": value, "href": href, "success": True}, profile_id, "ok"
                    state = data.get("state")
                    if state not in {"attached", "visible", "hidden", "detached"}:
                        return 400, self._error("invalid_request", "state must be attached, visible, hidden, or detached", profile_id), profile_id, "error"
                    await locator.wait_for(state=state, timeout=timeout)
                    return 200, {"profile_id": profile_id, "selector": selector, **locator_meta, "state": state, "success": True}, profile_id, "ok"
                except PlaywrightTimeoutError:
                    return 504, self._error("action_timeout", f"{action} timed out", profile_id), profile_id, "error"
                except Exception as exc:
                    return 502, self._error("action_failed", f"{action}: {exc}", profile_id), profile_id, "error"
            if action == "page" and method == "GET":
                page = await self._page_for(profile_id, page_id)
                return 200, {"profile_id": profile_id, "current_url": page.url, "title": await page.title(), "ready_state": await page.evaluate("document.readyState")}, profile_id, "ok"
            return 405, self._error("method_not_allowed", "method not allowed", profile_id), profile_id, "error"
        except ProfileManagerError as exc:
            message = str(exc)
            if message.startswith("unknown profile"):
                return 404, self._error("profile_not_found", message, profile_id), profile_id, "error"
            if message == "profile_not_running" or message.startswith("profile is not running"):
                return 409, self._error("profile_not_running", message, profile_id), profile_id, "error"
            if message == "automation_not_attached":
                return 409, self._error("automation_not_attached", "this Profile is running in native manual mode", profile_id), profile_id, "error"
            if message == "page_not_found":
                return 404, self._error("page_not_found", message, profile_id), profile_id, "error"
            if message == "page_closed":
                return 409, self._error("page_closed", message, profile_id), profile_id, "error"
            if message == "no_pages":
                return 409, self._error("no_pages", message, profile_id), profile_id, "error"
            if message.startswith("soft_concurrency_limit_reached"):
                return 409, self._error("soft_concurrency_limit_reached", message, profile_id), profile_id, "error"
            if message == "network_change_requires_stopped_profile":
                return 409, self._error("network_change_requires_stopped_profile", "stop the Profile before changing its network", profile_id), profile_id, "error"
            if message.startswith(("network_", "direct_network_", "fixed_network_", "proxy_", "dpapi_")):
                return 400, self._error("invalid_network_config", message, profile_id), profile_id, "error"
            if message.startswith("start failed"):
                return 503, self._error("browser_start_failed", message, profile_id), profile_id, "error"
            if "browser_process_disappeared" in message:
                return 503, self._error("browser_process_disappeared", message, profile_id), profile_id, "error"
            return 500, self._error("manager_error", message, profile_id), profile_id, "error"
        except WindowControlError as exc:
            if str(exc) == "window_not_found":
                return 404, self._error("window_not_found", str(exc), profile_id), profile_id, "error"
            return 500, self._error("window_control_failed", str(exc), profile_id), profile_id, "error"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        method = "?"
        target = "?"
        profile_id: str | None = None
        request_id = uuid.uuid4().hex
        started_at = time.monotonic()
        queued_iso = utc_now()
        action_started_iso = queued_iso
        try:
            method, target, headers, body = await self._read_request(reader)
            origin = headers.get("origin")
            segments = [item for item in urlsplit(target).path.split("/") if item]
            profile_id = segments[2] if len(segments) >= 3 and segments[:2] == ["api", "profiles"] else None
            if self._origin_is_rejected(origin):
                status, payload, result = 403, self._error("origin_not_allowed", "request Origin is not the configured local management page", profile_id), "error"
            elif method == "OPTIONS":
                status, payload, result = 200, {"success": True}, "ok"
            else:
                lock_key = None
                if profile_id and len(segments) >= 5 and segments[3] == "pages":
                    lock_key = f"{profile_id}/{segments[4]}"
                elif profile_id and len(segments) >= 4 and segments[3] in {"navigate", "click", "fill", "scroll", "element", "wait", "screenshot", "keyboard", "hover", "check", "uncheck", "select", "frame", "upload", "download", "back", "forward", "scroll_into_view"}:
                    lock_key = f"{profile_id}/default"
                if lock_key:
                    lock = self._page_locks.setdefault(lock_key, asyncio.Lock())
                    async with lock:
                        action_started_iso = utc_now()
                        status, payload, profile_id, result = await self._dispatch(method, target, body)
                else:
                    action_started_iso = utc_now()
                    status, payload, profile_id, result = await self._dispatch(method, target, body)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            status, payload, result = 400, self._error("invalid_request", str(exc)), "error"
        except Exception as exc:
            status, payload, result = 500, self._error("internal_error", str(exc)), "error"
        self._log(method=method, path=target, profile_id=profile_id, action=target, result=result, error=payload.get("error") if result == "error" else None, request_id=request_id, duration_ms=int((time.monotonic() - started_at) * 1000), queued_at=queued_iso, started_at=action_started_iso, finished_at=utc_now())
        try:
            await self._write_response(writer, status, payload, origin=locals().get("origin"))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
