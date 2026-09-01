"""Canonical product data-root protection for the local service.

The normal product ports require the reviewed ``profile-data`` root and its
non-sensitive marker. Random ports remain available for isolated local tests.
"""
from __future__ import annotations

import json
from pathlib import Path


CANONICAL_DATA_ROOT_NAME = "profile-data"
DATA_ROOT_MARKER_NAME = ".browser-profile-manager-data-root.json"
DATA_ROOT_MARKER = {"product": "local-browser-profile-manager", "schema": 1}
DEFAULT_API_PORT = 17321
DEFAULT_UI_PORT = 17322


class DataRootError(ValueError):
    """Raised when the normal product service points at an unsafe root."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def canonical_data_root() -> Path:
    return (project_root() / CANONICAL_DATA_ROOT_NAME).resolve()


def marker_path(data_root: Path) -> Path:
    return Path(data_root) / DATA_ROOT_MARKER_NAME


def initialize_canonical_data_root(data_root: Path) -> None:
    resolved = Path(data_root).resolve()
    expected = canonical_data_root()
    if resolved != expected:
        raise DataRootError(f"not_canonical_data_root:{resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    marker_path(resolved).write_text(
        json.dumps(DATA_ROOT_MARKER, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def require_safe_product_data_root(data_root: Path, *, api_port: int, ui_port: int) -> Path:
    """Protect the default product root while keeping random-port tests flexible."""
    resolved = Path(data_root).resolve()
    if (api_port, ui_port) != (DEFAULT_API_PORT, DEFAULT_UI_PORT):
        return resolved
    expected = canonical_data_root()
    if resolved != expected:
        raise DataRootError(
            f"noncanonical_product_data_root:{resolved}; expected:{expected}; use the reviewed product launcher"
        )
    if not resolved.exists():
        # A fresh checkout has no ignored data directory yet. Create only the
        # exact canonical root and its non-sensitive marker; an existing root
        # without a marker remains rejected as a possible historical/test root.
        initialize_canonical_data_root(resolved)
    marker = marker_path(resolved)
    try:
        stored = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DataRootError(f"missing_product_data_root_marker:{marker}") from error
    except json.JSONDecodeError as error:
        raise DataRootError(f"invalid_product_data_root_marker:{marker}") from error
    if stored != DATA_ROOT_MARKER:
        raise DataRootError(f"unrecognized_product_data_root_marker:{marker}")
    return resolved
