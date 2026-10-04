from __future__ import annotations

import time

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db as admin_db
from admin.auth import get_admin_role, has_permission
from admin.buttons import db as bdb
from admin.buttons.service import is_valid_button_url
from admin.handlers import admin_router, require_admin_permission
from admin.keyboards import back_button, back_kb, confirm_kb, fsm_cancel_kb


class ButtonCreate(StatesGroup):
    waiting_text = State()
    waiting_url = State()


class ButtonEdit(StatesGroup):
    waiting_text = State()
    waiting_url = State()


def _status_icon(enabled: int) -> str:
    return "ON" if enabled else "OFF"


def _list_kb(buttons: list) -> InlineKeyboardMarkup:
    rows = []
    for i, btn in enumerate(buttons, start=1):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{i}. {btn['text']} — {_status_icon(btn['enabled'])}",
                    callback_data=f"btn:view:{btn['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="btn:add")])
    rows.append([back_button("admin:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _button_card_text(btn) -> str:
    return (
        f"🔘 Кнопка #{btn['id']}\n\n"
        f"Текст: {btn['text']}\n"
        f"URL: {btn['url']}\n"
        f"Позиция: {btn['position']}\n"
        f"Статус: {_status_icon(btn['enabled'])}"
    )


def _button_card_kb(btn) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(text="🔴 Выключить", callback_data=f"btn:toggle:{btn['id']}")
        if btn["enabled"]
        else InlineKeyboardButton(text="🟢 Включить", callback_data=f"btn:toggle:{btn['id']}")
    )
    system_key = btn["system_key"] if "system_key" in btn.keys() else None
    if system_key:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [toggle],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:buttons")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"btn:edit_text:{btn['id']}")],
            [InlineKeyboardButton(text="✏️ Изменить URL", callback_data=f"btn:edit_url:{btn['id']}")],
            [toggle],
            [
                InlineKeyboardButton(text="⬆️", callback_data=f"btn:move:up:{btn['id']}"),
                InlineKeyboardButton(text="⬇️", callback_data=f"btn:move:down:{btn['id']}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"btn:delete:{btn['id']}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:buttons")],
        ]
    )


def _require_buttons_permission(role) -> bool:
    # Управление кнопками относится к тем же правам, что и "buttons" в
    # _PERMISSIONS (owner/admin). moderator/broadcaster не допускаются —
    # то же разграничение, что и в остальных admin-разделах.
    return has_permission(role, "buttons")


@admin_router.callback_query(F.data == "admin:buttons")
async def show_buttons_list(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    buttons = bdb.list_buttons(scope="downloads")
    text = "🔘 Inline-кнопки" if buttons else "🔘 Кнопок пока нет"
    await callback.message.edit_text(text, reply_markup=_list_kb(buttons))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("btn:view:"))
async def view_button(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    btn_id = int(callback.data.split(":")[-1])
    btn = bdb.get_button(btn_id)
    if btn is None:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return

    await callback.message.edit_text(_button_card_text(btn), reply_markup=_button_card_kb(btn))
    await callback.answer()


@admin_router.callback_query(F.data == "btn:add")
async def start_add_button(callback: CallbackQuery, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    await state.set_state(ButtonCreate.waiting_text)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text(
        "Введите текст новой кнопки:", reply_markup=fsm_cancel_kb()
    )
    await callback.answer()


@admin_router.message(ButtonCreate.waiting_text)
async def receive_new_button_text(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "buttons", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние добавления истекло.", reply_markup=back_kb("admin:buttons"))
        return
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 64:
        await message.answer("Текст кнопки должен содержать от 1 до 64 символов.", reply_markup=fsm_cancel_kb())
        return
    await state.update_data(text=text)
    await state.set_state(ButtonCreate.waiting_url)
    await message.answer("Теперь введите URL (должен начинаться с http:// или https://):", reply_markup=fsm_cancel_kb())


@admin_router.message(ButtonCreate.waiting_url)
async def receive_new_button_url(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "buttons", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние добавления истекло.", reply_markup=back_kb("admin:buttons"))
        return
    url = (message.text or "").strip()
    if not is_valid_button_url(url):
        await message.answer("Нужен корректный публичный HTTP(S) URL без логина/пароля и пробелов.", reply_markup=fsm_cancel_kb())
        return

    btn_id = bdb.create_button(data["text"], url, scope="downloads")
    admin_db.log_action(message.from_user.id, "button_create", target=str(btn_id))
    await state.clear()

    btn = bdb.get_button(btn_id)
    await message.answer(_button_card_text(btn), reply_markup=_button_card_kb(btn))


@admin_router.callback_query(F.data.startswith("btn:edit_text:"))
async def start_edit_text(callback: CallbackQuery, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    btn_id = int(callback.data.split(":")[-1])
    btn = bdb.get_button(btn_id)
    if btn is None or ("system_key" in btn.keys() and btn["system_key"]):
        await callback.answer("Системную кнопку можно только включить или выключить", show_alert=True)
        return
    await state.update_data(button_id=btn_id)
    await state.update_data(started_at=time.time())
    await state.set_state(ButtonEdit.waiting_text)
    await callback.message.edit_text(
        "Введите новый текст кнопки:", reply_markup=fsm_cancel_kb()
    )
    await callback.answer()


@admin_router.message(ButtonEdit.waiting_text)
async def receive_edited_text(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "buttons", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние редактирования истекло.", reply_markup=back_kb("admin:buttons"))
        return
    btn_id = data["button_id"]
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 64:
        await message.answer("Текст кнопки должен содержать от 1 до 64 символов.", reply_markup=fsm_cancel_kb())
        return
    if bdb.get_button(btn_id) is None:
        await state.clear()
        await message.answer("Кнопка уже удалена.", reply_markup=back_kb("admin:buttons"))
        return
    bdb.update_button(btn_id, text=text)
    admin_db.log_action(message.from_user.id, "button_edit_text", target=str(btn_id))
    await state.clear()

    btn = bdb.get_button(btn_id)
    await message.answer(_button_card_text(btn), reply_markup=_button_card_kb(btn))


@admin_router.callback_query(F.data.startswith("btn:edit_url:"))
async def start_edit_url(callback: CallbackQuery, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    btn_id = int(callback.data.split(":")[-1])
    btn = bdb.get_button(btn_id)
    if btn is None or ("system_key" in btn.keys() and btn["system_key"]):
        await callback.answer("Системную кнопку можно только включить или выключить", show_alert=True)
        return
    await state.update_data(button_id=btn_id)
    await state.update_data(started_at=time.time())
    await state.set_state(ButtonEdit.waiting_url)
    await callback.message.edit_text(
        "Введите новый URL (http:// или https://):", reply_markup=fsm_cancel_kb()
    )
    await callback.answer()


@admin_router.message(ButtonEdit.waiting_url)
async def receive_edited_url(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "buttons", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние редактирования истекло.", reply_markup=back_kb("admin:buttons"))
        return
    url = (message.text or "").strip()
    if not is_valid_button_url(url):
        await message.answer("Нужен корректный публичный HTTP(S) URL без логина/пароля и пробелов.", reply_markup=fsm_cancel_kb())
        return

    btn_id = data["button_id"]
    if bdb.get_button(btn_id) is None:
        await state.clear()
        await message.answer("Кнопка уже удалена.", reply_markup=back_kb("admin:buttons"))
        return
    bdb.update_button(btn_id, url=url)
    admin_db.log_action(message.from_user.id, "button_edit_url", target=str(btn_id))
    await state.clear()

    btn = bdb.get_button(btn_id)
    await message.answer(_button_card_text(btn), reply_markup=_button_card_kb(btn))


@admin_router.callback_query(F.data.startswith("btn:toggle:"))
async def toggle_button(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    btn_id = int(callback.data.split(":")[-1])
    btn = bdb.get_button(btn_id)
    if btn is None:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return

    new_state = not bool(btn["enabled"])
    bdb.set_button_enabled(btn_id, new_state)
    admin_db.log_action(
        callback.from_user.id,
        "button_enable" if new_state else "button_disable",
        target=str(btn_id),
    )

    btn = bdb.get_button(btn_id)
    await callback.message.edit_text(_button_card_text(btn), reply_markup=_button_card_kb(btn))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("btn:move:"))
async def move_button_handler(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    _, _, direction, btn_id_s = callback.data.split(":")
    btn_id = int(btn_id_s)
    current = bdb.get_button(btn_id)
    if current is None:
        await callback.answer("Кнопка не найдена", show_alert=True)
        return
    if "system_key" in current.keys() and current["system_key"]:
        await callback.answer("Системную кнопку перемещать нельзя", show_alert=True)
        return
    moved = bdb.move_button(btn_id, direction)

    btn = bdb.get_button(btn_id)
    await callback.message.edit_text(_button_card_text(btn), reply_markup=_button_card_kb(btn))
    await callback.answer("Перемещено" if moved else "Уже крайняя позиция")


@admin_router.callback_query(F.data.startswith("btn:delete:"))
async def confirm_delete_button(callback: CallbackQuery) -> None:
    btn_id = callback.data.split(":")[-1]
    btn = bdb.get_button(int(btn_id))
    if btn is None or ("system_key" in btn.keys() and btn["system_key"]):
        await callback.answer("Системную кнопку удалить нельзя", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ Удалить кнопку #{btn_id}? Это действие необратимо.",
        reply_markup=confirm_kb("button_delete", btn_id, back_to=f"btn:view:{btn_id}"),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:confirm:button_delete:"))
async def do_delete_button(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not _require_buttons_permission(role):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    btn_id = int(callback.data.split(":")[-1])
    btn = bdb.get_button(btn_id)
    if btn is None:
        await callback.answer("Кнопка уже удалена", show_alert=True)
        return
    if "system_key" in btn.keys() and btn["system_key"]:
        await callback.answer("Системную кнопку удалить нельзя", show_alert=True)
        return
    if not bdb.delete_button(btn_id):
        await callback.answer("Кнопка не удалена", show_alert=True)
        return
    admin_db.log_action(callback.from_user.id, "button_delete", target=str(btn_id))

    buttons = bdb.list_buttons(scope="downloads")
    text = "🔘 Inline-кнопки" if buttons else "🔘 Кнопок пока нет"
    await callback.message.edit_text(text, reply_markup=_list_kb(buttons))
    await callback.answer("Удалено")
