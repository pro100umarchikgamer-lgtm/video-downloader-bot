from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from admin import db
from admin.handlers import admin_router, require_admin_permission
from admin.keyboards import back_button, back_kb, fsm_cancel_kb, paginated_list_kb
from app.context import app_context
from app.database import get_connection, get_database_path
from app.services.jobs import active_jobs, distribution, finish_job, get_job, job_stats
from app.services.queue import download_queue


class RuntimeSettingEdit(StatesGroup):
    waiting_value = State()


class ModerationSearch(StatesGroup):
    waiting_query = State()


def _downloads_kb(period: str) -> InlineKeyboardMarkup:
    maintenance = bool(app_context().runtime.get("maintenance_mode"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="24ч", callback_data="admin:downloads:1"),
            InlineKeyboardButton(text="7д", callback_data="admin:downloads:7"),
            InlineKeyboardButton(text="30д", callback_data="admin:downloads:30"),
            InlineKeyboardButton(text="Всё", callback_data="admin:downloads:all"),
        ],
        [InlineKeyboardButton(text="▶️ Выключить maintenance" if maintenance else "⏸ Включить maintenance", callback_data="admin:downloads:maintenance")],
        [InlineKeyboardButton(text="📋 Активные задачи", callback_data="admin:downloads:active")],
        [back_button()],
    ])


@admin_router.callback_query(F.data.regexp(r"^admin:downloads:(all|1|7|30)$"))
async def downloads_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    value = callback.data.rsplit(":", 1)[1]
    days = None if value == "all" else int(value)
    stats = job_stats(days)
    completed = stats["success"] + stats["failed"]
    rate = stats["success"] / completed * 100 if completed else 0
    sources = ", ".join(f"{row['value']}: {row['count']}" for row in distribution("source", days, 5)) or "—"
    qualities = ", ".join(f"{row['value']}: {row['count']}" for row in distribution("requested_quality", days, 8)) or "—"
    hit_rate = stats["cache_hits"] / stats["cache_lookups"] * 100 if stats["cache_lookups"] else 0
    text = (
        f"📥 Загрузки · {'всё время' if days is None else str(days) + 'д'}\n\n"
        f"Всего: {stats['total']}\nАктивные: {stats['active']} · очередь: {stats['queued']}\n"
        f"Успешно: {stats['success']} · ошибок: {stats['failed']} · отменено: {stats['cancelled']}\n"
        f"Success rate: {rate:.1f}% · среднее: {stats['avg_ms'] / 1000:.1f} сек\n"
        f"Cache hit rate: {hit_rate:.1f}%\n"
        f"Средние этапы, сек: queue {stats['avg_queue_ms'] / 1000:.1f} · metadata {stats['avg_metadata_ms'] / 1000:.1f} · "
        f"download {stats['avg_download_ms'] / 1000:.1f} · process {stats['avg_processing_ms'] / 1000:.1f} · upload {stats['avg_upload_ms'] / 1000:.1f}\n\n"
        f"Источники: {sources}\nКачество: {qualities}"
    )
    await callback.message.edit_text(text, reply_markup=_downloads_kb(value))


@admin_router.callback_query(F.data == "admin:downloads:maintenance")
async def toggle_maintenance(callback: CallbackQuery) -> None:
    await callback.answer()
    runtime = app_context().runtime
    enabled = not bool(runtime.get("maintenance_mode"))
    runtime.set("maintenance_mode", enabled, actor_id=callback.from_user.id)
    db.log_action(callback.from_user.id, "maintenance_on" if enabled else "maintenance_off")
    await callback.message.edit_text(
        "⏸ Новые загрузки приостановлены." if enabled else "▶️ Приём новых загрузок возобновлён.",
        reply_markup=_downloads_kb("all"),
    )


@admin_router.callback_query(F.data == "admin:downloads:active")
async def active_downloads_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    rows = active_jobs()
    buttons = [
        [InlineKeyboardButton(text=f"{row['status']} · {row['id'][:8]} · {row['requester_id']}", callback_data=f"jobadmin:view:{row['id']}")]
        for row in rows
    ]
    buttons.append([back_button("admin:downloads:all")])
    await callback.message.edit_text(
        "📋 Активные задачи" if rows else "Активных задач нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("jobadmin:view:"))
async def active_job_card(callback: CallbackQuery) -> None:
    await callback.answer()
    job = get_job(callback.data.rsplit(":", 1)[1])
    if not job:
        await callback.message.edit_text("Задача уже завершена.", reply_markup=back_kb("admin:downloads:active"))
        return
    text = (
        f"Задача {job['id']}\nСтатус: {job['status']}\nПользователь: {job['requester_id']}\n"
        f"Чат: {job['chat_id']}\nКачество: {job['requested_quality']} → {job['effective_quality'] or '—'}\n"
        f"Источник: {job['source'] or '—'}\nСоздана: {job['created_at']}"
    )
    rows = []
    if job["status"] in {"queued", "active"}:
        rows.append([InlineKeyboardButton(text="❌ Отменить задачу", callback_data=f"jobadmin:cancel:{job['id']}")])
    rows.append([back_button("admin:downloads:active")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("jobadmin:cancel:"))
async def admin_cancel_job(callback: CallbackQuery) -> None:
    await callback.answer()
    job_id = callback.data.rsplit(":", 1)[1]
    job = get_job(job_id)
    if not job:
        await callback.message.edit_text("Задача не найдена.", reply_markup=back_kb("admin:downloads:active"))
        return
    result = await download_queue.cancel(job_id, job["requester_id"])
    if result == "queued":
        finish_job(job_id, "cancelled", error_category="admin_cancelled")
    db.log_action(callback.from_user.id, "download_cancel", target=job_id)
    await callback.message.edit_text("Отмена запрошена.", reply_markup=back_kb("admin:downloads:active"))


def _moderation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚫 Пользователи ({db.count_users(blocked=True)})", callback_data="mod:users:0")],
        [InlineKeyboardButton(text=f"🚫 Группы ({db.count_groups(blocked=True)})", callback_data="mod:groups:0")],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="mod:search")],
        [back_button()],
    ])


@admin_router.callback_query(F.data == "admin:moderation")
async def moderation_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    with get_connection() as conn:
        recent = conn.execute(
            """SELECT event_type,target,created_at FROM event_log
               WHERE event_type IN ('block_user','unblock_user','block_group','unblock_group')
               ORDER BY created_at DESC LIMIT 5"""
        ).fetchall()
    lines = "\n".join(f"• {row['created_at']} · {row['event_type']} · {row['target']}" for row in recent) or "Нет действий."
    await callback.message.edit_text(f"🛡 Модерация\n\nПоследние действия:\n{lines}", reply_markup=_moderation_kb())


@admin_router.callback_query(F.data.regexp(r"^mod:(users|groups):\d+$"))
async def blocked_list(callback: CallbackQuery) -> None:
    await callback.answer()
    _, kind, page_s = callback.data.split(":")
    page = int(page_s)
    rows = db.list_users(11, page * 10, blocked=True) if kind == "users" else db.list_groups(11, page * 10, blocked=True)
    has_next = len(rows) > 10
    rows = rows[:10]
    if kind == "users":
        items = [(f"@{r['username']}" if r['username'] else str(r['telegram_id']), f"admin:users:card:{r['telegram_id']}") for r in rows]
    else:
        items = [(r['title'] or str(r['chat_id']), f"admin:groups:card:{r['chat_id']}") for r in rows]
    await callback.message.edit_text(
        "Заблокированные пользователи" if kind == "users" else "Отключённые группы",
        reply_markup=paginated_list_kb(items, page, has_next, f"mod:{kind}", "admin:moderation"),
    )


@admin_router.callback_query(F.data == "mod:search")
async def moderation_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ModerationSearch.waiting_query)
    await state.update_data(started_at=time.time())
    await callback.message.edit_text("Введите ID, username или название группы:", reply_markup=fsm_cancel_kb())


@admin_router.message(ModerationSearch.waiting_query)
async def moderation_search_result(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "moderation", state):
        return
    data = await state.get_data()
    await state.clear()
    if time.time() - data.get("started_at", 0) > 600:
        await message.answer("Состояние поиска истекло. Откройте раздел снова.")
        return
    query = message.text or ""
    users = db.search_users(query, 10)
    groups = db.search_groups(query, 10)
    rows = [[InlineKeyboardButton(text=f"👤 {u['telegram_id']} @{u['username'] or '—'}", callback_data=f"admin:users:card:{u['telegram_id']}")] for u in users]
    rows += [[InlineKeyboardButton(text=f"💬 {g['title'] or g['chat_id']}", callback_data=f"admin:groups:card:{g['chat_id']}")] for g in groups]
    rows.append([back_button("admin:moderation")])
    await message.answer("Результаты поиска:" if users or groups else "Ничего не найдено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _settings_list_kb() -> InlineKeyboardMarkup:
    runtime = app_context().runtime
    rows = [
        [InlineKeyboardButton(text=f"{spec.label}: {value}{' ↻' if spec.requires_restart else ''}", callback_data=f"aset:view:{spec.key}")]
        for spec, value in runtime.all()
    ]
    rows.append([back_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "admin:settings")
async def runtime_settings_screen(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("⚙️ Runtime-настройки\n↻ — применяется после restart", reply_markup=_settings_list_kb())


@admin_router.callback_query(F.data.startswith("aset:view:"))
async def runtime_setting_card(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    key = callback.data.split(":", 2)[2]
    runtime = app_context().runtime
    spec = runtime.specs.get(key)
    if spec is None:
        await callback.message.edit_text("Настройка не найдена.", reply_markup=back_kb("admin:settings"))
        return
    value = runtime.get(key)
    if isinstance(value, bool):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Переключить", callback_data=f"aset:toggle:{key}")],
            [back_button("admin:settings")],
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"aset:edit:{key}")],
            [back_button("admin:settings")],
        ])
    await callback.message.edit_text(f"{spec.label}\n\nТекущее значение: {value}\nПрименение: {'после restart' if spec.requires_restart else 'сразу'}", reply_markup=kb)


@admin_router.callback_query(F.data.startswith("aset:toggle:"))
async def runtime_setting_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    key = callback.data.split(":", 2)[2]
    runtime = app_context().runtime
    try:
        runtime.set(key, not bool(runtime.get(key)), actor_id=callback.from_user.id)
        db.log_action(callback.from_user.id, "setting_toggle", target=key)
    except (KeyError, ValueError) as exc:
        await callback.message.edit_text(f"Ошибка валидации: {exc}", reply_markup=back_kb("admin:settings"))
        return
    await callback.message.edit_text("Настройка обновлена.", reply_markup=back_kb("admin:settings"))


@admin_router.callback_query(F.data.startswith("aset:edit:"))
async def runtime_setting_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    key = callback.data.split(":", 2)[2]
    if key not in app_context().runtime.specs:
        return
    await state.set_state(RuntimeSettingEdit.waiting_value)
    await state.update_data(setting_key=key, started_at=time.time())
    await callback.message.edit_text("Введите новое значение:", reply_markup=fsm_cancel_kb())


@admin_router.message(RuntimeSettingEdit.waiting_value)
async def runtime_setting_edit_finish(message: Message, state: FSMContext, bot) -> None:
    if not await require_admin_permission(message, bot, "settings", state):
        return
    data = await state.get_data()
    await state.clear()
    if time.time() - data.get("started_at", 0) > 600:
        await message.answer("Состояние редактирования истекло.")
        return
    key = data["setting_key"]
    try:
        value = app_context().runtime.set(key, message.text or "", actor_id=message.from_user.id)
        db.log_action(message.from_user.id, "setting_change", target=key)
        spec = app_context().runtime.specs[key]
        suffix = " Изменение вступит в силу после restart." if spec.requires_restart else ""
        await message.answer(f"✅ {spec.label}: {value}.{suffix}", reply_markup=back_kb("admin:settings"))
    except (KeyError, ValueError) as exc:
        await message.answer(f"❌ Некорректное значение: {exc}", reply_markup=back_kb("admin:settings"))


_health_cache: tuple[float, str] | None = None


async def _command_version(*args: str) -> str:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        return output.decode(errors="replace").splitlines()[0][:120] if process.returncode == 0 else "unavailable"
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except asyncio.TimeoutError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return "unavailable"
    except Exception:
        return "unavailable"


def _directory_size(path: Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                pass
    return total


async def _health_text(force: bool = False) -> str:
    global _health_cache
    if _health_cache and not force and time.monotonic() - _health_cache[0] < 60:
        return _health_cache[1]
    context = app_context()
    yt_dlp, ffmpeg, deno = await asyncio.gather(
        _command_version("yt-dlp", "--version"),
        _command_version("ffmpeg", "-version"),
        _command_version("deno", "--version"),
    )
    disk = shutil.disk_usage(context.bootstrap.data_dir)
    reserve = int(context.runtime.get("disk_min_free_mb")) * 1024 * 1024
    disk_icon = "🔴" if disk.free < reserve else "🟡" if disk.free < reserve * 2 else "🟢"
    try:
        with get_connection() as conn:
            db_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except Exception:
        db_ok = False
    storages = context.storage.list_storages(enabled_only=True)
    storage_ok = sum(bool(row["healthy"]) for row in storages)
    mode = "Local Bot API" if context.bootstrap.telegram_api_is_local else "Cloud Bot API"
    configured_workers = int(context.runtime.get("queue_workers"))
    running_workers = sum(state == "running" for state in context.worker_states.values())
    restarting_workers = sum(state == "restarting" for state in context.worker_states.values())
    worker_icon = "🟢" if running_workers == configured_workers else "🟡" if running_workers else "🔴"
    text = (
        f"🖥 Система · {context.bootstrap.app_version}\n\n"
        f"{'🟢' if context.polling else '🔴'} Polling\n"
        f"{worker_icon} Workers: {running_workers}/{configured_workers} running · restarting {restarting_workers} · active {download_queue.active_count} · queue {download_queue.queued_count}\n"
        f"{'🟢' if db_ok else '🔴'} SQLite · {get_database_path().stat().st_size / 1024 / 1024:.1f} MB\n"
        f"{disk_icon} Disk free: {disk.free / 1024 / 1024 / 1024:.1f} GB\n"
        f"🟢 Temp: {_directory_size(context.bootstrap.temp_dir) / 1024 / 1024:.1f} MB\n"
        f"{'🟢' if yt_dlp != 'unavailable' else '🔴'} yt-dlp: {yt_dlp}\n"
        f"{'🟢' if ffmpeg != 'unavailable' else '🔴'} FFmpeg: {ffmpeg}\n"
        f"{'🟢' if deno != 'unavailable' else '🔴'} Deno: {deno}\n"
        f"🟢 Telegram: {mode} · limit {context.runtime.get('max_delivery_mb')} MB\n"
        f"{'🟢' if storage_ok == len(storages) and storages else '🟡'} Storage: {storage_ok}/{len(storages)}"
    )
    _health_cache = (time.monotonic(), text)
    return text


@admin_router.callback_query(F.data.in_({"admin:system", "admin:system:refresh"}))
async def system_health(callback: CallbackQuery) -> None:
    await callback.answer()
    text = await _health_text(force=callback.data.endswith("refresh"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:system:refresh")],
        [back_button()],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@admin_router.callback_query(F.data.regexp(r"^admin:log:\d+$"))
async def audit_log(callback: CallbackQuery) -> None:
    await callback.answer()
    page = int(callback.data.rsplit(":", 1)[1])
    rows = db.list_actions(11, page * 10)
    has_next = len(rows) > 10
    rows = rows[:10]
    text = "📋 Журнал действий\n\n" + ("\n".join(
        f"{row['created_at']} · {row['event_type']} · actor={row['actor_id'] or 'system'} · {row['target'] or '—'}"
        for row in rows
    ) or "Записей пока нет.")
    kb_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:log:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:log:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([back_button()])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
