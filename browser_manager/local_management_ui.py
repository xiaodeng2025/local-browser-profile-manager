"""Loopback-only presentation and settings layer for local Profile Manager v0.1.

This module deliberately has no browser lifecycle implementation.  It stores
only user-facing presentation metadata, while the existing LocalProfileAPI
remains the sole lifecycle and page-control boundary.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


DEFAULT_COLORS = ["#2563eb", "#7c3aed", "#db2777", "#059669", "#d97706", "#0891b2"]
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class UISettingsStore:
    """Atomic, presentation-only settings separate from ProfileManager's registry."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "presets_initialized": False, "profiles": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("profiles", {}), dict):
            raise ValueError("ui_settings_invalid")
        return {
            "version": 1,
            "presets_initialized": bool(value.get("presets_initialized", False)),
            "profiles": value.get("profiles", {}),
        }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    @staticmethod
    def _default(profile_id: str, fallback_name: str, index: int) -> dict[str, Any]:
        return {
            "display_name": fallback_name,
            "color": DEFAULT_COLORS[index % len(DEFAULT_COLORS)],
            "note": "",
            "shortcuts": [],
        }

    def ensure_profiles(self, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        changed = False
        with self._lock:
            for index, record in enumerate(records):
                profile_id = str(record["id"])
                if profile_id not in self._data["profiles"]:
                    self._data["profiles"][profile_id] = self._default(profile_id, str(record.get("name") or profile_id), index)
                    changed = True
            if changed:
                self._save_locked()
            return {profile_id: dict(value) for profile_id, value in self._data["profiles"].items()}

    def mark_presets_initialized(self) -> None:
        with self._lock:
            if not self._data["presets_initialized"]:
                self._data["presets_initialized"] = True
                self._save_locked()

    def bootstrap_presets(self, create: Callable[[str, str], dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create the three stopped records once, only for a first empty Registry."""
        if records:
            self.mark_presets_initialized()
            return records
        with self._lock:
            already_initialized = bool(self._data["presets_initialized"])
        if not already_initialized:
            for number in range(1, 4):
                profile_id = f"Profile-{number:02d}"
                create(profile_id, f"浏览器档案 {number:02d}")
            records = [
                {"id": f"Profile-{number:02d}", "name": f"浏览器档案 {number:02d}"}
                for number in range(1, 4)
            ]
        self.mark_presets_initialized()
        self.ensure_profiles(records)
        return records

    def get_all(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "version": 1,
            "presets_initialized": True,
            "profiles": self.ensure_profiles(records),
            "manual_mode": True,
            "network": "direct",
            "hidden_status": "P0-1 pending / unavailable",
        }

    @staticmethod
    def _validate_shortcuts(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or len(value) > 20:
            raise ValueError("shortcuts_must_be_a_list_of_at_most_20_items")
        result: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("shortcut_must_be_an_object")
            name = item.get("name")
            url = item.get("url")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise ValueError("shortcut_name_invalid")
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError("shortcut_url_invalid")
            parsed = urlsplit(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("shortcut_url_must_be_http_or_https")
            result.append({"name": name.strip(), "url": url.strip()})
        return result

    def update(self, profile_id: str, payload: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        if not PROFILE_ID_RE.match(profile_id) or profile_id not in {str(item["id"]) for item in records}:
            raise KeyError("profile_not_found")
        self.ensure_profiles(records)
        display_name = payload.get("display_name")
        color = payload.get("color")
        note = payload.get("note")
        shortcuts = payload.get("shortcuts")
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 80:
            raise ValueError("display_name_invalid")
        if not isinstance(color, str) or not HEX_COLOR_RE.match(color):
            raise ValueError("color_invalid")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note_invalid")
        validated_shortcuts = self._validate_shortcuts(shortcuts)
        with self._lock:
            self._data["profiles"][profile_id] = {
                "display_name": display_name.strip(),
                "color": color.lower(),
                "note": note,
                "shortcuts": validated_shortcuts,
            }
            self._save_locked()
            return dict(self._data["profiles"][profile_id])


def _safe_static(static_dir: Path, request_path: str) -> Path | None:
    relative = unquote(request_path.removeprefix("/assets/")).replace("/", os.sep)
    if not relative or ".." in Path(relative).parts:
        return None
    candidate = (static_dir / relative).resolve()
    try:
        candidate.relative_to(static_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def start_management_ui_server(*, static_dir: Path, settings: UISettingsStore, records: Callable[[], list[dict[str, Any]]], api_base_provider: Callable[[], str], port: int) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start a separate loopback UI server; it never controls Chrome directly."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalProfileManagerUI/0.1"

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, value: dict[str, Any]) -> None:
            self._send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/":
                index = static_dir / "index.html"
                self._send_bytes(HTTPStatus.OK, index.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/ui-config.json":
                self._send_json(HTTPStatus.OK, {"api_base": api_base_provider(), "ui_version": "0.1", "loopback_only": True})
                return
            if path == "/api/ui/settings":
                self._send_json(HTTPStatus.OK, settings.get_all(records()))
                return
            if path.startswith("/assets/"):
                asset = _safe_static(static_dir, path)
                if asset is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                content_type = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}.get(asset.suffix.lower(), "application/octet-stream")
                self._send_bytes(HTTPStatus.OK, asset.read_bytes(), content_type)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_PUT(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            match = re.fullmatch(r"/api/ui/settings/([A-Za-z0-9_-]{1,64})", path)
            if not match:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 32 * 1024:
                    raise ValueError("body_too_large")
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload_must_be_an_object")
                item = settings.update(match.group(1), payload, records())
                self._send_json(HTTPStatus.OK, {"profile_id": match.group(1), "settings": item})
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "profile_not_found"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_settings", "message": str(exc)})

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="local-profile-manager-ui", daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/"
