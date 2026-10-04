"""Role and permission checks. Visibility is never treated as authorization."""

from __future__ import annotations

from typing import Optional

from admin.db import get_admin_role_from_table
from app.config import load_bootstrap_config

ROLES = ("owner", "admin", "moderator", "broadcaster")
_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"dashboard", "downloads", "users", "groups", "moderation", "storage", "broadcast", "buttons", "settings", "system", "log", "manage_admins"},
    "admin": {"dashboard", "downloads", "users", "groups", "moderation", "storage", "broadcast", "buttons", "settings", "system", "log"},
    "moderator": {"dashboard", "downloads", "users", "groups", "moderation", "log"},
    "broadcaster": {"dashboard", "broadcast"},
}


def bootstrap_owner_ids() -> frozenset[int]:
    return load_bootstrap_config(require_token=False).owner_ids


async def get_admin_role(bot, telegram_id: int) -> Optional[str]:
    config = load_bootstrap_config(require_token=False)
    if telegram_id in config.owner_ids:
        return "owner"
    role = get_admin_role_from_table(telegram_id)
    if role:
        return role
    if config.admin_group_id:
        try:
            member = await bot.get_chat_member(config.admin_group_id, telegram_id)
            if member.status in ("creator", "administrator"):
                return "admin"
        except Exception:
            return None
    return None


def is_admin(role: Optional[str]) -> bool:
    return role in ROLES


def has_permission(role: Optional[str], action: str) -> bool:
    return action in _PERMISSIONS.get(role or "", set())


def permission_matrix() -> dict[str, set[str]]:
    return {role: set(actions) for role, actions in _PERMISSIONS.items()}
