from __future__ import annotations

import time

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from admin import db, stats
from admin.auth import get_admin_role
from admin.handlers import admin_router
from admin.keyboards import back_kb, main_menu_kb
from app.context import app_context
from app.services.queue import download_queue


@admin_router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message, bot) -> None:
    role = await get_admin_role(bot, message.from_user.id)
    db.log_action(message.from_user.id, "admin_open")
    await message.answer(f"👑 Админ-панель\nРоль: {role}", reply_markup=main_menu_kb(role or ""))


@admin_router.callback_query(F.data == "admin:main")
async def show_main_menu(callback: CallbackQuery, bot) -> None:
    await callback.answer()
    role = await get_admin_role(bot, callback.from_user.id)
    await callback.message.edit_text(f"👑 Админ-панель\nРоль: {role}", reply_markup=main_menu_kb(role or ""))


@admin_router.callback_query(F.data == "admin:dashboard")
async def show_dashboard(callback: CallbackQuery) -> None:
    await callback.answer()
    context = app_context()
    downloads = stats.download_stats()
    cache = stats.cache_stats()
    storage = context.storage.statistics()
    lookups = cache["hits"] + cache["misses"]
    hit_rate = cache["hits"] / lookups * 100 if lookups else 0
    uptime = int(time.monotonic() - context.started_monotonic)
    healthy = sum(1 for item in context.storage.list_storages(enabled_only=True) if item["healthy"])
    enabled = len(context.storage.list_storages(enabled_only=True))
    text = (
        "📊 Дашборд\n\n"
        f"Пользователи: {stats.total_users()}\n"
        f"Активные 24ч / 7д / 30д: {stats.active_users(1)} / {stats.active_users(7)} / {stats.active_users(30)}\n"
        f"Новые 24ч / 7д / 30д: {stats.new_users(1)} / {stats.new_users(7)} / {stats.new_users(30)}\n"
        f"Группы: {stats.total_groups()}\n\n"
        f"Загрузки: {downloads['total']} · успешно {downloads['success']} · ошибок {downloads['failed']}\n"
        f"Сейчас: {download_queue.active_count} активных · {download_queue.queued_count} в очереди\n"
        f"Cache HIT/MISS: {cache['hits']}/{cache['misses']} · {hit_rate:.1f}%\n"
        f"Медиа: {storage['entries']} · {storage['bytes'] / 1024 / 1024 / 1024:.2f} GB\n"
        f"Хранилища: {healthy}/{enabled} healthy\n"
        f"Uptime: {uptime // 3600}ч {(uptime % 3600) // 60}м"
    )
    await callback.message.edit_text(text, reply_markup=back_kb())
