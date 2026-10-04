from __future__ import annotations

import logging

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from admin.auth import get_admin_role, has_permission, is_admin
from admin.keyboards import main_menu_kb


class AdminFilter(Filter):
    """
    Пропускает апдейт дальше только если отправитель — админ (любая роль).
    Разграничение прав ПО РАЗДЕЛАМ (has_permission) делается внутри
    конкретных хендлеров, не здесь — иначе пришлось бы дублировать
    список разделов в фильтре.

    При отказе ничего не отвечаем: чтобы не палить посторонним
    пользователям сам факт существования админ-команд.
    """

    async def __call__(self, event: Message | CallbackQuery, bot) -> bool:
        user = event.from_user
        if user is None:
            return False
        role = await get_admin_role(bot, user.id)
        return is_admin(role)


admin_router = Router(name="admin")
admin_router.message.filter(AdminFilter())
admin_router.callback_query.filter(AdminFilter())


def _callback_permission(data: str) -> str | None:
    if data in {"admin:main"}:
        return None
    mappings = (
        (("admin:dashboard",), "dashboard"),
        (("admin:downloads", "jobadmin:"), "downloads"),
        (("admin:users", "admin:confirm:block_user"), "users"),
        (("admin:groups", "admin:confirm:block_group"), "groups"),
        (("admin:moderation", "mod:"), "moderation"),
        (("admin:storage", "stg:", "admin:confirm:storage"), "storage"),
        (("admin:broadcast", "bc:", "admin:confirm:broadcast"), "broadcast"),
        (("admin:buttons", "btn:", "admin:confirm:button"), "buttons"),
        (("admin:settings", "aset:"), "settings"),
        (("admin:system",), "system"),
        (("admin:log",), "log"),
        (("admin:admins", "adm:", "admin:confirm:admin"), "manage_admins"),
    )
    for prefixes, permission in mappings:
        if data.startswith(prefixes):
            return permission
    return None


class AdminPermissionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        callback_data = getattr(event, "data", "") or ""
        permission = _callback_permission(callback_data)
        if permission:
            role = await get_admin_role(data["bot"], event.from_user.id)
            if not has_permission(role, permission):
                await event.answer("⛔ Недостаточно прав", show_alert=True)
                return None
        return await handler(event, data)


admin_router.callback_query.outer_middleware(AdminPermissionMiddleware())


async def require_admin_permission(
    event: Message | CallbackQuery,
    bot,
    permission: str,
    state: FSMContext | None = None,
) -> bool:
    """Re-check a permission inside multi-step message workflows."""
    user = event.from_user
    role = await get_admin_role(bot, user.id) if user else None
    if has_permission(role, permission):
        return True
    if state is not None:
        await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer("⛔ Недостаточно прав", show_alert=True)
    else:
        await event.answer("⛔ Недостаточно прав")
    return False


async def _cancel_admin_fsm(event: Message | CallbackQuery, state: FSMContext, bot) -> None:
    await state.clear()
    role = await get_admin_role(bot, event.from_user.id)
    text = "Операция отменена."
    if isinstance(event, CallbackQuery):
        await event.answer("Отменено")
        if event.message:
            await event.message.edit_text(text, reply_markup=main_menu_kb(role or "broadcaster"))
    else:
        await event.answer(text, reply_markup=main_menu_kb(role or "broadcaster"))


@admin_router.callback_query(F.data == "admin:fsm:cancel")
async def cancel_admin_fsm_callback(callback: CallbackQuery, state: FSMContext, bot) -> None:
    await _cancel_admin_fsm(callback, state, bot)


@admin_router.message(Command("cancel"))
async def cancel_admin_fsm_command(message: Message, state: FSMContext, bot) -> None:
    await _cancel_admin_fsm(message, state, bot)

logger = logging.getLogger(__name__)


@admin_router.errors()
async def admin_error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    logger.error(
        "Admin handler error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    update = event.update
    callback = getattr(update, "callback_query", None)
    if callback:
        try:
            await callback.answer("Действие устарело или содержит некорректные данные.", show_alert=True)
        except Exception:
            pass
    return True


# Импортируются после создания admin_router, т.к. регистрируют хендлеры на нём.
from admin.handlers import (  # noqa: E402,F401
    admins,
    broadcast,
    buttons,
    dashboard,
    groups,
    operations,
    storage,
    users,
)
