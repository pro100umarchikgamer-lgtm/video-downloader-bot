from __future__ import annotations

import time

from aiogram import F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db
from admin.handlers import admin_router, require_admin_permission
from admin.keyboards import back_kb, confirm_kb, fsm_cancel_kb, paginated_list_kb

PAGE_SIZE = 10


class GroupMessage(StatesGroup):
    waiting_text = State()


class GroupSearch(StatesGroup):
    waiting_query = State()


def _group_label(row) -> str:
    name = row["title"] or str(row["chat_id"])
    blocked = " 🚫" if row["blocked"] else ""
    return f"{name}{blocked}"


def _group_card_text(row) -> str:
    status = "🚫 заблокирована" if row["blocked"] else "🟢 активна"
    uname = f"@{row['username']}" if row["username"] else "—"
    return (
        f"💬 Группа {row['chat_id']}\n\n"
        f"Название: {row['title'] or '—'}\n"
        f"Username: {uname}\n"
        f"Тип: {row['type']}\n"
        f"Первое сообщение: {row['first_seen']}\n"
        f"Последняя активность: {row['last_seen']}\n\n"
        f"Загрузок всего: {row['downloads_total']}\n"
        f"Успешных: {row['downloads_success']}\n"
        f"Неудачных: {row['downloads_failed']}\n\n"
        f"Язык: {row['locale']}\nКачество: {row['default_quality']}\n\n"
        f"Статус: {status}"
    )


def _group_card_kb(chat_id: int, blocked: bool) -> InlineKeyboardMarkup:
    block_btn = (
        InlineKeyboardButton(
            text="🟢 Разблокировать",
            callback_data=f"admin:groups:unblock:{chat_id}",
        )
        if blocked
        else InlineKeyboardButton(
            text="🚫 Заблокировать",
            callback_data=f"admin:groups:block:{chat_id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [block_btn],
            [
                InlineKeyboardButton(
                    text="🎬 Качество группы",
                    callback_data=f"admin:groups:quality:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📨 Отправить сообщение",
                    callback_data=f"admin:groups:message:{chat_id}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:groups:0")],
        ]
    )


@admin_router.callback_query(F.data.regexp(r"^admin:groups:\d+$"))
async def show_groups_list(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    offset = page * PAGE_SIZE

    rows = db.list_groups(limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    total = db.count_groups()
    items = [
        (_group_label(row), f"admin:groups:card:{row['chat_id']}")
        for row in rows
    ]

    text = f"💬 Группы (всего: {total})\n\nСтраница {page + 1}"
    if not items:
        text = f"💬 Групп пока нет (всего: {total})"

    kb = paginated_list_kb(items, page, has_next, "admin:groups", back_to="admin:main")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:groups:search")],
        *kb.inline_keyboard,
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:groups:card:"))
async def show_group_card(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[-1])
    row = db.get_group(chat_id)
    if row is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        _group_card_text(row), reply_markup=_group_card_kb(chat_id, bool(row["blocked"]))
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:groups:block:"))
async def confirm_block_group(callback: CallbackQuery) -> None:
    chat_id = callback.data.split(":")[-1]
    await callback.message.edit_text(
        f"⚠️ Заблокировать группу {chat_id}?\n"
        f"Загрузки видео в ней перестанут обрабатываться.",
        reply_markup=confirm_kb(
            "block_group", chat_id, back_to=f"admin:groups:card:{chat_id}"
        ),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:confirm:block_group:"))
async def do_block_group(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[-1])
    db.set_group_blocked(chat_id, True)
    db.log_action(callback.from_user.id, "block_group", target=str(chat_id))

    row = db.get_group(chat_id)
    if row is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        _group_card_text(row), reply_markup=_group_card_kb(chat_id, True)
    )
    await callback.answer("Группа заблокирована")


@admin_router.callback_query(F.data.startswith("admin:groups:unblock:"))
async def do_unblock_group(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.split(":")[-1])
    db.set_group_blocked(chat_id, False)
    db.log_action(callback.from_user.id, "unblock_group", target=str(chat_id))

    row = db.get_group(chat_id)
    if row is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        _group_card_text(row), reply_markup=_group_card_kb(chat_id, False)
    )
    await callback.answer("Группа разблокирована")


@admin_router.callback_query(F.data.startswith("admin:groups:message:"))
async def ask_group_message_text(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = int(callback.data.split(":")[-1])
    if db.get_group(chat_id) is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    await state.update_data(target_chat_id=chat_id, started_at=time.time())
    await state.set_state(GroupMessage.waiting_text)
    await callback.message.edit_text(
        f"📨 Введите текст сообщения для группы {chat_id}:",
        reply_markup=fsm_cancel_kb(),
    )
    await callback.answer()


@admin_router.message(GroupMessage.waiting_text)
async def send_message_to_group(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "groups", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние отправки истекло.", reply_markup=back_kb("admin:groups:0"))
        return
    chat_id = data["target_chat_id"]
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите непустой текст.", reply_markup=fsm_cancel_kb())
        return
    await state.clear()

    try:
        await bot.send_message(chat_id, text)
        db.log_action(message.from_user.id, "message_group", target=str(chat_id))
        await message.answer("✅ Сообщение отправлено.")
    except TelegramForbiddenError:
        await message.answer("❌ Бот не состоит в группе или не может писать, сообщение не доставлено.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить сообщение (временная ошибка).")


@admin_router.callback_query(F.data == "admin:groups:search")
async def start_group_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GroupSearch.waiting_query)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text("Введите chat ID, title или username:", reply_markup=fsm_cancel_kb())


@admin_router.message(GroupSearch.waiting_query)
async def finish_group_search(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "groups", state):
        return
    data = await state.get_data()
    await state.clear()
    if time.time() - data.get("started_at", 0) > 600:
        await message.answer("Состояние поиска истекло.", reply_markup=back_kb("admin:groups:0"))
        return
    rows = db.search_groups(message.text or "", limit=20)
    items = [(_group_label(row), f"admin:groups:card:{row['chat_id']}") for row in rows]
    kb = paginated_list_kb(items, 0, False, "admin:groups", back_to="admin:groups:0")
    await message.answer("Результаты поиска:" if rows else "Ничего не найдено.", reply_markup=kb)


@admin_router.callback_query(F.data.startswith("admin:groups:quality:"))
async def group_quality_menu(callback: CallbackQuery) -> None:
    chat_id = int(callback.data.rsplit(":", 1)[1])
    if db.get_group(chat_id) is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=value, callback_data=f"admin:groups:setquality:{chat_id}:{value}")] for value in ("auto", "360", "480", "720", "1080", "1440", "2160")]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:groups:card:{chat_id}")])
    await callback.message.edit_text("Выберите качество группы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:groups:setquality:"))
async def group_quality_set(callback: CallbackQuery) -> None:
    _, _, _, chat_id_s, quality = callback.data.split(":")
    chat_id = int(chat_id_s)
    if not db.set_group_quality(chat_id, quality):
        await callback.answer("Группа или качество не найдены", show_alert=True)
        return
    db.log_action(callback.from_user.id, "group_quality", target=str(chat_id), metadata=f'{{"quality":"{quality}"}}')
    row = db.get_group(chat_id)
    await callback.message.edit_text(_group_card_text(row), reply_markup=_group_card_kb(chat_id, bool(row["blocked"])))
    await callback.answer("Сохранено")
