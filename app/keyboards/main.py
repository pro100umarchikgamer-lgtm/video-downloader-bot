from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.i18n import LOCALE_LABELS, SUPPORTED_LOCALES, quality_label, t
from app.runtime_config import QUALITIES


def settings_keyboard(locale: str, *, group: bool = False, quality_enabled: bool = True) -> InlineKeyboardMarkup:
    prefix = "gset" if group else "uset"
    rows = [[InlineKeyboardButton(text=f"🌐 {t(locale, 'language')}", callback_data=f"{prefix}:language")]]
    if quality_enabled:
        rows.append([InlineKeyboardButton(text=f"🎬 {t(locale, 'quality')}", callback_data=f"{prefix}:quality")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard(locale: str, *, group: bool = False) -> InlineKeyboardMarkup:
    prefix = "gset" if group else "uset"
    rows = [
        [InlineKeyboardButton(text=LOCALE_LABELS[value], callback_data=f"{prefix}:lang:{value}")]
        for value in SUPPORTED_LOCALES
    ]
    rows.append([InlineKeyboardButton(text=f"⬅️ {t(locale, 'back')}", callback_data=f"{prefix}:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quality_keyboard(locale: str, *, group: bool = False, available: list[int] | None = None) -> InlineKeyboardMarkup:
    prefix = "gset" if group else "uset"
    values = ["auto", *(str(q) for q in (360, 480, 720, 1080, 1440, 2160))]
    if available is not None:
        values = ["auto", *(str(q) for q in available)]
    rows = [
        [InlineKeyboardButton(text=quality_label(locale, value), callback_data=f"{prefix}:quality:{value}")]
        for value in values if value in QUALITIES
    ]
    rows.append([InlineKeyboardButton(text=f"⬅️ {t(locale, 'back')}", callback_data=f"{prefix}:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard(locale: str, job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"❌ {t(locale, 'cancel')}", callback_data=f"job:cancel:{job_id}")]]
    )


def retry_keyboard(locale: str, job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"🔄 {t(locale, 'retry')}", callback_data=f"job:retry:{job_id}")]]
    )
