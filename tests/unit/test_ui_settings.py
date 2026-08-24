import tempfile
import unittest
from pathlib import Path

from browser_manager.local_management_ui import UISettingsStore, _safe_static


class UISettingsTests(unittest.TestCase):
    def test_settings_are_presentation_only_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            store = UISettingsStore(path)
            created = []

            def create(profile_id, name):
                created.append((profile_id, name))
                return {"id": profile_id, "name": name}

            records = store.bootstrap_presets(create, [])
            self.assertEqual(len(created), 3)
            self.assertEqual(len(records), 3)
            saved = store.update(
                "Profile-01",
                {
                    "display_name": "工作档案",
                    "color": "#123456",
                    "note": "local-only",
                    "shortcuts": [{"name": "Example", "url": "https://example.com"}],
                },
                records,
            )
            self.assertEqual(saved["display_name"], "工作档案")
            reloaded = UISettingsStore(path)
            self.assertEqual(reloaded.get_all(records)["profiles"]["Profile-01"]["color"], "#123456")

    def test_static_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("ok", encoding="utf-8")
            self.assertIsNotNone(_safe_static(root, "/assets/index.html"))
            self.assertIsNone(_safe_static(root, "/assets/../secret.txt"))


if __name__ == "__main__":
    unittest.main()
