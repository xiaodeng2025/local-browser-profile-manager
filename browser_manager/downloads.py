"""Native Chromium download configuration for one Profile at a time."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class DownloadTrackingError(RuntimeError):
    """A native Chromium download could not be confirmed safely."""


def configure_chromium_download_preferences(profile_dir: Path, download_dir: Path) -> Path:
    """Change only Chromium's download preferences and preserve other fields."""
    destination = Path(download_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    preferences_path = Path(profile_dir) / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    preferences: dict[str, Any] = {}
    if preferences_path.exists():
        try:
            loaded = json.loads(preferences_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DownloadTrackingError("download_preferences_unreadable") from exc
        if not isinstance(loaded, dict):
            raise DownloadTrackingError("download_preferences_invalid")
        preferences = loaded

    download = preferences.get("download")
    if not isinstance(download, dict):
        download = {}
        preferences["download"] = download
    download["default_directory"] = str(destination)
    download["directory_upgrade"] = True
    download["prompt_for_download"] = False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="Preferences.download-",
        suffix=".tmp",
        dir=preferences_path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary:
            json.dump(preferences, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, preferences_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


class ProfileDownloadManager:
    """Observe one Profile's native Chromium downloads through CDP."""

    def __init__(self, profile_id: str, root: Path, on_error: Callable[[str], None] | None = None) -> None:
        self.profile_id = profile_id
        self.root = Path(root).resolve()
        self._on_error = on_error
        self._cdp: Any | None = None
        self._closed = False
        self._sequence = 0
        self._downloads: dict[str, dict[str, Any]] = {}
        self._changed = asyncio.Event()
        self._will_begin_callback = self._on_download_will_begin
        self._progress_callback = self._on_download_progress

    async def attach(self, cdp: Any) -> None:
        if self._cdp is not None:
            raise RuntimeError("download_tracker_already_attached")
        if cdp is None or not hasattr(cdp, "on") or not hasattr(cdp, "send"):
            raise DownloadTrackingError("download_cdp_session_unavailable")
        self.root.mkdir(parents=True, exist_ok=True)
        self._cdp = cdp
        cdp.on("Browser.downloadWillBegin", self._will_begin_callback)
        cdp.on("Browser.downloadProgress", self._progress_callback)
        try:
            await cdp.send("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": True})
        except Exception as exc:
            await self.close()
            raise DownloadTrackingError(f"download_behavior_default_failed:{type(exc).__name__}") from exc

    def observation_marker(self) -> int:
        if self._closed:
            raise DownloadTrackingError("download_tracker_closed")
        return self._sequence

    def _on_download_will_begin(self, event: dict[str, Any]) -> None:
        guid = event.get("guid")
        if self._closed or not isinstance(guid, str) or not guid:
            return
        self._sequence += 1
        self._downloads[guid] = {
            "guid": guid,
            "sequence": self._sequence,
            "url": event.get("url"),
            "suggested_filename": event.get("suggestedFilename"),
            "state": "inProgress",
            "result": None,
            "error": None,
        }
        self._changed.set()

    def _on_download_progress(self, event: dict[str, Any]) -> None:
        guid = event.get("guid")
        if self._closed or not isinstance(guid, str) or not guid:
            return
        item = self._downloads.get(guid)
        if item is None:
            self._sequence += 1
            item = {"guid": guid, "sequence": self._sequence, "url": None, "suggested_filename": None, "result": None, "error": None}
            self._downloads[guid] = item
        state = event.get("state")
        item["state"] = state
        if state == "completed":
            self._complete(item, event.get("filePath"))
        elif state == "canceled":
            self._fail(item, "download_canceled")
        self._changed.set()

    def _complete(self, item: dict[str, Any], file_path: Any) -> None:
        if not isinstance(file_path, str) or not file_path:
            self._fail(item, "download_completed_missing_file_path")
            return
        destination = Path(file_path).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError:
            self._fail(item, "download_completed_path_outside_profile_directory")
            return
        if not destination.is_file():
            self._fail(item, "download_completed_file_missing")
            return
        item["result"] = {
            "profile_id": self.profile_id,
            "filename": destination.name,
            "path": str(destination),
            "success": True,
        }

    def _fail(self, item: dict[str, Any], error: str) -> None:
        item["error"] = error
        if self._on_error:
            self._on_error(f"{error}:{item['guid']}")

    async def wait_for_download(self, download: Any, marker: int, timeout_seconds: float) -> dict[str, Any]:
        """Return the native final path for the first matching post-click download."""
        if timeout_seconds <= 0:
            raise DownloadTrackingError("download_tracking_timeout")
        url = getattr(download, "url", None)
        suggested_filename = getattr(download, "suggested_filename", None)
        deadline = time.monotonic() + timeout_seconds
        while True:
            matches = [
                item
                for item in self._downloads.values()
                if item["sequence"] > marker
                and item.get("url") == url
                and item.get("suggested_filename") == suggested_filename
            ]
            if matches:
                item = min(matches, key=lambda candidate: candidate["sequence"])
                if item.get("error"):
                    raise DownloadTrackingError(str(item["error"]))
                if item.get("result") is not None:
                    return dict(item["result"])
            if self._closed:
                raise DownloadTrackingError("download_tracker_closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadTrackingError("download_tracking_timeout")
            await self._wait_for_change(remaining)

    async def _wait_for_change(self, timeout_seconds: float) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise DownloadTrackingError("download_tracking_timeout") from exc
        finally:
            self._changed.clear()

    async def close(self) -> None:
        self._closed = True
        self._changed.set()
        if self._cdp is not None:
            for event_name, callback in (
                ("Browser.downloadWillBegin", self._will_begin_callback),
                ("Browser.downloadProgress", self._progress_callback),
            ):
                try:
                    self._cdp.remove_listener(event_name, callback)
                except Exception:
                    pass
        self._cdp = None
        self._downloads.clear()
