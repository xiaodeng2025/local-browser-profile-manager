import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_manager.data_root import (
    DATA_ROOT_MARKER,
    DataRootError,
    require_safe_product_data_root,
)


class DataRootGuardTests(unittest.TestCase):
    def test_random_ports_allow_isolated_test_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(require_safe_product_data_root(root, api_port=0, ui_port=0), root.resolve())

    def test_default_ports_initialize_a_fresh_canonical_root_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "profile-data"
            with patch("browser_manager.data_root.canonical_data_root", return_value=root.resolve()):
                self.assertEqual(require_safe_product_data_root(root, api_port=17321, ui_port=17322), root.resolve())
                self.assertEqual(
                    json.loads((root / ".browser-profile-manager-data-root.json").read_text(encoding="utf-8")),
                    DATA_ROOT_MARKER,
                )
                # A second normal startup validates the existing marker rather
                # than silently initializing it again.
                self.assertEqual(require_safe_product_data_root(root, api_port=17321, ui_port=17322), root.resolve())

    def test_default_ports_reject_a_different_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("browser_manager.data_root.canonical_data_root", return_value=root / "profile-data"):
                with self.assertRaisesRegex(DataRootError, "noncanonical_product_data_root"):
                    require_safe_product_data_root(root / "other", api_port=17321, ui_port=17322)

    def test_existing_canonical_root_without_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "profile-data"
            root.mkdir()
            with patch("browser_manager.data_root.canonical_data_root", return_value=root.resolve()):
                with self.assertRaisesRegex(DataRootError, "missing_product_data_root_marker"):
                    require_safe_product_data_root(root, api_port=17321, ui_port=17322)


if __name__ == "__main__":
    unittest.main()
