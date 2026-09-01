import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_manager.profile_manager import ProfileManager, ProfileManagerError


class _Credentials:
    def get(self, _profile_id):
        return {"username": "fixture-user", "password": "fixture-password"}


class PublicR1Tests(unittest.TestCase):
    def _manager(self, root: Path, *, playwright=object(), background_mode=True):
        executable = root / "fixture-browser.exe"
        executable.write_bytes(b"fixture-browser-v1")
        with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
            return ProfileManager(
                playwright,
                registry_path=root / "registry.json",
                profiles_root=root / "profiles",
                browser_executable=executable,
                ready_url="http://127.0.0.1:1/ready",
                ready_title="Ready",
                max_retries=0,
                background_mode=background_mode,
                automation_enabled=True,
                proxy_credentials=_Credentials(),
            )

    def test_profile_records_persist_seed_but_all_public_views_redact_it(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            created = manager.create("Profile-01")
            listed = manager.list()[0]
            loaded = manager.get("Profile-01")
            raw = manager._records["Profile-01"]["fingerprint"]

            self.assertIn("seed", raw)
            self.assertIn("engine_sha256", raw)
            for public_record in (created, listed, loaded):
                encoded = json.dumps(public_record)
                self.assertEqual(public_record["fingerprint"]["engine"], "pending_first_start")
                self.assertNotIn("seed", public_record["fingerprint"])
                self.assertNotIn("engine_sha256", encoded)

    def test_executable_digest_is_pinned_and_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self._manager(root)
            manager.create("Profile-01")
            record = manager._records["Profile-01"]

            manager._ensure_fingerprint_engine(record)
            self.assertEqual(len(record["fingerprint"]["engine_sha256"]), 64)
            (root / "fixture-browser.exe").write_bytes(b"fixture-browser-v2")
            with self.assertRaisesRegex(ProfileManagerError, "fingerprint_engine_hash_mismatch"):
                manager._ensure_fingerprint_engine(record)

    def test_http_and_https_basic_proxy_configs_build_launch_time_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory), background_mode=False)
            manager.create("Profile-01")
            record = manager._records["Profile-01"]
            for scheme in ("http", "https"):
                record["network"] = {
                    "mode": "fixed",
                    "scheme": scheme,
                    "host": "proxy.example.test",
                    "port": 8443,
                    "authentication": "basic",
                }
                proxy = manager._persistent_proxy_for("Profile-01", record)
                self.assertEqual(proxy["server"], f"{scheme}://proxy.example.test:8443")
                self.assertEqual(set(proxy), {"server", "username", "password"})


class PublicR1AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_proxy_uses_persistent_launch_without_credentials_in_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fixture-browser.exe"
            executable.write_bytes(b"fixture-browser-v1")
            page = SimpleNamespace(
                goto=AsyncMock(),
                title=AsyncMock(return_value="Ready"),
                evaluate=AsyncMock(return_value="complete"),
            )
            context = SimpleNamespace(pages=[page], close=AsyncMock())
            launch = AsyncMock(return_value=context)
            playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            with patch("browser_manager.profile_manager.query_profile_processes_strict", return_value=[]):
                manager = ProfileManager(
                    playwright,
                    registry_path=root / "registry.json",
                    profiles_root=root / "profiles",
                    browser_executable=executable,
                    ready_url="http://127.0.0.1:1/ready",
                    ready_title="Ready",
                    max_retries=0,
                    background_mode=True,
                    automation_enabled=True,
                    proxy_credentials=_Credentials(),
                )
            manager.create("Profile-01")
            manager.configure_network("Profile-01", {
                "mode": "fixed",
                "scheme": "http",
                "host": "proxy.example.test",
                "port": 8080,
                "authentication": "basic",
            })
            manager._ensure_profile_processes_absent = AsyncMock()
            manager._wait_for_processes = AsyncMock(return_value=[{
                "ProcessId": 1001,
                "Name": "fixture-browser.exe",
                "CommandLine": "fixture-browser.exe --user-data-dir=fixture",
            }])
            manager._monitor = lambda _profile_id: asyncio.sleep(3600)

            with patch("browser_manager.profile_manager.query_profile_windows", return_value=[]):
                result = await manager.start("Profile-01")

            kwargs = launch.await_args.kwargs
            self.assertEqual(kwargs["proxy"]["server"], "http://proxy.example.test:8080")
            self.assertTrue(any(argument.startswith("--fingerprint=") for argument in kwargs["args"]))
            self.assertFalse(any(argument.startswith("--proxy-server") for argument in kwargs["args"]))
            self.assertNotIn("seed", result["fingerprint"])
            page.goto.assert_awaited_once_with("about:blank", wait_until="domcontentloaded", timeout=30_000)

            monitor = manager._runtime["Profile-01"]["monitor"]
            monitor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await monitor


if __name__ == "__main__":
    unittest.main()
