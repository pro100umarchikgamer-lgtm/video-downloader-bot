"""Compose system and administrator-configured buttons under delivered media."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin.buttons import db
from app.i18n import t

ADD_TO_GROUP_TEXT = "➕ Добавить бота в группу"
_bot_username_cache: str | None = None
logger = logging.getLogger(__name__)


def is_valid_button_url(url: str) -> bool:
    if not 1 <= len(url) <= 2048 or any(character.isspace() for character in url):
        return False
    try:
        parsed = urlsplit(url)
        # Accessing .port also validates malformed bracket/port syntax.
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


async def _get_bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username or ""
    return _bot_username_cache


async def build_video_keyboard(
    bot: Bot,
    *,
    locale: str = "ru",
    video_note_token: str | None = None,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if video_note_token:
        rows.append([
            InlineKeyboardButton(
                text=f"⭕ {t(locale, 'video_note')}",
                callback_data=f"vn:{video_note_token}",
            )
        ])
    add_enabled = True
    for button in db.list_buttons(scope="downloads", enabled_only=True):
        system_key = button["system_key"] if "system_key" in button.keys() else None
        if system_key == "add_to_group" or button["text"] == ADD_TO_GROUP_TEXT:
            add_enabled = True
            continue
        if not 1 <= len(button["text"]) <= 64 or not is_valid_button_url(button["url"]):
            logger.warning("Skipping invalid configured inline button id=%s", button["id"])
            continue
        rows.append([InlineKeyboardButton(text=button["text"], url=button["url"])])
    marker = _get_add_to_group_record()
    if marker is not None:
        add_enabled = bool(marker["enabled"])
    if add_enabled:
        username = await _get_bot_username(bot)
        if username:
            rows.append([
                InlineKeyboardButton(
                    text=f"➕ {t(locale, 'add_to_group')}",
                    url=f"https://t.me/{username}?startgroup=true",
                )
            ])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _get_add_to_group_record():
    for button in db.list_buttons(scope="downloads", enabled_only=False):
        system_key = button["system_key"] if "system_key" in button.keys() else None
        if system_key == "add_to_group" or button["text"] == ADD_TO_GROUP_TEXT:
            return button
    return None


def ensure_add_to_group_entry() -> None:
    if _get_add_to_group_record() is not None:
        return
    button_id = db.create_button(ADD_TO_GROUP_TEXT, url="https://t.me/", scope="downloads")
    from app.database import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE inline_buttons SET system_key='add_to_group' WHERE id=?", (button_id,))
        conn.commit()
