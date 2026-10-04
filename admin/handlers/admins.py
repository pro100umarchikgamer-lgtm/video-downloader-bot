from __future__ import annotations

import time

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db
from admin.auth import bootstrap_owner_ids, get_admin_role, has_permission
from admin.handlers import admin_router
from admin.keyboards import back_button, back_kb, confirm_kb, fsm_cancel_kb


class AdminAdd(StatesGroup):
    waiting_id = State()


def _admins_kb() -> InlineKeyboardMarkup:
    rows = []
    bootstrap = bootstrap_owner_ids()
    for owner_id in sorted(bootstrap):
        rows.append([InlineKeyboardButton(text=f"👑 {owner_id} · bootstrap owner", callback_data=f"adm:view:{owner_id}")])
    for row in db.list_admins():
        if row["telegram_id"] in bootstrap:
            continue
        icon = "🟢" if row["enabled"] else "⏸"
        rows.append([InlineKeyboardButton(text=f"{icon} {row['telegram_id']} · {row['role']}", callback_data=f"adm:view:{row['telegram_id']}")])
    rows.extend([
        [InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adm:add")],
        [back_button()],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "admin:admins")
async def admins_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("👑 Администраторы", reply_markup=_admins_kb())


@admin_router.callback_query(F.data.startswith("adm:view:"))
async def admin_card(callback: CallbackQuery) -> None:
    await callback.answer()
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    if telegram_id in bootstrap_owner_ids():
        await callback.message.edit_text(
            f"👑 {telegram_id}\nBootstrap owner из ADMIN_IDS. Защищён от изменения и удаления через UI.",
            reply_markup=back_kb("admin:admins"),
        )
        return
    row = db.get_admin(telegram_id)
    if not row:
        await callback.message.edit_text("Администратор не найден.", reply_markup=back_kb("admin:admins"))
        return
    rows = [
        [InlineKeyboardButton(text=f"Роль: {role}", callback_data=f"adm:role:{telegram_id}:{role}")]
        for role in db.ROLES
    ]
    rows += [
        [InlineKeyboardButton(text="⏸ Отключить" if row["enabled"] else "▶️ Включить", callback_data=f"adm:toggle:{telegram_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:delete:{telegram_id}")],
        [back_button("admin:admins")],
    ]
    await callback.message.edit_text(
        f"Администратор {telegram_id}\nРоль: {row['role']}\nСтатус: {'enabled' if row['enabled'] else 'disabled'}\nДобавлен: {row['created_at']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_router.callback_query(F.data == "adm:add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminAdd.waiting_id)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=fsm_cancel_kb())


@admin_router.message(AdminAdd.waiting_id)
async def admin_add_id(message: Message, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, message.from_user.id)
    if not has_permission(role, "manage_admins"):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    value = (message.text or "").strip()
    if time.time() - data.get("started_at", 0) > 600:
        await message.answer("Состояние добавления истекло.", reply_markup=back_kb("admin:admins"))
        return
    if not value.isdigit():
        await message.answer("Нужен положительный числовой Telegram ID.", reply_markup=back_kb("admin:admins"))
        return
    telegram_id = int(value)
    rows = [[InlineKeyboardButton(text=role_name, callback_data=f"adm:create:{telegram_id}:{role_name}")] for role_name in db.ROLES]
    rows.append([back_button("admin:admins")])
    await message.answer("Выберите роль:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.regexp(r"^adm:create:\d+:(owner|admin|moderator|broadcaster)$"))
async def admin_create(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, telegram_id, role = callback.data.split(":")
    if int(telegram_id) in bootstrap_owner_ids():
        await callback.message.edit_text("Этот ID уже является bootstrap owner.", reply_markup=back_kb("admin:admins"))
        return
    db.add_admin(int(telegram_id), role, callback.from_user.id)
    db.log_action(callback.from_user.id, "admin_add", target=telegram_id, metadata=f'{{"role":"{role}"}}')
    await callback.message.edit_text("Администратор добавлен.", reply_markup=back_kb("admin:admins"))


@admin_router.callback_query(F.data.regexp(r"^adm:role:\d+:(owner|admin|moderator|broadcaster)$"))
async def admin_change_role(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, telegram_id, role = callback.data.split(":")
    if int(telegram_id) in bootstrap_owner_ids():
        return
    if not db.set_admin_role(int(telegram_id), role):
        await callback.message.edit_text("Администратор уже удалён.", reply_markup=back_kb("admin:admins"))
        return
    db.log_action(callback.from_user.id, "admin_role", target=telegram_id, metadata=f'{{"role":"{role}"}}')
    await callback.message.edit_text("Роль изменена.", reply_markup=back_kb("admin:admins"))


@admin_router.callback_query(F.data.startswith("adm:toggle:"))
async def admin_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    if telegram_id in bootstrap_owner_ids():
        return
    row = db.get_admin(telegram_id)
    if not row:
        return
    enabled = not bool(row["enabled"])
    db.set_admin_enabled(telegram_id, enabled)
    db.log_action(callback.from_user.id, "admin_enable" if enabled else "admin_disable", target=str(telegram_id))
    await callback.message.edit_text("Статус изменён.", reply_markup=back_kb("admin:admins"))


@admin_router.callback_query(F.data.startswith("adm:delete:"))
async def admin_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    telegram_id = callback.data.rsplit(":", 1)[1]
    if int(telegram_id) in bootstrap_owner_ids():
        await callback.message.edit_text("Bootstrap owner нельзя удалить через UI.", reply_markup=back_kb("admin:admins"))
        return
    await callback.message.edit_text(
        f"Удалить DB-admin {telegram_id}?",
        reply_markup=confirm_kb("admin_delete", telegram_id, f"adm:view:{telegram_id}"),
    )


@admin_router.callback_query(F.data.startswith("admin:confirm:admin_delete:"))
async def admin_delete(callback: CallbackQuery) -> None:
    await callback.answer()
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    if telegram_id in bootstrap_owner_ids():
        return
    db.delete_admin(telegram_id)
    db.log_action(callback.from_user.id, "admin_delete", target=str(telegram_id))
    await callback.message.edit_text("Администратор удалён.", reply_markup=back_kb("admin:admins"))
