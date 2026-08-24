"""Small permission policy layer used by the ProfileManager.

Only explicit allow/block policies are accepted in the managed startup path.
The intentionally unsupported ``ask`` state must never silently reach a
Profile that is being started for automation.
"""
from __future__ import annotations

from typing import Any


class PermissionPolicyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PermissionManager:
    SUPPORTED = {"notifications", "local-network-access"}
    SETTINGS = {"allow", "block"}

    def __init__(self) -> None:
        self._policies: dict[str, dict[str, dict[str, str]]] = {}

    def configure(self, profile_id: str, origin: str, policy: dict[str, str]) -> None:
        if not origin.startswith(("http://", "https://")):
            raise PermissionPolicyError("permission_policy_invalid", f"origin must be http(s): {origin}")
        for permission, setting in policy.items():
            if permission not in self.SUPPORTED:
                raise PermissionPolicyError("permission_unsupported", permission)
            if setting not in self.SETTINGS:
                raise PermissionPolicyError("permission_unsupported", f"{permission}={setting}; ask is not allowed in managed startup")
        self._policies.setdefault(profile_id, {})[origin] = dict(policy)

    def policies_for(self, profile_id: str) -> dict[str, dict[str, str]]:
        return {origin: dict(policy) for origin, policy in self._policies.get(profile_id, {}).items()}

    async def apply(self, profile_id: str, context: Any) -> list[dict[str, Any]]:
        policies = self._policies.get(profile_id, {})
        try:
            await context.clear_permissions()
            applied: list[dict[str, Any]] = []
            for origin, entries in policies.items():
                allowed = [permission for permission, setting in entries.items() if setting == "allow"]
                # Playwright's allowlist is the supported Chromium path for
                # this version: omitted permissions are denied for the origin.
                await context.grant_permissions(allowed, origin=origin)
                for permission, setting in entries.items():
                    applied.append({
                        "origin": origin,
                        "permission": permission,
                        "setting": setting,
                        "mechanism": "playwright.grant_permissions_allowlist",
                        "allowlist": allowed,
                    })
            return applied
        except PermissionPolicyError:
            raise
        except Exception as exc:
            raise PermissionPolicyError("permission_apply_failed", f"{type(exc).__name__}: {exc}") from exc
