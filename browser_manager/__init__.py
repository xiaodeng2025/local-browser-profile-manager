"""Formal product package for the local browser profile manager."""

from .api import LocalProfileAPI, LocatorResolutionError, WindowControlError
from .permission_manager import PermissionManager, PermissionPolicyError
from .profile_manager import ProfileManager, ProfileManagerError

__all__ = [
    "LocalProfileAPI",
    "LocatorResolutionError",
    "PermissionManager",
    "PermissionPolicyError",
    "ProfileManager",
    "ProfileManagerError",
    "WindowControlError",
]
