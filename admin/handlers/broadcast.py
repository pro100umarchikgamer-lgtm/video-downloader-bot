from __future__ import annotations

import json
import time

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from admin import db, stats
from admin.auth import get_admin_role, has_permission
from admin.broadcast import db as bdb
from admin.broadcast import sender
from admin.buttons.service import is_valid_button_url
from admin.handlers import admin_router, require_admin_permission
from admin.keyboards import back_button, back_kb, confirm_kb, fsm_cancel_kb

PAGE_SIZE = 10

AUDIENCE_LABELS = {
    "all": "👥 Все пользователи",
    "active_1": "🟢 Активные за 1 день",
    "active_7": "🟢 Активные за 7 дней",
    "active_30": "🟢 Активные за 30 дней",
    "new_7": "🆕 Новые за 7 дней",
    "downloaded": "📥 Скачивавшие видео",
    "top": "💎 Самые активные",
    "groups": "💬 Все группы",
}


class BroadcastCreate(StatesGroup):
    choosing_audience = State()
    waiting_content = State()
    waiting_buttons = State()
    previewing = State()


def _require_permission(action: str):
    """
    Декоратор-проверка для callback-хендлеров раздела broadcast.
    AdminFilter на admin_router уже гарантирует "админ хоть какой-то";
    здесь проверяется конкретное право на broadcast-действия (п.18-20:
    moderator не должен иметь доступ к разделу, права проверяются перед
    каждым запуском).
    """

    def decorator(func):
        async def wrapper(event, *args, **kwargs):
            bot = kwargs.get("bot")
            role = await get_admin_role(bot, event.from_user.id)
            if not has_permission(role, action):
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Недостаточно прав", show_alert=True)
                return
            return await func(event, *args, **kwargs)

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator


def _broadcast_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Новая рассылка", callback_data="bc:new")],
            [InlineKeyboardButton(text="📋 Черновики", callback_data="bc:list:draft:0")],
            [InlineKeyboardButton(text="▶️ Активные", callback_data="bc:list:running:0")],
            [InlineKeyboardButton(text="✅ Завершённые", callback_data="bc:list:completed:0")],
            [InlineKeyboardButton(text="❌ Ошибки", callback_data="bc:list:failed:0")],
            [InlineKeyboardButton(text="⏹ Отменённые", callback_data="bc:list:cancelled:0")],
            [back_button("admin:main")],
        ]
    )


def _audience_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"bc:audience:{key}")]
        for key, label in AUDIENCE_LABELS.items()
    ]
    rows.append([back_button("admin:broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _resolve_audience(audience_key: str) -> tuple[list[int], str]:
    """Возвращает (список chat_id, человекочитаемое имя аудитории)."""
    if audience_key == "groups":
        return stats.resolve_group_audience(), AUDIENCE_LABELS["groups"]
    if audience_key == "all":
        return stats.resolve_user_audience("all"), AUDIENCE_LABELS["all"]
    if audience_key.startswith("active_"):
        days = int(audience_key.split("_")[1])
        return stats.resolve_user_audience("active", period_days=days), AUDIENCE_LABELS[audience_key]
    if audience_key.startswith("new_"):
        days = int(audience_key.split("_")[1])
        return stats.resolve_user_audience("new", period_days=days), AUDIENCE_LABELS[audience_key]
    if audience_key == "downloaded":
        return stats.resolve_user_audience("downloaded"), AUDIENCE_LABELS["downloaded"]
    if audience_key == "top":
        return stats.resolve_user_audience("top", period_days=50), AUDIENCE_LABELS["top"]
    raise ValueError(f"Unknown audience key: {audience_key}")


def _message_to_payload(message: Message) -> dict | None:
    """
    Поддерживаемые типы: text, photo, video, document, animation.
    file_id берётся из самого сообщения — ничего не перезакачивается,
    медиа используется по ссылке (п.16: не создавать копии без нужды).
    """
    if message.text:
        return {"type": "text", "text": message.text}
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id, "caption": message.caption}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id, "caption": message.caption}
    if message.animation:
        return {"type": "animation", "file_id": message.animation.file_id, "caption": message.caption}
    return None


def _preview_text(payload: dict, audience_label: str, audience_count: int) -> str:
    content = payload.get("text") or payload.get("caption") or "(без текста)"
    return (
        "📢 Предпросмотр\n\n"
        f"Аудитория:\n{audience_label} — {audience_count}\n\n"
        f"Тип: {payload['type']}\n\n"
        f"Сообщение:\n{content}"
    )


def _preview_kb(draft_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Запустить", callback_data=f"bc:launch:{draft_token}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin:fsm:cancel"),
            ]
        ]
    )


def _progress_bar(sent_total: int, total: int, width: int = 10) -> str:
    if total == 0:
        return "░" * width + " 0%"
    filled = round(width * sent_total / total)
    pct = round(100 * sent_total / total)
    return "█" * filled + "░" * (width - filled) + f" {pct}%"


def _control_text(b) -> str:
    done = b["sent"] + b["failed"] + b["blocked"]
    return (
        f"📢 Рассылка #{b['id']}\n\n"
        f"Статус: {b['status'].upper()}\n\n"
        f"{_progress_bar(done, b['total'])}\n\n"
        f"Всего: {b['total']}\n"
        f"Отправлено: {b['sent']}\n"
        f"Ошибок: {b['failed']}\n"
        f"Заблокировали: {b['blocked']}"
    )


def _control_kb(b) -> InlineKeyboardMarkup:
    rows = []
    if b["status"] == "running":
        rows.append([InlineKeyboardButton(text="⏸ Пауза", callback_data=f"bc:pause:{b['id']}")])
    elif b["status"] == "paused":
        rows.append([InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"bc:resume:{b['id']}")])

    if b["status"] in ("running", "paused"):
        rows.append([InlineKeyboardButton(text="❌ Остановить", callback_data=f"bc:cancel:{b['id']}")])

    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"bc:view:{b['id']}")])

    if b["status"] in ("completed", "cancelled", "failed") and b["failed"] > 0:
        rows.append([InlineKeyboardButton(text="🔁 Повторить ошибки", callback_data=f"bc:retry:{b['id']}")])

    rows.append([back_button("admin:broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------
# Меню и списки
# ---------------------------------------------------------------------

@admin_router.callback_query(F.data == "admin:broadcast")
async def show_broadcast_menu(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав для раздела рассылок", show_alert=True)
        return
    await callback.message.edit_text("📢 Рассылки", reply_markup=_broadcast_menu_kb())
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^bc:list:\w+:\d+$"))
async def show_broadcast_list(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    _, status, page_s = callback.data.split(":")[1:]
    page = int(page_s)
    offset = page * PAGE_SIZE

    rows = bdb.list_broadcasts(status=status, limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    total = bdb.count_broadcasts(status=status)

    kb_rows = [
        [InlineKeyboardButton(text=f"#{r['id']} — {r['created_at']}", callback_data=f"bc:view:{r['id']}")]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"bc:list:{status}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"bc:list:{status}:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([back_button("admin:broadcast")])

    text = f"Рассылки ({status}): всего {total}"
    if not rows:
        text = f"Рассылок со статусом '{status}' пока нет"

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("bc:view:"))
async def view_broadcast(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    bid = int(callback.data.split(":")[-1])
    b = bdb.get_broadcast(bid)
    if b is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return

    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer()


# ---------------------------------------------------------------------
# Создание: аудитория -> контент -> кнопки -> preview -> запуск
# ---------------------------------------------------------------------

@admin_router.callback_query(F.data == "bc:new")
async def start_new_broadcast(callback: CallbackQuery, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    await state.set_state(BroadcastCreate.choosing_audience)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text(
        "Шаг 1 из 4 — выберите аудиторию:", reply_markup=_audience_kb()
    )
    await callback.answer()


@admin_router.callback_query(
    BroadcastCreate.choosing_audience, F.data.startswith("bc:audience:")
)
async def choose_audience(callback: CallbackQuery, state: FSMContext) -> None:
    audience_key = callback.data.split(":", 2)[2]
    if audience_key not in AUDIENCE_LABELS:
        await callback.answer("Неизвестная аудитория", show_alert=True)
        return
    await state.update_data(audience_key=audience_key)
    await state.set_state(BroadcastCreate.waiting_content)
    await callback.message.edit_text(
        "Шаг 2 из 4 — отправьте сообщение для рассылки.\n\n"
        "Поддерживается: текст, фото, видео, документ, GIF — "
        "с подписью, если нужно.",
        reply_markup=fsm_cancel_kb(),
    )
    await callback.answer()


@admin_router.message(BroadcastCreate.waiting_content)
async def receive_content(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "broadcast", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние рассылки истекло.", reply_markup=back_kb("admin:broadcast"))
        return
    payload = _message_to_payload(message)
    if payload is None:
        await message.answer(
            "Не удалось распознать тип сообщения. Поддерживаются: "
            "текст, фото, видео, документ, GIF.",
            reply_markup=fsm_cancel_kb(),
        )
        return

    await state.update_data(payload=payload)
    await state.set_state(BroadcastCreate.waiting_buttons)
    await message.answer(
        "Шаг 3 из 4 — добавить inline-кнопку?\n\n"
        "Отправьте в формате `Текст|https://ссылка`, или нажмите "
        "«Пропустить», если кнопки не нужны.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Пропустить", callback_data="bc:skip_buttons")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:fsm:cancel")],
            ]
        ),
    )


@admin_router.message(BroadcastCreate.waiting_buttons, F.text.contains("|"))
async def receive_button(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "broadcast", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние рассылки истекло.", reply_markup=back_kb("admin:broadcast"))
        return
    text, _, url = message.text.partition("|")
    text, url = text.strip(), url.strip()
    if not 1 <= len(text) <= 64:
        await message.answer("Текст кнопки должен содержать от 1 до 64 символов.", reply_markup=fsm_cancel_kb())
        return
    if not is_valid_button_url(url):
        await message.answer("Нужен корректный публичный HTTP(S) URL без логина/пароля и пробелов.", reply_markup=fsm_cancel_kb())
        return

    buttons = data.get("buttons", [])
    buttons.append({"text": text, "url": url})
    await state.update_data(buttons=buttons)
    await _go_to_preview(message, state)


@admin_router.message(BroadcastCreate.waiting_buttons)
async def receive_invalid_button(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "broadcast", state):
        return
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await message.answer("Состояние рассылки истекло.", reply_markup=back_kb("admin:broadcast"))
        return
    await message.answer(
        "Отправьте кнопку в формате `Текст|https://ссылка` или нажмите «Пропустить».",
        reply_markup=fsm_cancel_kb(),
    )


@admin_router.callback_query(BroadcastCreate.waiting_buttons, F.data == "bc:skip_buttons")
async def skip_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await callback.answer("Состояние рассылки истекло", show_alert=True)
        return
    await _go_to_preview(callback.message, state, edit=True)
    await callback.answer()


async def _go_to_preview(message: Message, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    payload = dict(data["payload"])
    buttons = data.get("buttons", [])
    if buttons:
        payload["reply_markup"] = [[b] for b in buttons]

    chat_ids, audience_label = _resolve_audience(data["audience_key"])
    await state.update_data(payload=payload, resolved_audience=chat_ids)
    await state.set_state(BroadcastCreate.previewing)

    text = _preview_text(payload, audience_label, len(chat_ids))
    kb = _preview_kb("pending")

    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@admin_router.callback_query(BroadcastCreate.previewing, F.data == "bc:launch:pending")
async def launch_broadcast(callback: CallbackQuery, state: FSMContext, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    data = await state.get_data()
    if time.time() - data.get("started_at", 0) > 600:
        await state.clear()
        await callback.answer("Состояние рассылки истекло", show_alert=True)
        return
    chat_ids = data["resolved_audience"]
    payload = data["payload"]

    bid = bdb.create_broadcast(
        callback.from_user.id,
        target_filter={"audience": data["audience_key"]},
        message_payload=payload,
    )
    bdb.add_targets(bid, chat_ids)
    bdb.set_broadcast_total(bid, len(chat_ids))

    db.log_action(callback.from_user.id, "broadcast_create", target=str(bid))
    db.log_action(callback.from_user.id, "broadcast_start", target=str(bid))

    sender.start_broadcast(bot, bid)
    await state.clear()

    b = bdb.get_broadcast(bid)
    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer("Рассылка запущена")


# ---------------------------------------------------------------------
# Управление запущенной рассылкой
# ---------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("bc:pause:"))
async def pause_broadcast_handler(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    bid = int(callback.data.split(":")[-1])
    if bdb.get_broadcast(bid) is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return
    sender.pause_broadcast(bid)
    db.log_action(callback.from_user.id, "broadcast_pause", target=str(bid))

    b = bdb.get_broadcast(bid)
    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer("Пауза")


@admin_router.callback_query(F.data.startswith("bc:resume:"))
async def resume_broadcast_handler(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    bid = int(callback.data.split(":")[-1])
    if bdb.get_broadcast(bid) is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return
    started = sender.start_broadcast(bot, bid)
    db.log_action(callback.from_user.id, "broadcast_resume", target=str(bid))

    b = bdb.get_broadcast(bid)
    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer("Продолжаю" if started else "Уже выполняется")


@admin_router.callback_query(F.data.startswith("bc:cancel:"))
async def cancel_broadcast_handler(callback: CallbackQuery) -> None:
    bid = callback.data.split(":")[-1]
    if bdb.get_broadcast(int(bid)) is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ Остановить рассылку #{bid}? Уже отправленные сообщения отозвать нельзя.",
        reply_markup=confirm_kb("broadcast_cancel", bid, back_to=f"bc:view:{bid}"),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:confirm:broadcast_cancel:"))
async def confirm_cancel_broadcast(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    bid = int(callback.data.split(":")[-1])
    if bdb.get_broadcast(bid) is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return
    sender.cancel_broadcast(bid)
    db.log_action(callback.from_user.id, "broadcast_cancel", target=str(bid))

    b = bdb.get_broadcast(bid)
    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer("Остановлено")


@admin_router.callback_query(F.data.startswith("bc:retry:"))
async def retry_broadcast_handler(callback: CallbackQuery, bot) -> None:
    role = await get_admin_role(bot, callback.from_user.id)
    if not has_permission(role, "broadcast"):
        await callback.answer("⛔ Недостаточно прав", show_alert=True)
        return

    bid = int(callback.data.split(":")[-1])
    if bdb.get_broadcast(bid) is None:
        await callback.answer("Рассылка не найдена", show_alert=True)
        return
    count = sender.retry_failed(bot, bid)
    db.log_action(callback.from_user.id, "broadcast_retry_failed", target=str(bid), metadata=json.dumps({"count": count}))

    b = bdb.get_broadcast(bid)
    await callback.message.edit_text(_control_text(b), reply_markup=_control_kb(b))
    await callback.answer(f"Повтор для {count} адресатов" if count else "Нет ошибок для повтора")
