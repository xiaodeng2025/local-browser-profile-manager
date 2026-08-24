"""Local Profile Manager control layer for the browser manager.

It deliberately owns lifecycle state, per-Profile fixed network settings, and
Playwright bindings, but does not implement GUI, business automation, proxy
rotation policy, or browser modifications.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .network_config import normalize_network_config, proxy_launch_args, without_proxy_arguments
from .permission_manager import PermissionPolicyError


STATUSES = {"stopped", "starting", "running", "stopping", "error"}


class ProfileManagerError(RuntimeError):
    pass


class ProfileProcessProbeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_line_arguments(command_line: str) -> list[str]:
    if not isinstance(command_line, str):
        return []
    if os.name != "nt":
        try:
            return shlex.split(command_line, posix=False)
        except ValueError:
            return []

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    argc = ctypes.c_int()
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


def _strip_argument_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _extract_user_data_dir(command_line: str) -> str | None:
    arguments = _command_line_arguments(command_line)
    for index, argument in enumerate(arguments):
        lower = argument.lower()
        if lower == "--user-data-dir" and index + 1 < len(arguments):
            return _strip_argument_quotes(arguments[index + 1])
        if lower.startswith("--user-data-dir="):
            return _strip_argument_quotes(argument.split("=", 1)[1])
    return None


def _normalized_user_data_dir(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).resolve(strict=False))))


def profile_process_matches(command_line: str, user_data_dir: Path) -> bool:
    actual = _extract_user_data_dir(command_line)
    return actual is not None and _normalized_user_data_dir(actual) == _normalized_user_data_dir(user_data_dir)


def foreground_snapshot() -> dict[str, int]:
    """Read the current foreground HWND/PID; never changes either one."""
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    pid = ctypes.c_ulong()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"hwnd": hwnd, "pid": int(pid.value)}


def _query_profile_processes(user_data_dir: Path, *, strict: bool) -> list[dict[str, Any]]:
    """Query Chrome processes, optionally failing closed on probe errors."""
    command = (
        "$items=@(Get-CimInstance Win32_Process | Where-Object "
        "{$_.Name -eq 'chrome.exe' -and $_.CommandLine} | "
        "Select-Object ProcessId,Name,CommandLine); "
        "$items | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
        if not raw:
            return []
        value = json.loads(raw)
        if isinstance(value, dict):
            value = [value]
        return [
            item for item in value
            if isinstance(item, dict) and profile_process_matches(str(item.get("CommandLine", "")), user_data_dir)
        ]
    except Exception as exc:
        if strict:
            raise ProfileProcessProbeError(f"profile_process_probe_failed:{type(exc).__name__}: {exc}") from exc
        return []


def query_profile_processes(user_data_dir: Path) -> list[dict[str, Any]]:
    """Return Chrome processes while preserving the legacy empty-list behavior."""
    return _query_profile_processes(user_data_dir, strict=False)


def query_profile_processes_strict(user_data_dir: Path) -> list[dict[str, Any]]:
    """Return Chrome processes or raise when the startup safety probe fails."""
    return _query_profile_processes(user_data_dir, strict=True)


def query_profile_windows(user_data_dir: Path) -> list[int]:
    """Read top-level HWNDs owned by this Profile's exact Chrome PIDs."""
    pids = {int(item["ProcessId"]) for item in query_profile_processes(user_data_dir) if str(item.get("ProcessId", "")).isdigit()}
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


class ProfileManager:
    def __init__(
        self,
        playwright: Any,
        *,
        registry_path: Path,
        profiles_root: Path,
        browser_executable: Path,
        ready_url: str,
        ready_title: str | None = None,
        soft_concurrency_limit: int = 24,
        ready_timeout_ms: int = 30_000,
        max_retries: int = 2,
        monitor_interval: float = 0.5,
        browser_args: list[str] | None = None,
        background_mode: bool = True,
        permission_manager: Any | None = None,
        proxy_credentials: Any | None = None,
    ) -> None:
        self.playwright = playwright
        self.registry_path = Path(registry_path)
        self.profiles_root = Path(profiles_root)
        self.browser_executable = Path(browser_executable)
        self.ready_url = ready_url
        self.ready_title = ready_title
        self.soft_concurrency_limit = soft_concurrency_limit
        self.ready_timeout_ms = ready_timeout_ms
        self.max_retries = max_retries
        self.monitor_interval = monitor_interval
        self.browser_args = list(browser_args or [])
        self.background_mode = bool(background_mode)
        self.permission_manager = permission_manager
        self.proxy_credentials = proxy_credentials
        self._runtime: dict[str, dict[str, Any]] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.registry_path.exists():
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            self._records = {item["id"]: item for item in data.get("profiles", [])}
        for record in self._records.values():
            try:
                record["network"] = normalize_network_config(record.get("network"))
            except ValueError as exc:
                raise ProfileManagerError(f"invalid_network_config:{record.get('id', '?')}:{exc}") from exc
            try:
                processes = query_profile_processes_strict(Path(record["user_data_dir"]))
            except ProfileProcessProbeError as exc:
                record.update({
                    "status": "error",
                    "process_ids": [],
                    "last_error": f"recovery_required:process_probe_failed:{exc}",
                    "startup_stage": "error",
                })
                continue
            if processes:
                record.update({
                    "status": "error",
                    "process_ids": [p["ProcessId"] for p in processes if str(p.get("ProcessId", "")).isdigit()],
                    "last_error": "recovery_required:live_profile_processes",
                    "startup_stage": "error",
                })
                continue
            if record.get("status") == "error" and str(record.get("last_error", "")).startswith("recovery_required:"):
                self._clear_recovery_state(record)
            elif record.get("status") in {"starting", "running", "stopping"}:
                record["status"] = "stopped"
                record["process_ids"] = []
                record["last_error"] = "manager_restarted"
        self._save()

    def _save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": utc_now(), "profiles": list(self._records.values())}
        temp = self.registry_path.with_suffix(self.registry_path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.registry_path)

    def list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._records.values()]

    def get(self, profile_id: str) -> dict[str, Any]:
        if profile_id not in self._records:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        return dict(self._records[profile_id])

    def context_for(self, profile_id: str) -> Any:
        record = self._records.get(profile_id)
        runtime = self._runtime.get(profile_id)
        if record is None:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        if record.get("status") != "running" or runtime is None:
            raise ProfileManagerError(f"profile is not running: {profile_id}")
        return runtime["context"]

    def create(self, profile_id: str, name: str | None = None) -> dict[str, Any]:
        if profile_id in self._records:
            raise ProfileManagerError(f"profile already exists: {profile_id}")
        user_data_dir = self.profiles_root / profile_id
        user_data_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": profile_id,
            "name": name or profile_id,
            "user_data_dir": str(user_data_dir.resolve()),
            "browser_executable": str(self.browser_executable.resolve()),
            "created_at": utc_now(),
            "status": "stopped",
            "process_ids": [],
            "retry_count": 0,
            "last_ready_at": None,
            "last_error": None,
            "root_pid": None,
            "debug_endpoint": None,
            "startup_stage": "stopped",
            "project_hwnds": [],
            "permission_policy": {},
            "target_id": None,
            "startup_trace": [],
            # Direct means this Profile explicitly does not use a Chromium
            # application proxy. It does not override operating-system routes.
            "network": {"mode": "direct"},
        }
        self._records[profile_id] = record
        self._save()
        return dict(record)

    def configure_network(self, profile_id: str, value: dict[str, Any]) -> dict[str, Any]:
        """Persist the next-start network route for one stopped Profile only."""
        record = self._records.get(profile_id)
        if record is None:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        if record.get("status") != "stopped":
            raise ProfileManagerError("network_change_requires_stopped_profile")
        try:
            record["network"] = normalize_network_config(value)
        except ValueError as exc:
            raise ProfileManagerError(str(exc)) from exc
        self._save()
        return dict(record)

    def _browser_args_for(self, record: dict[str, Any]) -> list[str]:
        """Make the per-Profile route override any accidental global proxy arg."""
        return [*without_proxy_arguments(self.browser_args), *proxy_launch_args(record["network"])]

    def _proxy_credentials_for(self, profile_id: str, record: dict[str, Any]) -> dict[str, str] | None:
        network = record["network"]
        if network.get("authentication") != "basic":
            return None
        if not self.background_mode:
            raise ProfileManagerError("proxy_basic_authentication_requires_background_mode")
        if self.proxy_credentials is None:
            raise ProfileManagerError("proxy_credentials_store_unavailable")
        try:
            credentials = self.proxy_credentials.get(profile_id)
        except ValueError as exc:
            raise ProfileManagerError(str(exc)) from exc
        if credentials is None:
            raise ProfileManagerError("proxy_credentials_missing")
        return credentials

    async def _configure_proxy_auth_handler(self, cdp: Any, credentials: dict[str, str] | None) -> set[asyncio.Task[Any]]:
        """Answer only proxy auth challenges; never inject credentials into sites."""
        tasks: set[asyncio.Task[Any]] = set()
        if credentials is None:
            return tasks
        await cdp.send("Fetch.enable", {"patterns": [], "handleAuthRequests": True})

        async def continue_auth(event: dict[str, Any]) -> None:
            challenge = event.get("authChallenge") if isinstance(event, dict) else None
            source = challenge.get("source") if isinstance(challenge, dict) else None
            response: dict[str, Any]
            if source == "Proxy":
                response = {"response": "ProvideCredentials", "username": credentials["username"], "password": credentials["password"]}
            else:
                response = {"response": "Default"}
            try:
                await cdp.send("Fetch.continueWithAuth", {"requestId": event["requestId"], "authChallengeResponse": response})
            except Exception:
                # CDP can close while an auth response is queued during stop.
                # Do not leak credential values into service errors or logs.
                return

        def on_auth_required(event: dict[str, Any]) -> None:
            task = asyncio.create_task(continue_auth(event))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        cdp.on("Fetch.authRequired", on_auth_required)
        return tasks

    def _count_starting_or_running(self) -> int:
        return sum(item.get("status") in {"starting", "running"} for item in self._records.values())

    def _mark_recovery_required(self, record: dict[str, Any], error: str, processes: list[dict[str, Any]] | None = None) -> None:
        record.update({
            "status": "error",
            "process_ids": [p["ProcessId"] for p in (processes or []) if str(p.get("ProcessId", "")).isdigit()],
            "last_error": error,
            "startup_stage": "error",
        })

    def _clear_recovery_state(self, record: dict[str, Any]) -> None:
        record.update({
            "status": "stopped",
            "last_error": None,
            "process_ids": [],
            "root_pid": None,
            "debug_endpoint": None,
            "project_hwnds": [],
            "target_id": None,
            "startup_stage": "stopped",
        })

    async def _ensure_profile_processes_absent(self, record: dict[str, Any]) -> None:
        try:
            processes = await asyncio.to_thread(query_profile_processes_strict, Path(record["user_data_dir"]))
        except ProfileProcessProbeError as exc:
            error = f"recovery_required:process_probe_failed:{exc}"
            self._mark_recovery_required(record, error)
            self._save()
            raise ProfileManagerError(error) from exc
        if processes:
            error = "recovery_required:live_profile_processes"
            self._mark_recovery_required(record, error, processes)
            self._save()
            raise ProfileManagerError(error)

    async def _wait_for_processes(self, profile_id: str, present: bool, timeout: float = 10.0) -> list[dict[str, Any]]:
        record = self._records[profile_id]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            processes = await asyncio.to_thread(query_profile_processes, Path(record["user_data_dir"]))
            if bool(processes) == present:
                return processes
            await asyncio.sleep(0.25)
        return query_profile_processes(Path(record["user_data_dir"]))

    def _set_startup_stage(self, record: dict[str, Any], stage: str) -> None:
        record["startup_stage"] = stage
        record.setdefault("startup_trace", []).append({"stage": stage, "at": utc_now(), "foreground": foreground_snapshot()})
        self._save()

    async def _wait_devtools_endpoint(self, profile_dir: Path, timeout: float = 20.0) -> str:
        port_file = profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if lines and lines[0].isdigit():
                    endpoint = f"http://127.0.0.1:{lines[0]}"
                    try:
                        await asyncio.to_thread(urllib.request.urlopen, endpoint + "/json/version", timeout=0.5)
                        return endpoint
                    except Exception:
                        pass
            await asyncio.sleep(0.1)
        raise ProfileManagerError("cdp_endpoint_timeout")

    async def _launch_background_runtime(self, profile_id: str, record: dict[str, Any]) -> dict[str, Any]:
        profile_dir = Path(record["user_data_dir"])
        stale_port = profile_dir / "DevToolsActivePort"
        if stale_port.exists():
            # Chrome can release the endpoint file a fraction after its
            # process has exited.  Retry only this exact Profile file; never
            # bypass the lock by touching another Profile or process.
            deadline = time.monotonic() + 5.0
            while stale_port.exists():
                try:
                    stale_port.unlink()
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise ProfileManagerError("cdp_port_file_cleanup_failed")
                    await asyncio.sleep(0.1)
        self._set_startup_stage(record, "starting_browser")
        browser_args = self._browser_args_for(record)
        proxy_credentials = self._proxy_credentials_for(profile_id, record)
        process = subprocess.Popen(
            [
                str(record["browser_executable"]),
                f"--user-data-dir={record['user_data_dir']}",
                "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-startup-window",
                *browser_args,
            ],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        record["root_pid"] = int(process.pid)
        self._save()
        endpoint = await self._wait_devtools_endpoint(profile_dir)
        record["debug_endpoint"] = endpoint
        self._set_startup_stage(record, "connecting_cdp")
        browser = await self.playwright.chromium.connect_over_cdp(endpoint)
        contexts = list(browser.contexts)
        if not contexts:
            raise ProfileManagerError("cdp_default_context_unavailable")
        context = contexts[0]
        self._set_startup_stage(record, "applying_permissions")
        permission_applied: list[dict[str, Any]] = []
        if self.permission_manager is not None:
            try:
                permission_applied = await self.permission_manager.apply(profile_id, context)
            except PermissionPolicyError:
                raise
        record["permission_policy"] = permission_applied
        self._set_startup_stage(record, "creating_background_page")
        cdp = await browser.new_browser_cdp_session()
        proxy_auth_tasks = await self._configure_proxy_auth_handler(cdp, proxy_credentials)
        before_pages = {id(page) for page in context.pages}
        try:
            target_result = await cdp.send(
                "Target.createTarget",
                {"url": "about:blank", "background": True, "focus": False},
            )
        except Exception as exc:
            raise ProfileManagerError(f"background_target_create_failed:{type(exc).__name__}: {exc}") from exc
        target_id = target_result.get("targetId") if isinstance(target_result, dict) else None
        if not target_id:
            raise ProfileManagerError("background_target_missing_id")
        deadline = time.monotonic() + 10.0
        page = None
        while time.monotonic() < deadline:
            candidates = [item for item in context.pages if id(item) not in before_pages]
            if candidates:
                page = candidates[-1]
                break
            await asyncio.sleep(0.1)
        if page is None:
            raise ProfileManagerError("background_target_not_attached")
        self._set_startup_stage(record, "playwright_ready")
        return {
            "process": process,
            "root_pid": int(process.pid),
            "browser": browser,
            "cdp": cdp,
            "context": context,
            "page": page,
            "target_id": target_id,
            "endpoint": endpoint,
            "permission_applied": permission_applied,
            "proxy_auth_tasks": proxy_auth_tasks,
        }

    async def _shutdown_runtime(self, profile_id: str, runtime: dict[str, Any], *, fallback: bool = True) -> dict[str, Any]:
        record = self._records[profile_id]
        graceful_error: str | None = None
        started = time.monotonic()
        context = runtime.get("context")
        browser = runtime.get("browser")
        cdp = runtime.get("cdp")
        for task in list(runtime.get("proxy_auth_tasks", set())):
            task.cancel()
        if context is not None:
            for page in list(context.pages):
                try:
                    await page.close()
                except Exception:
                    pass
        if cdp is not None:
            try:
                await cdp.send("Browser.close")
            except Exception as exc:
                graceful_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        processes = await self._wait_for_processes(profile_id, False, timeout=8.0)
        forced: list[int] = []
        if processes and fallback:
            pids = sorted({int(item["ProcessId"]) for item in processes if str(item.get("ProcessId", "")).isdigit()})
            root_pid = int(runtime.get("root_pid") or record.get("root_pid") or 0)
            if root_pid and root_pid not in pids:
                pids.append(root_pid)
            if pids:
                expression = "$ids=@(" + ",".join(str(pid) for pid in sorted(set(pids))) + "); foreach($id in $ids){Stop-Process -Id $id -Force -ErrorAction SilentlyContinue}"
                subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", expression], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                forced = sorted(set(pids))
                processes = await self._wait_for_processes(profile_id, False, timeout=8.0)
        return {
            "graceful_error": graceful_error,
            "graceful": not processes,
            "fallback_pids": forced,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "remaining_processes": [int(item["ProcessId"]) for item in processes if str(item.get("ProcessId", "")).isdigit()],
            "remaining_hwnds": query_profile_windows(Path(record["user_data_dir"])),
        }

    async def start(self, profile_id: str) -> dict[str, Any]:
        record = self._records.get(profile_id)
        if record is None:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        if record.get("status") == "running":
            return dict(record)
        if str(record.get("last_error", "")).startswith("recovery_required:"):
            await self._ensure_profile_processes_absent(record)
            self._clear_recovery_state(record)
            self._save()
        else:
            await self._ensure_profile_processes_absent(record)
        if self._count_starting_or_running() >= self.soft_concurrency_limit:
            record["last_error"] = f"soft_concurrency_limit_reached:{self.soft_concurrency_limit}"
            self._save()
            raise ProfileManagerError(record["last_error"])

        record.update({"status": "starting", "last_error": None, "retry_count": 0, "process_ids": [], "startup_trace": [], "startup_stage": "starting"})
        self._save()
        last_error = "unknown_start_error"
        for attempt in range(self.max_retries + 1):
            record["retry_count"] = attempt
            self._save()
            runtime: dict[str, Any] | None = None
            context = None
            try:
                browser_args = self._browser_args_for(record)
                if not self.background_mode:
                    context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=record["user_data_dir"],
                        executable_path=record["browser_executable"],
                        headless=False,
                        viewport={"width": 1280, "height": 720},
                        args=["--no-first-run", "--no-default-browser-check", *browser_args],
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                    runtime = {"context": context, "browser": None, "cdp": None, "page": page, "root_pid": None, "target_id": None, "endpoint": None}
                else:
                    runtime = await self._launch_background_runtime(profile_id, record)
                    context = runtime["context"]
                    page = runtime["page"]
                await page.goto(self.ready_url, wait_until="domcontentloaded", timeout=self.ready_timeout_ms)
                title = await page.title()
                await page.evaluate("() => document.readyState")
                if self.ready_title is not None and title != self.ready_title:
                    raise ProfileManagerError(f"ready_title_mismatch:{title}")
                processes = await self._wait_for_processes(profile_id, True)
                if not processes:
                    raise ProfileManagerError("browser_process_not_observed")
                record["project_hwnds"] = query_profile_windows(Path(record["user_data_dir"]))
                record["target_id"] = runtime.get("target_id") if runtime else None
                record.update({
                    "status": "running",
                    "process_ids": [p["ProcessId"] for p in processes],
                    "last_ready_at": utc_now(),
                    "last_error": None,
                    "startup_stage": "running",
                })
                self._save()
                monitor = asyncio.create_task(self._monitor(profile_id))
                runtime.update({"monitor": monitor, "expected_stop": False})
                self._runtime[profile_id] = runtime
                return dict(record)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                record["last_error"] = last_error
                record["startup_stage"] = "error"
                if runtime is not None:
                    try:
                        await self._shutdown_runtime(profile_id, runtime, fallback=True)
                    except Exception:
                        pass
                elif context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                elif record.get("root_pid"):
                    try:
                        await self._shutdown_runtime(profile_id, {"root_pid": record.get("root_pid")}, fallback=True)
                    except Exception:
                        pass
                await self._wait_for_processes(profile_id, False, timeout=5.0)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        record.update({"status": "error", "last_error": last_error, "process_ids": [], "startup_stage": "error", "project_hwnds": [], "target_id": None})
        self._save()
        raise ProfileManagerError(f"start failed for {profile_id}: {last_error}")

    async def _monitor(self, profile_id: str) -> None:
        while True:
            record = self._records.get(profile_id)
            runtime = self._runtime.get(profile_id)
            if record is None or runtime is None or record.get("status") != "running":
                return
            processes = await asyncio.to_thread(query_profile_processes, Path(record["user_data_dir"]))
            if not processes:
                expected = bool(runtime.get("expected_stop"))
                try:
                    await runtime["context"].close()
                except Exception:
                    pass
                try:
                    if runtime.get("browser") is not None:
                        await runtime["browser"].close()
                except Exception:
                    pass
                record["process_ids"] = []
                record["project_hwnds"] = []
                record["status"] = "stopped" if expected else "error"
                record["last_error"] = None if expected else "browser_process_disappeared"
                record["startup_stage"] = "stopped" if expected else "error"
                self._save()
                return
            record["process_ids"] = [p["ProcessId"] for p in processes]
            record["project_hwnds"] = query_profile_windows(Path(record["user_data_dir"]))
            await asyncio.sleep(self.monitor_interval)

    async def status(self, profile_id: str) -> dict[str, Any]:
        record = self._records.get(profile_id)
        if record is None:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        if record.get("status") == "running" and not await asyncio.to_thread(query_profile_processes, Path(record["user_data_dir"])):
            runtime = self._runtime.pop(profile_id, None)
            if runtime:
                runtime["monitor"].cancel()
                try:
                    await runtime["context"].close()
                except Exception:
                    pass
            record.update({"status": "error", "process_ids": [], "last_error": "browser_process_disappeared"})
            self._save()
        return dict(record)

    async def stop(self, profile_id: str) -> dict[str, Any]:
        record = self._records.get(profile_id)
        if record is None:
            raise ProfileManagerError(f"unknown profile: {profile_id}")
        if record.get("status") == "stopped":
            return dict(record)
        record["status"] = "stopping"
        self._save()
        runtime = self._runtime.pop(profile_id, None)
        if runtime:
            runtime["expected_stop"] = True
            runtime["monitor"].cancel()
            try:
                await runtime["monitor"]
            except asyncio.CancelledError:
                pass
            cleanup = await self._shutdown_runtime(profile_id, runtime, fallback=True)
        else:
            cleanup = {"graceful": not query_profile_processes(Path(record["user_data_dir"])), "fallback_pids": [], "remaining_processes": [], "remaining_hwnds": query_profile_windows(Path(record["user_data_dir"]))}
        processes = query_profile_processes(Path(record["user_data_dir"]))
        if processes:
            record.update({"status": "error", "process_ids": [p["ProcessId"] for p in processes], "last_error": "profile_processes_remain", "startup_stage": "error", "project_hwnds": query_profile_windows(Path(record["user_data_dir"]))})
        else:
            record.update({"status": "stopped", "process_ids": [], "root_pid": None, "debug_endpoint": None, "startup_stage": "stopped", "project_hwnds": [], "target_id": None, "last_error": None})
        record["last_cleanup"] = cleanup
        self._save()
        return dict(record)

    async def restart(self, profile_id: str) -> dict[str, Any]:
        await self.stop(profile_id)
        return await self.start(profile_id)

    async def start_many(self, profile_ids: list[str], starting_limit: int | None = None) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(starting_limit) if starting_limit else None

        async def one(profile_id: str) -> dict[str, Any]:
            if semaphore is None:
                return await self.start(profile_id)
            async with semaphore:
                return await self.start(profile_id)

        return await asyncio.gather(*(one(profile_id) for profile_id in profile_ids), return_exceptions=True)

    async def stop_all(self) -> list[dict[str, Any]]:
        running = [p["id"] for p in self._records.values() if p.get("status") in {"running", "starting", "error"}]
        return await asyncio.gather(*(self.stop(pid) for pid in running)) if running else []
