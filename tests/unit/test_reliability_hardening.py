import multiprocessing
import asyncio
import json
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from unittest.mock import patch

from browser_manager.instance_lock import DataDirInstanceLock, DataDirLockError
from browser_manager.profile_manager import ProfileManager, ProfileManagerError, ProfileProcessProbeError, profile_process_matches, query_profile_processes


class _FakePage:
    async def goto(self, *_args, **_kwargs):
        return None

    async def title(self):
        return ""

    async def evaluate(self, *_args, **_kwargs):
        return "complete"


class _FakeContext:
    pages: list = []

    async def close(self):
        return None


def _hold_data_dir_lock(data_dir: str, ready, release) -> None:
    lock = DataDirInstanceLock(Path(data_dir))
    try:
        lock.acquire()
        ready.set()
        release.wait(10)
    finally:
        lock.release()


class ReliabilityHardeningTests(unittest.TestCase):
    @staticmethod
    def _record(root: Path, profile_id: str, status: str) -> dict:
        return {
            "id": profile_id,
            "name": profile_id,
            "user_data_dir": str((root / profile_id).resolve()),
            "browser_executable": str((root / "chrome.exe").resolve()),
            "status": status,
            "process_ids": [],
            "retry_count": 0,
            "last_ready_at": None,
            "last_error": None,
            "root_pid": None,
            "debug_endpoint": None,
            "startup_stage": status,
            "project_hwnds": [],
            "permission_policy": {},
            "target_id": None,
            "startup_trace": [],
            "network": {"mode": "direct"},
        }

    def _manager(self, root: Path, records: list[dict]) -> ProfileManager:
        registry = root / "registry.json"
        registry.write_text(json.dumps({"version": 1, "profiles": records}), encoding="utf-8")
        return ProfileManager(
            object(),
            registry_path=registry,
            profiles_root=root / "profiles",
            browser_executable=root / "chrome.exe",
            ready_url="http://127.0.0.1:1/",
            max_retries=0,
            permission_manager=None,
        )

    def test_manager_restart_marks_running_live_profile_recovery_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-01", "running")
            process = {"ProcessId": 101, "Name": "chrome.exe", "CommandLine": "synthetic"}
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[process]):
                manager = self._manager(root, [record])
            loaded = manager.get("Profile-01")
            self.assertEqual(loaded["status"], "error")
            self.assertEqual(loaded["last_error"], "recovery_required:live_profile_processes")
            self.assertEqual(loaded["process_ids"], [101])

    def test_manager_restart_marks_starting_live_profile_recovery_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-01", "starting")
            process = {"ProcessId": 102, "Name": "chrome.exe", "CommandLine": "synthetic"}
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[process]):
                manager = self._manager(root, [record])
            loaded = manager.get("Profile-01")
            self.assertEqual(loaded["status"], "error")
            self.assertEqual(loaded["last_error"], "recovery_required:live_profile_processes")

    def test_stopped_profile_with_live_process_rejects_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-01", "stopped")
            process = {"ProcessId": 103, "Name": "chrome.exe", "CommandLine": "synthetic"}
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[process]):
                manager = self._manager(root, [record])
                with self.assertRaises(ProfileManagerError) as raised:
                    __import__("asyncio").run(manager.start("Profile-01"))
            self.assertIn("recovery_required:live_profile_processes", str(raised.exception))

    def test_stopped_profile_without_process_reaches_normal_start_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-01", "stopped")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = self._manager(root, [record])
            launch = AsyncMock(side_effect=RuntimeError("launch_path_reached"))
            manager._launch_background_runtime = launch
            manager._wait_for_processes = AsyncMock(return_value=[])
            with self.assertRaises(ProfileManagerError) as raised:
                __import__("asyncio").run(manager.start("Profile-01"))
            self.assertIn("launch_path_reached", str(raised.exception))
            self.assertEqual(launch.await_count, 1)

    def test_live_profile_10_does_not_block_stopped_profile_100(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                self._record(root, "Profile-10", "stopped"),
                self._record(root, "Profile-100", "stopped"),
            ]

            def strict_probe(path: Path):
                if path.name == "Profile-10":
                    return [{"ProcessId": 110, "Name": "chrome.exe", "CommandLine": "synthetic"}]
                return []

            with patch("browser_manager.profile_manager.query_profile_processes_strict", side_effect=strict_probe):
                manager = self._manager(root, records)
            self.assertEqual(manager.get("Profile-10")["status"], "error")
            self.assertEqual(manager.get("Profile-100")["status"], "stopped")

            launch = AsyncMock(side_effect=RuntimeError("profile_100_launch_path_reached"))
            manager._launch_background_runtime = launch
            manager._wait_for_processes = AsyncMock(return_value=[])
            with patch("browser_manager.profile_manager.query_profile_processes_strict", side_effect=strict_probe):
                with self.assertRaises(ProfileManagerError) as raised:
                    __import__("asyncio").run(manager.start("Profile-100"))
                self.assertIn("profile_100_launch_path_reached", str(raised.exception))
                self.assertEqual(launch.await_count, 1)

                with self.assertRaises(ProfileManagerError) as raised:
                    __import__("asyncio").run(manager.start("Profile-10"))
            self.assertIn("recovery_required:live_profile_processes", str(raised.exception))
            self.assertEqual(launch.await_count, 1)

    def test_recovery_required_with_live_process_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record["last_error"] = "recovery_required:live_profile_processes"
            record["root_pid"] = 410
            process = {"ProcessId": 411, "Name": "chrome.exe", "CommandLine": "synthetic"}
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[process]):
                manager = self._manager(root, [record])
                loaded = manager.get("Profile-10")
                self.assertEqual(loaded["status"], "error")
                self.assertEqual(loaded["last_error"], "recovery_required:live_profile_processes")
                self.assertEqual(loaded["process_ids"], [411])
                with self.assertRaises(ProfileManagerError):
                    __import__("asyncio").run(manager.start("Profile-10"))

    def test_recovery_required_with_empty_probe_resets_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record.update({
                "last_error": "recovery_required:live_profile_processes",
                "process_ids": [410, 411],
                "root_pid": 410,
                "debug_endpoint": "http://127.0.0.1:41000",
                "project_hwnds": [1234],
                "target_id": "stale-target",
                "startup_stage": "error",
            })
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = self._manager(root, [record])
            loaded = manager.get("Profile-10")
            self.assertEqual(loaded["status"], "stopped")
            self.assertIsNone(loaded["last_error"])
            self.assertEqual(loaded["process_ids"], [])
            self.assertIsNone(loaded["root_pid"])
            self.assertIsNone(loaded["debug_endpoint"])
            self.assertEqual(loaded["project_hwnds"], [])
            self.assertIsNone(loaded["target_id"])
            self.assertEqual(loaded["startup_stage"], "stopped")

    def test_recovery_required_empty_probe_allows_normal_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record["last_error"] = "recovery_required:live_profile_processes"
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = self._manager(root, [record])
            launch = AsyncMock(side_effect=RuntimeError("recovered_launch_path_reached"))
            manager._launch_background_runtime = launch
            manager._wait_for_processes = AsyncMock(return_value=[])
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                with self.assertRaises(ProfileManagerError) as raised:
                    __import__("asyncio").run(manager.start("Profile-10"))
            self.assertIn("recovered_launch_path_reached", str(raised.exception))
            self.assertEqual(launch.await_count, 1)

    def test_recovery_required_probe_failure_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record["last_error"] = "recovery_required:live_profile_processes"
            probe_error = ProfileProcessProbeError("synthetic_probe_failure")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", side_effect=probe_error):
                manager = self._manager(root, [record])
                loaded = manager.get("Profile-10")
                self.assertEqual(loaded["status"], "error")
                self.assertIn("recovery_required:process_probe_failed", loaded["last_error"])
                with self.assertRaises(ProfileManagerError) as raised:
                    __import__("asyncio").run(manager.start("Profile-10"))
            self.assertIn("recovery_required:process_probe_failed", str(raised.exception))

    def test_orphan_cleanup_then_manager_restart_restores_startable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record.update({"last_error": "recovery_required:live_profile_processes", "process_ids": [510], "root_pid": 510})
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = self._manager(root, [record])
            loaded = manager.get("Profile-10")
            self.assertEqual(loaded["status"], "stopped")
            self.assertIsNone(loaded["last_error"])

    def test_recovery_cleanup_for_profile_10_does_not_change_profile_100(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_10 = self._record(root, "Profile-10", "error")
            profile_10["last_error"] = "recovery_required:live_profile_processes"
            profile_10["process_ids"] = [610]
            profile_100 = self._record(root, "Profile-100", "stopped")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = self._manager(root, [profile_10, profile_100])
            self.assertEqual(manager.get("Profile-10")["status"], "stopped")
            self.assertIsNone(manager.get("Profile-10")["last_error"])
            self.assertEqual(manager.get("Profile-100")["status"], "stopped")
            self.assertIsNone(manager.get("Profile-100")["last_error"])

    def test_concurrent_start_start_same_profile_launches_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "stopped")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]), patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                manager = self._manager(root, [record])

                async def scenario():
                    launch_entered = asyncio.Event()
                    release_launch = asyncio.Event()
                    launch_count = 0
                    root_pid = 12010

                    async def launch(profile_id, current_record):
                        nonlocal launch_count
                        launch_count += 1
                        current_record["root_pid"] = root_pid
                        launch_entered.set()
                        await release_launch.wait()
                        return {"context": _FakeContext(), "browser": None, "cdp": None, "page": _FakePage(), "root_pid": root_pid, "target_id": None}

                    async def wait_for_processes(_profile_id, present, timeout=10.0):
                        return [{"ProcessId": root_pid, "Name": "chrome.exe", "CommandLine": "synthetic"}] if present else []

                    manager._launch_background_runtime = launch
                    manager._wait_for_processes = wait_for_processes
                    manager._monitor = lambda _profile_id: asyncio.sleep(3600)
                    first = asyncio.create_task(manager.start("Profile-10"))
                    await asyncio.wait_for(launch_entered.wait(), 1)
                    second = asyncio.create_task(manager.start("Profile-10"))
                    await asyncio.sleep(0)
                    release_launch.set()
                    first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), 2)
                    self.assertEqual(launch_count, 1)
                    self.assertEqual(len(manager._runtime), 1)
                    self.assertEqual(first_result["status"], "running")
                    self.assertEqual(second_result["status"], "running")
                    await manager.stop("Profile-10")

                asyncio.run(scenario())

    def test_concurrent_start_stop_same_profile_serializes_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "stopped")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]), patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                manager = self._manager(root, [record])

                async def scenario():
                    launch_entered = asyncio.Event()
                    release_launch = asyncio.Event()
                    launch_count = 0
                    root_pid = 12011

                    async def launch(profile_id, current_record):
                        nonlocal launch_count
                        launch_count += 1
                        current_record["root_pid"] = root_pid
                        launch_entered.set()
                        await release_launch.wait()
                        return {"context": _FakeContext(), "browser": None, "cdp": None, "page": _FakePage(), "root_pid": root_pid, "target_id": None}

                    async def wait_for_processes(_profile_id, present, timeout=10.0):
                        return [{"ProcessId": root_pid, "Name": "chrome.exe", "CommandLine": "synthetic"}] if present else []

                    manager._launch_background_runtime = launch
                    manager._wait_for_processes = wait_for_processes
                    manager._monitor = lambda _profile_id: asyncio.sleep(3600)
                    start_task = asyncio.create_task(manager.start("Profile-10"))
                    await asyncio.wait_for(launch_entered.wait(), 1)
                    stop_task = asyncio.create_task(manager.stop("Profile-10"))
                    await asyncio.sleep(0)
                    release_launch.set()
                    start_result, stop_result = await asyncio.wait_for(asyncio.gather(start_task, stop_task), 2)
                    self.assertEqual(launch_count, 1)
                    self.assertEqual(start_result["status"], "running")
                    self.assertEqual(stop_result["status"], "stopped")
                    self.assertEqual(manager.get("Profile-10")["status"], "stopped")
                    self.assertEqual(manager._runtime, {})

                asyncio.run(scenario())

    def test_concurrent_restart_start_same_profile_serializes_without_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "stopped")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]), patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                manager = self._manager(root, [record])

                async def scenario():
                    launch_entered = asyncio.Event()
                    release_launch = asyncio.Event()
                    launch_count = 0
                    root_pid = 12012

                    async def launch(profile_id, current_record):
                        nonlocal launch_count
                        launch_count += 1
                        current_record["root_pid"] = root_pid
                        launch_entered.set()
                        await release_launch.wait()
                        return {"context": _FakeContext(), "browser": None, "cdp": None, "page": _FakePage(), "root_pid": root_pid, "target_id": None}

                    async def wait_for_processes(_profile_id, present, timeout=10.0):
                        return [{"ProcessId": root_pid, "Name": "chrome.exe", "CommandLine": "synthetic"}] if present else []

                    manager._launch_background_runtime = launch
                    manager._wait_for_processes = wait_for_processes
                    manager._monitor = lambda _profile_id: asyncio.sleep(3600)
                    restart_task = asyncio.create_task(manager.restart("Profile-10"))
                    await asyncio.wait_for(launch_entered.wait(), 1)
                    start_task = asyncio.create_task(manager.start("Profile-10"))
                    await asyncio.sleep(0)
                    release_launch.set()
                    restart_result, start_result = await asyncio.wait_for(asyncio.gather(restart_task, start_task), 2)
                    self.assertEqual(launch_count, 1)
                    self.assertEqual(restart_result["status"], "running")
                    self.assertEqual(start_result["status"], "running")
                    self.assertEqual(len(manager._runtime), 1)
                    await manager.stop("Profile-10")

                asyncio.run(scenario())

    def test_concurrent_stop_stop_same_profile_shutdowns_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "running")
            record["root_pid"] = 12013
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]), patch("browser_manager.profile_manager.query_profile_processes", return_value=[]), patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                manager = self._manager(root, [record])
                manager._records["Profile-10"].update({"status": "running", "process_ids": [12013], "startup_stage": "running"})

                async def scenario():
                    monitor = asyncio.create_task(asyncio.sleep(3600))
                    manager._runtime["Profile-10"] = {"monitor": monitor, "root_pid": 12013}
                    shutdown_entered = asyncio.Event()
                    release_shutdown = asyncio.Event()
                    shutdown_count = 0

                    async def shutdown(_profile_id, _runtime, fallback=True):
                        nonlocal shutdown_count
                        shutdown_count += 1
                        shutdown_entered.set()
                        await release_shutdown.wait()
                        return {"graceful": True, "fallback_pids": [], "remaining_processes": [], "remaining_hwnds": []}

                    manager._shutdown_runtime = shutdown
                    first = asyncio.create_task(manager.stop("Profile-10"))
                    await asyncio.wait_for(shutdown_entered.wait(), 1)
                    second = asyncio.create_task(manager.stop("Profile-10"))
                    await asyncio.sleep(0)
                    release_shutdown.set()
                    first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), 2)
                    self.assertEqual(shutdown_count, 1)
                    self.assertEqual(first_result["status"], "stopped")
                    self.assertEqual(second_result["status"], "stopped")
                    self.assertEqual(manager._runtime, {})

                asyncio.run(scenario())

    def test_different_profiles_start_can_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [self._record(root, "Profile-10", "stopped"), self._record(root, "Profile-100", "stopped")]
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]), patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                manager = self._manager(root, records)

                async def scenario():
                    entered = {"Profile-10": asyncio.Event(), "Profile-100": asyncio.Event()}
                    release = asyncio.Event()
                    roots = {"Profile-10": 12010, "Profile-100": 12100}

                    async def launch(profile_id, current_record):
                        current_record["root_pid"] = roots[profile_id]
                        entered[profile_id].set()
                        await release.wait()
                        return {"context": _FakeContext(), "browser": None, "cdp": None, "page": _FakePage(), "root_pid": roots[profile_id], "target_id": None}

                    async def wait_for_processes(profile_id, present, timeout=10.0):
                        return [{"ProcessId": roots[profile_id], "Name": "chrome.exe", "CommandLine": "synthetic"}] if present else []

                    manager._launch_background_runtime = launch
                    manager._wait_for_processes = wait_for_processes
                    manager._monitor = lambda _profile_id: asyncio.sleep(3600)
                    first = asyncio.create_task(manager.start("Profile-10"))
                    second = asyncio.create_task(manager.start("Profile-100"))
                    await asyncio.wait_for(asyncio.gather(entered["Profile-10"].wait(), entered["Profile-100"].wait()), 1)
                    release.set()
                    first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), 2)
                    self.assertEqual(first_result["status"], "running")
                    self.assertEqual(second_result["status"], "running")
                    self.assertEqual(set(manager._runtime), {"Profile-10", "Profile-100"})
                    await asyncio.gather(manager.stop("Profile-10"), manager.stop("Profile-100"))

                asyncio.run(scenario())

    def test_recovery_required_concurrent_starts_remain_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root, "Profile-10", "error")
            record["last_error"] = "recovery_required:live_profile_processes"
            process = {"ProcessId": 12014, "Name": "chrome.exe", "CommandLine": "synthetic"}
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[process]):
                manager = self._manager(root, [record])

                async def scenario():
                    launch = AsyncMock()
                    manager._launch_background_runtime = launch
                    results = await asyncio.gather(
                        manager.start("Profile-10"),
                        manager.start("Profile-10"),
                        return_exceptions=True,
                    )
                    self.assertTrue(all(isinstance(result, ProfileManagerError) for result in results))
                    self.assertEqual(launch.await_count, 0)
                    self.assertEqual(manager.get("Profile-10")["status"], "error")
                    self.assertEqual(manager.get("Profile-10")["last_error"], "recovery_required:live_profile_processes")

                asyncio.run(scenario())

    def test_same_data_dir_is_exclusive_across_processes_and_releases(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_data_dir_lock,
                args=(directory, ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(10), "lock holder did not start")
            try:
                with self.assertRaises(DataDirLockError):
                    DataDirInstanceLock(Path(str(directory).upper()) / "").acquire()
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)

            lock = DataDirInstanceLock(Path(directory))
            lock.acquire()
            lock.release()

    def test_user_data_dir_matching_handles_quotes_case_and_trailing_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Program Files" / "Profiles"
            target = root / "Profile-10"
            target.mkdir(parents=True)
            command_line = subprocess.list2cmdline(
                ["chrome.exe", f"--user-data-dir={str(target).upper()}\\"]
            )
            self.assertTrue(profile_process_matches(command_line, target))

    def test_profile_10_does_not_match_profile_100(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Profiles"
            target = root / "Profile-10"
            other = root / "Profile-100"
            self.assertFalse(
                profile_process_matches(
                    f'chrome.exe --user-data-dir="{other}"',
                    target,
                )
            )

    def test_user_data_dir_matching_accepts_separate_argument_form(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Profile 10"
            command_line = f'chrome.exe --user-data-dir "{target}"'
            self.assertTrue(profile_process_matches(command_line, target))

    def test_query_profile_processes_filters_by_parsed_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Profile-10"
            raw = json.dumps([
                {"ProcessId": 10, "Name": "chrome.exe", "CommandLine": f'chrome.exe --user-data-dir="{target}"'},
                {"ProcessId": 100, "Name": "chrome.exe", "CommandLine": f'chrome.exe --user-data-dir="{target}0"'},
            ])
            with patch("browser_manager.profile_manager.subprocess.check_output", return_value=raw) as check_output:
                result = query_profile_processes(target)
            self.assertEqual([item["ProcessId"] for item in result], [10])
            command = check_output.call_args.args[0][-1]
            self.assertNotIn("Contains($root)", command)


if __name__ == "__main__":
    unittest.main()
