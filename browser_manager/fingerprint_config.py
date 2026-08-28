"""Per-Profile fixed fingerprint seed configuration.

The seed is persisted as private local state and supplied only as a browser
launch argument. Public status helpers intentionally expose neither the seed
nor the executable digest.
"""
from __future__ import annotations

import secrets
from typing import Any


FINGERPRINT_ARGUMENT = "--fingerprint"
FINGERPRINT_SCHEMA_VERSION = 1


def new_fixed_fingerprint(existing_seeds: set[int] | None = None) -> dict[str, Any]:
    used = existing_seeds if existing_seeds is not None else set()
    while True:
        seed = secrets.randbits(32)
        if seed not in used:
            return {
                "mode": "fixed",
                "schema_version": FINGERPRINT_SCHEMA_VERSION,
                "seed": seed,
                "engine_sha256": None,
            }


def normalize_fingerprint(value: Any) -> dict[str, Any]:
    expected_fields = {"mode", "schema_version", "seed", "engine_sha256"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("fingerprint_invalid_shape")
    if value.get("mode") != "fixed" or value.get("schema_version") != FINGERPRINT_SCHEMA_VERSION:
        raise ValueError("fingerprint_unsupported")
    seed = value.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("fingerprint_seed_must_be_uint32")
    digest = value.get("engine_sha256")
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("fingerprint_engine_sha256_invalid")
    return {
        "mode": "fixed",
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "seed": seed,
        "engine_sha256": digest,
    }


def fingerprint_launch_args(config: dict[str, Any]) -> list[str]:
    return [f"{FINGERPRINT_ARGUMENT}={normalize_fingerprint(config)['seed']}"]


def without_fingerprint_arguments(arguments: list[str]) -> list[str]:
    return [
        item
        for item in arguments
        if item != FINGERPRINT_ARGUMENT and not item.startswith(f"{FINGERPRINT_ARGUMENT}=")
    ]


def public_fingerprint_status(config: dict[str, Any]) -> dict[str, str]:
    value = normalize_fingerprint(config)
    return {
        "mode": "fixed",
        "engine": "locked" if value["engine_sha256"] else "pending_first_start",
    }
