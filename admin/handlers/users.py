from __future__ import annotations

import time

from aiogram import F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from admin import db
from admin.handlers import admin_router, require_admin_permission
from admin.keyboards import back_kb, confirm_kb, fsm_cancel_kb, paginated_list_kb

PAGE_SIZE = 10


class UserSearch(StatesGroup):
    waiting_query = State()


class UserMessage(StatesGroup):
    waiting_text = State()


def _user_label(row) -> str:
    uname = f"@{row['username']}" if row["username"] else str(row["telegram_id"])
    blocked = " 🚫" if row["blocked"] else ""
    return f"{uname}{blocked}"


def _user_card_text(row) -> str:
    status = "🚫 заблокирован" if row["blocked"] else "🟢 активен"
    return (
        f"👤 Пользователь {row['telegram_id']}\n\n"
        f"Username: @{row['username'] if row['username'] else '—'}\n"
        f"Имя: {row['first_name'] or '—'}\n"
        f"Первое сообщение: {row['first_seen']}\n"
        f"Последняя активность: {row['last_seen']}\n\n"
        f"Загрузок всего: {row['downloads_total']}\n"
        f"Успешных: {row['downloads_success']}\n"
        f"Неудачных: {row['downloads_failed']}\n"
        f"Cache hits: {row['cache_hits']}\n\n"
        f"Язык: {row['locale']} ({'manual' if row['locale_manual'] else 'auto'})\n"
        f"Качество: {row['default_quality']}\n\n"
        f"Статус: {status}"
    )


def _user_card_kb(telegram_id: int, blocked: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    block_btn = (
        InlineKeyboardButton(
            text="🟢 Разблокировать",
            callback_data=f"admin:users:unblock:{telegram_id}",
        )
        if blocked
        else InlineKeyboardButton(
            text="🚫 Заблокировать",
            callback_data=f"admin:users:block:{telegram_id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [block_btn],
            [
                InlineKeyboardButton(
                    text="📨 Отправить сообщение",
                    callback_data=f"admin:users:message:{telegram_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="admin:users:0"
                )
            ],
        ]
    )


@admin_router.callback_query(F.data.regexp(r"^admin:users:\d+$"))
async def show_users_list(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    offset = page * PAGE_SIZE

    rows = db.list_users(limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    total = db.count_users()
    items = [
        (_user_label(row), f"admin:users:card:{row['telegram_id']}")
        for row in rows
    ]

    text = f"👥 Пользователи (всего: {total})\n\nСтраница {page + 1}"
    if not items:
        text = f"👥 Пользователей пока нет (всего: {total})"

    kb = paginated_list_kb(items, page, has_next, "admin:users", back_to="admin:main")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:users:search")],
        *kb.inline_keyboard,
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:users:card:"))
async def show_user_card(callback: CallbackQuery) -> None:
    telegram_id = int(callback.data.split(":")[-1])
    row = db.get_user(telegram_id)
    if row is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    await callback.message.edit_text(
        _user_card_text(row), reply_markup=_user_card_kb(telegram_id, bool(row["blocked"]))
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:users:block:"))
async def confirm_block_user(callback: CallbackQuery) -> None:
    telegram_id = callback.data.split(":")[-1]
    await callback.message.edit_text(
        f"⚠️ Заблокировать пользователя {telegram_id}?\n"
        f"Он больше не сможет отправлять видео на скачивание.",
        reply_markup=confirm_kb(
            "block_user", telegram_id, back_to=f"admin:users:card:{telegram_id}"
        ),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:confirm:block_user:"))
async def do_block_user(callback: CallbackQuery) -> None:
    telegram_id = int(callback.data.split(":")[-1])
    db.set_user_blocked(telegram_id, True)
    db.log_action(callback.from_user.id, "block_user", target=str(telegram_id))

    row = db.get_user(telegram_id)
    if row is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await callback.message.edit_text(
        _user_card_text(row), reply_markup=_user_card_kb(telegram_id, True)
    )
    await callback.answer("Пользователь заблокирован")


@admin_router.callback_query(F.data.startswith("admin:users:unblock:"))
async def do_unblock_user(callback: CallbackQuery) -> None:
    telegram_id = int(callback.data.split(":")[-1])
    db.set_user_blocked(telegram_id, False)
    db.log_action(callback.from_user.id, "unblock_user", target=str(telegram_id))

    row = db.get_user(telegram_id)
    if row is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await callback.message.edit_text(
        _user_card_text(row), reply_markup=_user_card_kb(telegram_id, False)
    )
    await callback.answer("Пользователь разблокирован")


@admin_router.callback_query(F.data.startswith("admin:users:message:"))
async def ask_message_text(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = int(callback.data.split(":")[-1])
    if db.get_user(telegram_id) is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await state.update_data(target_user_id=telegram_id, started_at=time.time())
    await state.set_state(UserMessage.waiting_text)
    await callback.message.edit_text(
        f"📨 Введите текст сообщения для пользователя {telegram_id}:",
        reply_markup=fsm_cancel_kb(),
    )
    await callback.answer()


@admin_router.message(UserMessage.waiting_text)
async def send_message_to_user(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "users", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние отправки истекло.", reply_markup=back_kb("admin:users:0"))
        return
    telegram_id = data["target_user_id"]
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите непустой текст.", reply_markup=fsm_cancel_kb())
        return
    await state.clear()

    try:
        await bot.send_message(telegram_id, text)
        db.log_action(message.from_user.id, "message_user", target=str(telegram_id))
        await message.answer("✅ Сообщение отправлено.")
    except TelegramForbiddenError:
        await message.answer("❌ Пользователь заблокировал бота, сообщение не доставлено.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить сообщение (временная ошибка).")


@admin_router.callback_query(F.data == "admin:users:search")
async def start_user_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(UserSearch.waiting_query)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text("Введите Telegram ID, username или имя:", reply_markup=fsm_cancel_kb())


@admin_router.message(UserSearch.waiting_query)
async def finish_user_search(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "users", state):
        return
    data = await state.get_data()
    await state.clear()
    if time.time() - data.get("started_at", 0) > 600:
        await message.answer("Состояние поиска истекло.", reply_markup=back_kb("admin:users:0"))
        return
    rows = db.search_users(message.text or "", limit=20)
    items = [(_user_label(row), f"admin:users:card:{row['telegram_id']}") for row in rows]
    kb = paginated_list_kb(items, 0, False, "admin:users", back_to="admin:users:0")
    await message.answer("Результаты поиска:" if rows else "Ничего не найдено.", reply_markup=kb)
