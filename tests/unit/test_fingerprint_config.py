import unittest
from unittest.mock import patch

from browser_manager.fingerprint_config import (
    fingerprint_launch_args,
    new_fixed_fingerprint,
    normalize_fingerprint,
    public_fingerprint_status,
    without_fingerprint_arguments,
)


class FingerprintConfigTests(unittest.TestCase):
    def test_new_seed_is_uint32_and_avoids_existing_values(self):
        with patch("browser_manager.fingerprint_config.secrets.randbits", side_effect=[7, 11]):
            created = new_fixed_fingerprint({7})
        self.assertEqual(created["seed"], 11)
        self.assertEqual(created["engine_sha256"], None)

    def test_normalization_rejects_invalid_seed_and_digest(self):
        valid = {
            "mode": "fixed",
            "schema_version": 1,
            "seed": 42,
            "engine_sha256": "a" * 64,
        }
        self.assertEqual(normalize_fingerprint(valid), valid)
        with self.assertRaisesRegex(ValueError, "fingerprint_seed_must_be_uint32"):
            normalize_fingerprint({**valid, "seed": True})
        with self.assertRaisesRegex(ValueError, "fingerprint_engine_sha256_invalid"):
            normalize_fingerprint({**valid, "engine_sha256": "not-a-digest"})

    def test_launch_argument_overrides_only_the_exact_fingerprint_switch(self):
        config = {
            "mode": "fixed",
            "schema_version": 1,
            "seed": 42,
            "engine_sha256": None,
        }
        arguments = without_fingerprint_arguments([
            "--fingerprint=9",
            "--fingerprinting-policy=keep",
            "--no-first-run",
        ])
        self.assertEqual(arguments, ["--fingerprinting-policy=keep", "--no-first-run"])
        self.assertEqual(fingerprint_launch_args(config), ["--fingerprint=42"])

    def test_public_status_never_contains_seed_or_executable_digest(self):
        config = {
            "mode": "fixed",
            "schema_version": 1,
            "seed": 42,
            "engine_sha256": "b" * 64,
        }
        status = public_fingerprint_status(config)
        self.assertEqual(status, {"mode": "fixed", "engine": "locked"})
        self.assertNotIn("seed", status)
        self.assertNotIn("sha256", repr(status))


if __name__ == "__main__":
    unittest.main()
