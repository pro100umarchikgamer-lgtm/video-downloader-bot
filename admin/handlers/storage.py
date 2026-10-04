from __future__ import annotations

import time

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db
from admin.auth import get_admin_role, has_permission
from admin.handlers import admin_router
from admin.keyboards import back_button, back_kb, confirm_kb
from app.context import app_context


class StorageAdd(StatesGroup):
    waiting_channel = State()


def _storage_service():
    return app_context().storage


def _storage_list_kb() -> InlineKeyboardMarkup:
    rows = []
    for storage in _storage_service().list_storages():
        health = "🟢" if storage["healthy"] and storage["enabled"] else "⏸" if not storage["enabled"] else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{health} {storage['storage_type'].upper()} · {storage['role']} · {storage['telegram_chat_id']}",
            callback_data=f"stg:view:{storage['id']}",
        )])
    rows.extend([
        [InlineKeyboardButton(text="➕ Добавить", callback_data="stg:add")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stg:stats")],
        [back_button()],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "admin:storage")
async def storage_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    storages = _storage_service().list_storages()
    adult = next((row for row in storages if row["storage_type"] == "adult"), None)
    adult_state = "настроено, автоматическая классификация отключена" if adult else "неактивно: надёжная классификация не включена"
    await callback.message.edit_text(
        f"💾 Хранилище\n\nКонфигураций: {len(storages)}\nADULT: {adult_state}",
        reply_markup=_storage_list_kb(),
    )


def _storage_card_text(row) -> str:
    permissions = row["bot_permissions"] or "—"
    return (
        f"💾 Storage #{row['id']}\n\n"
        f"Тип: {row['storage_type'].upper()}\nРоль: {row['role']}\n"
        f"Канал: {row['title'] or '—'}\nID: {row['telegram_chat_id']}\n"
        f"Статус: {'enabled' if row['enabled'] else 'disabled'} · {'healthy' if row['healthy'] else 'unhealthy/unknown'}\n"
        f"Entries: {row['entry_count']} · объём: {row['total_bytes'] / 1024 / 1024:.1f} MB\n"
        f"Права бота: {permissions[:180]}\n"
        f"Последняя запись: {row['last_success_at'] or '—'}\nПоследняя ошибка: {row['last_error'] or '—'}"
    )


def _storage_card_kb(row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Проверить", callback_data=f"stg:test:{row['id']}")],
        [InlineKeyboardButton(text="⏸ Отключить" if row["enabled"] else "▶️ Включить", callback_data=f"stg:toggle:{row['id']}")],
        [InlineKeyboardButton(text="Сделать backup" if row["role"] == "primary" else "Сделать primary", callback_data=f"stg:role:{row['id']}")],
        [InlineKeyboardButton(text="🔄 Заменить", callback_data=f"stg:replace:{row['id']}")],
        [InlineKeyboardButton(text="🗑 Удалить конфигурацию", callback_data=f"stg:delete:{row['id']}")],
        [back_button("admin:storage")],
    ])


@admin_router.callback_query(F.data.startswith("stg:view:"))
async def storage_card(callback: CallbackQuery) -> None:
    await callback.answer()
    row = _storage_service().get_storage(int(callback.data.rsplit(":", 1)[1]))
    if not row:
        await callback.message.edit_text("Storage уже удалён.", reply_markup=back_kb("admin:storage"))
        return
    await callback.message.edit_text(_storage_card_text(row), reply_markup=_storage_card_kb(row))


@admin_router.callback_query(F.data == "stg:add")
async def storage_add_choose_tier(callback: CallbackQuery) -> None:
    await callback.answer()
    rows = [[InlineKeyboardButton(text=tier.upper(), callback_data=f"stg:addtier:{tier}")] for tier in ("small", "medium", "large", "adult")]
    rows.append([back_button("admin:storage")])
    await callback.message.edit_text("Выберите тип storage:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("stg:addtier:"))
async def storage_add_choose_role(callback: CallbackQuery) -> None:
    await callback.answer()
    tier = callback.data.rsplit(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Primary", callback_data=f"stg:addrole:{tier}:primary")],
        [InlineKeyboardButton(text="Backup", callback_data=f"stg:addrole:{tier}:backup")],
        [back_button("admin:storage")],
    ])
    await callback.message.edit_text("Выберите роль:", reply_markup=kb)


async def _start_waiting_channel(callback: CallbackQuery, state: FSMContext, tier: str, role: str, replacing_id: int | None = None) -> None:
    await state.set_state(StorageAdd.waiting_channel)
    await state.update_data(tier=tier, role=role, replacing_id=replacing_id, started_at=time.time())
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="stg:cancel")]])
    await callback.message.edit_text(
        "Перешлите сообщение из канала или отправьте его числовой chat_id.\nБот должен быть администратором с правом публикации.",
        reply_markup=kb,
    )


@admin_router.callback_query(F.data.regexp(r"^stg:addrole:(small|medium|large|adult):(primary|backup)$"))
async def storage_add_wait(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, _, tier, role = callback.data.split(":")
    await _start_waiting_channel(callback, state, tier, role)


@admin_router.callback_query(F.data.startswith("stg:replace:"))
async def storage_replace_wait(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    row = _storage_service().get_storage(int(callback.data.rsplit(":", 1)[1]))
    if not row:
        return
    await _start_waiting_channel(callback, state, row["storage_type"], row["role"], row["id"])


@admin_router.callback_query(F.data == "stg:cancel")
async def storage_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Операция отменена.", reply_markup=back_kb("admin:storage"))


async def _verify_channel(bot, chat_id: int, *, probe_write: bool = False) -> tuple[bool, str, dict]:
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat_id, me.id)
        status_ok = member.status in {"creator", "administrator"}
        can_post = getattr(member, "can_post_messages", True)
        permissions = {"status": str(member.status), "can_post_messages": can_post}
        allowed = bool(status_ok and can_post is not False)
        if allowed and probe_write:
            probe = await bot.send_message(chat_id, "Storage access test", disable_notification=True)
            try:
                await bot.delete_message(chat_id, probe.message_id)
            except Exception:
                pass
        return allowed, chat.title or str(chat_id), permissions
    except Exception as exc:
        return False, str(exc)[:300], {}


def _forwarded_chat_id(message: Message) -> int | None:
    old = getattr(message, "forward_from_chat", None)
    if old:
        return old.id
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if chat:
        return chat.id
    text = (message.text or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


@admin_router.message(StorageAdd.waiting_channel)
async def storage_receive_channel(message: Message, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, message.from_user.id)
    if not has_permission(role, "storage"):
        await state.clear()
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние добавления истекло.", reply_markup=back_kb("admin:storage"))
        return
    chat_id = _forwarded_chat_id(message)
    if chat_id is None:
        await message.answer("Не удалось определить канал. Перешлите сообщение или введите числовой ID.")
        return
    ok, title, permissions = await _verify_channel(bot, chat_id, probe_write=True)
    if not ok:
        await message.answer(f"❌ Проверка не пройдена: {title[:300]}")
        return
    storage_id = _storage_service().add_storage(data["tier"], chat_id, title, data["role"], enabled=True, healthy=True, permissions=permissions)
    replacing = data.get("replacing_id")
    if replacing and replacing != storage_id:
        _storage_service().set_enabled(replacing, False)
    db.log_action(message.from_user.id, "storage_replace" if replacing else "storage_add", target=str(storage_id))
    await state.clear()
    row = _storage_service().get_storage(storage_id)
    await message.answer(_storage_card_text(row), reply_markup=_storage_card_kb(row))


@admin_router.callback_query(F.data.startswith("stg:test:"))
async def storage_test(callback: CallbackQuery, bot) -> None:
    await callback.answer()
    storage_id = int(callback.data.rsplit(":", 1)[1])
    row = _storage_service().get_storage(storage_id)
    if not row:
        return
    ok, title, permissions = await _verify_channel(bot, row["telegram_chat_id"], probe_write=True)
    if ok:
        _storage_service().mark_storage_success(storage_id)
    else:
        _storage_service().mark_storage_error(storage_id, title)
    db.log_action(callback.from_user.id, "storage_test", target=str(storage_id), metadata=f'{{"ok": {str(ok).lower()}}}')
    row = _storage_service().get_storage(storage_id)
    await callback.message.edit_text(_storage_card_text(row), reply_markup=_storage_card_kb(row))


@admin_router.callback_query(F.data.startswith("stg:toggle:"))
async def storage_toggle(callback: CallbackQuery, bot) -> None:
    await callback.answer()
    storage_id = int(callback.data.rsplit(":", 1)[1])
    row = _storage_service().get_storage(storage_id)
    if not row:
        return
    enable = not bool(row["enabled"])
    if enable:
        ok, error, _ = await _verify_channel(bot, row["telegram_chat_id"])
        if not ok:
            await callback.message.edit_text(f"Нельзя включить: {error}", reply_markup=_storage_card_kb(row))
            return
    _storage_service().set_enabled(storage_id, enable)
    db.log_action(callback.from_user.id, "storage_enable" if enable else "storage_disable", target=str(storage_id))
    row = _storage_service().get_storage(storage_id)
    await callback.message.edit_text(_storage_card_text(row), reply_markup=_storage_card_kb(row))


@admin_router.callback_query(F.data.startswith("stg:role:"))
async def storage_role(callback: CallbackQuery) -> None:
    await callback.answer()
    storage_id = int(callback.data.rsplit(":", 1)[1])
    row = _storage_service().get_storage(storage_id)
    if not row:
        return
    new_role = "backup" if row["role"] == "primary" else "primary"
    _storage_service().set_role(storage_id, new_role)
    db.log_action(callback.from_user.id, "storage_role", target=str(storage_id), metadata=f'{{"role":"{new_role}"}}')
    row = _storage_service().get_storage(storage_id)
    await callback.message.edit_text(_storage_card_text(row), reply_markup=_storage_card_kb(row))


@admin_router.callback_query(F.data.startswith("stg:delete:"))
async def storage_delete_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    storage_id = callback.data.rsplit(":", 1)[1]
    await callback.message.edit_text(
        "Удалить конфигурацию? Исторические media references сохранятся, новые записи сюда не пойдут.",
        reply_markup=confirm_kb("storage_delete", storage_id, f"stg:view:{storage_id}"),
    )


@admin_router.callback_query(F.data.startswith("admin:confirm:storage_delete:"))
async def storage_delete(callback: CallbackQuery) -> None:
    await callback.answer()
    storage_id = int(callback.data.rsplit(":", 1)[1])
    _storage_service().delete_storage_config(storage_id)
    db.log_action(callback.from_user.id, "storage_delete", target=str(storage_id))
    await callback.message.edit_text("Конфигурация удалена.", reply_markup=back_kb("admin:storage"))


@admin_router.callback_query(F.data == "stg:stats")
async def storage_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    stats = _storage_service().statistics()
    lines = [
        f"Entries: {stats['entries']}",
        f"Объём: {stats['bytes'] / 1024 / 1024 / 1024:.2f} GB",
        f"Storage failures: {stats['failures']}",
        f"Failovers: {stats['failovers']}",
        "Permanently stale: удаляются/заменяются, не накапливаются",
    ]
    await callback.message.edit_text("📊 Storage statistics\n\n" + "\n".join(lines), reply_markup=back_kb("admin:storage"))
