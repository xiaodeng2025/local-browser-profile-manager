"""Cross-process ownership for one persistent Manager data directory."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from typing import Any


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED_0 = 0x00000080
WAIT_TIMEOUT = 0x00000102


class DataDirLockError(RuntimeError):
    """Raised when another Manager owns the requested data directory."""


def _mutex_name(data_dir: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(data_dir.resolve(strict=False))))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"Local\\LocalBrowserProfileManager-{digest}"


class DataDirInstanceLock:
    """Own a Windows named mutex for the lifetime of one Manager process.

    The mutex handle is owned by the process and is therefore released by
    Windows when the process exits unexpectedly; no stale lock file remains.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._handle: Any | None = None
        self._kernel32: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if os.name != "nt":
            raise RuntimeError("windows_data_dir_lock_required")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        handle = kernel32.CreateMutexW(None, False, _mutex_name(self.data_dir))
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        result = kernel32.WaitForSingleObject(handle, 0)
        if result in {WAIT_OBJECT_0, WAIT_ABANDONED_0}:
            self._kernel32 = kernel32
            self._handle = handle
            return

        kernel32.CloseHandle(handle)
        if result == WAIT_TIMEOUT:
            raise DataDirLockError(f"data_dir_in_use:{self.data_dir}")
        raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed: {result}")

    def release(self) -> None:
        handle = self._handle
        kernel32 = self._kernel32
        self._handle = None
        self._kernel32 = None
        if handle is None or kernel32 is None:
            return
        try:
            kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)

    def __enter__(self) -> "DataDirInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()
