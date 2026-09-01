import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from browser_manager.downloads import ProfileDownloadManager, configure_chromium_download_preferences
from browser_manager.profile_manager import ProfileManager


class FakeCdp:
    def __init__(self):
        self.handlers = {}
        self.messages = []

    def on(self, name, callback):
        self.handlers[name] = callback

    def remove_listener(self, name, callback):
        self.handlers.pop(name, None)

    async def send(self, name, payload=None):
        self.messages.append((name, payload))


class FakeDownload:
    url = "https://download.example.test/file"
    suggested_filename = "report.txt"


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def test_each_profile_resolves_to_its_own_download_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(ProfileManager)
            manager.download_root = Path(directory) / "downloads"
            first = manager._download_directory_for("Profile-01")
            second = manager._download_directory_for("Profile-02")
            self.assertNotEqual(first, second)
            self.assertTrue(str(first).endswith("Profile-01"))
            self.assertTrue(str(second).endswith("Profile-02"))

    def test_preferences_preserve_existing_profile_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "Profile-01"
            preferences = profile / "Default" / "Preferences"
            preferences.parent.mkdir(parents=True)
            preferences.write_text(json.dumps({"homepage": "https://example.test", "download": {"prompt_for_download": True}}), encoding="utf-8")
            destination = configure_chromium_download_preferences(profile, profile.parent / "downloads" / "Profile-01")
            value = json.loads(preferences.read_text(encoding="utf-8"))
            self.assertEqual(value["homepage"], "https://example.test")
            self.assertEqual(Path(value["download"]["default_directory"]).resolve(), destination)
            self.assertFalse(value["download"]["prompt_for_download"])

    async def test_completed_download_must_be_inside_profile_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "downloads" / "Profile-01"
            root.mkdir(parents=True)
            cdp = FakeCdp()
            manager = ProfileDownloadManager("Profile-01", root)
            await manager.attach(cdp)
            marker = manager.observation_marker()
            cdp.handlers["Browser.downloadWillBegin"]({"guid": "g1", "url": FakeDownload.url, "suggestedFilename": FakeDownload.suggested_filename})
            target = root / FakeDownload.suggested_filename
            target.write_text("public fixture", encoding="utf-8")
            cdp.handlers["Browser.downloadProgress"]({"guid": "g1", "state": "completed", "filePath": str(target)})
            result = await manager.wait_for_download(FakeDownload(), marker, 0.2)
            self.assertEqual(Path(result["path"]).resolve(), target.resolve())
            await manager.close()

    async def test_outside_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "downloads" / "Profile-01"
            root.mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("fixture", encoding="utf-8")
            cdp = FakeCdp()
            manager = ProfileDownloadManager("Profile-01", root)
            await manager.attach(cdp)
            marker = manager.observation_marker()
            cdp.handlers["Browser.downloadWillBegin"]({"guid": "g2", "url": FakeDownload.url, "suggestedFilename": FakeDownload.suggested_filename})
            cdp.handlers["Browser.downloadProgress"]({"guid": "g2", "state": "completed", "filePath": str(outside)})
            with self.assertRaisesRegex(Exception, "outside_profile_directory"):
                await manager.wait_for_download(FakeDownload(), marker, 0.2)
            await manager.close()


if __name__ == "__main__":
    unittest.main()
