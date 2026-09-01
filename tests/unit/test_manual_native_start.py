import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_manager.profile_manager import ProfileManager, ProfileManagerError
from browser_manager.server import PROFILE_STARTUP_MODE


class ManualNativeStartTests(unittest.IsolatedAsyncioTestCase):
    def test_product_startup_status_names_native_manual_mode(self):
        self.assertEqual(PROFILE_STARTUP_MODE, "native_manual_default")

    async def test_manual_start_does_not_attach_cdp_and_gates_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fixture-browser.exe"
            executable.write_bytes(b"public-fixture")
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = ProfileManager(
                    object(),
                    registry_path=root / "registry.json",
                    profiles_root=root / "profiles",
                    browser_executable=executable,
                    ready_url="http://127.0.0.1:1/ready",
                    background_mode=True,
                    max_retries=0,
                    download_root=root / "downloads",
                )
            self.assertFalse(manager.automation_enabled)
            manager.create("Profile-01")
            manager._ensure_profile_processes_absent = AsyncMock()
            manager._wait_for_processes = AsyncMock(return_value=[{"ProcessId": 1234, "Name": "chrome.exe", "CommandLine": "chrome.exe"}])
            manager._monitor = lambda _profile_id: asyncio.sleep(3600)
            fake_process = SimpleNamespace(pid=1234)
            with patch("browser_manager.profile_manager.subprocess.Popen", return_value=fake_process) as popen:
                with patch("browser_manager.profile_manager.foreground_snapshot", return_value={"hwnd": 0, "pid": 0}):
                    with patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                        result = await manager.start("Profile-01")
            arguments = popen.call_args.args[0]
            self.assertFalse(any(argument.startswith("--remote-debugging") for argument in arguments))
            self.assertFalse(result["automation_attached"])
            self.assertEqual(result["status"], "running")
            with self.assertRaisesRegex(ProfileManagerError, "automation_not_attached"):
                manager.context_for("Profile-01")
            monitor = manager._runtime["Profile-01"]["monitor"]
            monitor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await monitor


if __name__ == "__main__":
    unittest.main()
