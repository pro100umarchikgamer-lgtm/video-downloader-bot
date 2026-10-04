from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from admin import db
from app.context import app_context
from app.i18n import LOCALE_LABELS, quality_label, t
from app.keyboards.main import language_keyboard, quality_keyboard, settings_keyboard

router = Router(name="settings")


async def _is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"creator", "administrator"}
    except Exception:
        return False


def _user_settings_text(user_id: int, locale: str) -> str:
    quality = db.get_user_quality(user_id, app_context().runtime.get("private_default_quality"))
    return t(locale, "settings_title", language=LOCALE_LABELS[locale], quality=quality_label(locale, quality))


def _settings_kb(locale: str, *, group: bool = False):
    return settings_keyboard(locale, group=group, quality_enabled=bool(app_context().runtime.get("quality_selection_enabled")))


def _group_settings_text(chat_id: int, locale: str) -> str:
    quality = db.get_group_quality(chat_id, app_context().runtime.get("group_default_quality"))
    return t(locale, "group_settings_title", language=LOCALE_LABELS[locale], quality=quality_label(locale, quality))


@router.message(Command("settings"))
async def settings_command(message: Message, bot) -> None:
    if not message.from_user:
        return
    if message.chat.type == "private":
        locale = db.get_user_locale(message.from_user.id, message.from_user.language_code)
        await message.answer(_user_settings_text(message.from_user.id, locale), reply_markup=_settings_kb(locale))
        return
    locale = db.get_group_locale(message.chat.id)
    if not await _is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.answer(t(locale, "group_settings_denied"))
        return
    await message.answer(_group_settings_text(message.chat.id, locale), reply_markup=_settings_kb(locale, group=True))


@router.callback_query(F.data == "uset:main")
async def user_settings_main(callback: CallbackQuery) -> None:
    await callback.answer()
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if callback.message:
        await callback.message.edit_text(_user_settings_text(callback.from_user.id, locale), reply_markup=_settings_kb(locale))


@router.callback_query(F.data == "uset:language")
async def user_language_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if callback.message:
        await callback.message.edit_text(t(locale, "language_title"), reply_markup=language_keyboard(locale))


@router.callback_query(F.data.startswith("uset:lang:"))
async def user_set_language(callback: CallbackQuery) -> None:
    await callback.answer()
    locale = callback.data.split(":", 2)[2]
    if not db.set_user_locale(callback.from_user.id, locale):
        locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if callback.message:
        await callback.message.edit_text(_user_settings_text(callback.from_user.id, locale), reply_markup=_settings_kb(locale))


@router.callback_query(F.data == "uset:quality")
async def user_quality_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if not app_context().runtime.get("quality_selection_enabled"):
        return
    if callback.message:
        await callback.message.edit_text(t(locale, "quality_title"), reply_markup=quality_keyboard(locale))


@router.callback_query(F.data.startswith("uset:quality:"))
async def user_set_quality(callback: CallbackQuery) -> None:
    await callback.answer()
    quality = callback.data.split(":", 2)[2]
    locale = db.get_user_locale(callback.from_user.id, callback.from_user.language_code)
    if not app_context().runtime.get("quality_selection_enabled"):
        return
    db.set_user_quality(callback.from_user.id, quality)
    if callback.message:
        await callback.message.edit_text(_user_settings_text(callback.from_user.id, locale), reply_markup=_settings_kb(locale))


async def _validate_group_callback(callback: CallbackQuery, bot) -> tuple[int, str] | None:
    await callback.answer()
    if not callback.message or callback.message.chat.type == "private":
        return None
    chat_id = callback.message.chat.id
    locale = db.get_group_locale(chat_id)
    if not await _is_group_admin(bot, chat_id, callback.from_user.id):
        if callback.message:
            await callback.message.answer(t(locale, "group_settings_denied"))
        return None
    return chat_id, locale


@router.callback_query(F.data == "gset:main")
async def group_settings_main(callback: CallbackQuery, bot) -> None:
    result = await _validate_group_callback(callback, bot)
    if result and callback.message:
        chat_id, locale = result
        await callback.message.edit_text(_group_settings_text(chat_id, locale), reply_markup=_settings_kb(locale, group=True))


@router.callback_query(F.data == "gset:language")
async def group_language_menu(callback: CallbackQuery, bot) -> None:
    result = await _validate_group_callback(callback, bot)
    if result and callback.message:
        _, locale = result
        await callback.message.edit_text(t(locale, "language_title"), reply_markup=language_keyboard(locale, group=True))


@router.callback_query(F.data.startswith("gset:lang:"))
async def group_set_language(callback: CallbackQuery, bot) -> None:
    result = await _validate_group_callback(callback, bot)
    if result and callback.message:
        chat_id, old_locale = result
        locale = callback.data.split(":", 2)[2]
        if not db.set_group_locale(chat_id, locale):
            locale = old_locale
        await callback.message.edit_text(_group_settings_text(chat_id, locale), reply_markup=_settings_kb(locale, group=True))


@router.callback_query(F.data == "gset:quality")
async def group_quality_menu(callback: CallbackQuery, bot) -> None:
    result = await _validate_group_callback(callback, bot)
    if result and callback.message:
        _, locale = result
        if not app_context().runtime.get("quality_selection_enabled"):
            return
        await callback.message.edit_text(t(locale, "quality_title"), reply_markup=quality_keyboard(locale, group=True))


@router.callback_query(F.data.startswith("gset:quality:"))
async def group_set_quality(callback: CallbackQuery, bot) -> None:
    result = await _validate_group_callback(callback, bot)
    if result and callback.message:
        chat_id, locale = result
        if not app_context().runtime.get("quality_selection_enabled"):
            return
        quality = callback.data.split(":", 2)[2]
        db.set_group_quality(chat_id, quality)
        await callback.message.edit_text(_group_settings_text(chat_id, locale), reply_markup=_settings_kb(locale, group=True))
