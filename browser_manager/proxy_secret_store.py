"""Windows user-scoped encrypted storage for HTTP(S) proxy credentials."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any


CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _win_error(operation: str) -> OSError:
    return OSError(ctypes.get_last_error(), f"{operation}_failed")


class ProxyCredentialStore:
    """Keep proxy credentials encrypted for the current Windows user only.

    The on-disk JSON contains base64-encoded DPAPI blobs, never plaintext
    username/password values.  It is intentionally separate from the Profile
    registry so ordinary status records and service event logs cannot expose
    credentials.
    """

    def __init__(self, path: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("windows_dpapi_required")
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob), wintypes.LPVOID,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob), wintypes.LPVOID,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "profiles": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        profiles = value.get("profiles") if isinstance(value, dict) else None
        if not isinstance(profiles, dict) or any(not isinstance(profile_id, str) or not isinstance(blob, str) for profile_id, blob in profiles.items()):
            raise ValueError("proxy_secret_store_invalid")
        return {"version": 1, "profiles": dict(profiles)}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, Any]:
        buffer = ctypes.create_string_buffer(value)
        return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    def _protect(self, value: bytes) -> bytes:
        source, source_buffer = self._blob(value)
        output = _DataBlob()
        if not self._crypt32.CryptProtectData(ctypes.byref(source), "browser-manager-proxy", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
            raise _win_error("dpapi_protect")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
            del source_buffer

    def _unprotect(self, value: bytes) -> bytes:
        source, source_buffer = self._blob(value)
        output = _DataBlob()
        if not self._crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
            raise _win_error("dpapi_unprotect")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
            del source_buffer

    @staticmethod
    def _validate(username: Any, password: Any) -> tuple[str, str]:
        if not isinstance(username, str) or not 1 <= len(username) <= 256:
            raise ValueError("proxy_username_invalid")
        if not isinstance(password, str) or not 1 <= len(password) <= 512:
            raise ValueError("proxy_password_invalid")
        return username, password

    def has(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._data["profiles"]

    def set(self, profile_id: str, username: Any, password: Any) -> None:
        username, password = self._validate(username, password)
        encrypted = self._protect(json.dumps({"username": username, "password": password}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        with self._lock:
            self._data["profiles"][profile_id] = base64.b64encode(encrypted).decode("ascii")
            self._save_locked()

    def get(self, profile_id: str) -> dict[str, str] | None:
        with self._lock:
            encoded = self._data["profiles"].get(profile_id)
        if encoded is None:
            return None
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            value = json.loads(self._unprotect(raw).decode("utf-8"))
        except Exception as exc:
            raise ValueError("proxy_credentials_unavailable") from exc
        username, password = self._validate(value.get("username") if isinstance(value, dict) else None, value.get("password") if isinstance(value, dict) else None)
        return {"username": username, "password": password}

    def clear(self, profile_id: str) -> None:
        with self._lock:
            if profile_id in self._data["profiles"]:
                self._data["profiles"].pop(profile_id, None)
                self._save_locked()
