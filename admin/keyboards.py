from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin.auth import has_permission

SECTIONS = [
    ("dashboard", "📊 Дашборд", "admin:dashboard"),
    ("downloads", "📥 Загрузки", "admin:downloads:all"),
    ("users", "👥 Пользователи", "admin:users:0"),
    ("groups", "💬 Группы", "admin:groups:0"),
    ("moderation", "🛡 Модерация", "admin:moderation"),
    ("storage", "💾 Хранилище", "admin:storage"),
    ("broadcast", "📢 Рассылки", "admin:broadcast"),
    ("buttons", "🔘 Inline-кнопки", "admin:buttons"),
    ("settings", "⚙️ Настройки", "admin:settings"),
    ("system", "🖥 Система", "admin:system"),
    ("log", "📋 Журнал действий", "admin:log:0"),
    ("manage_admins", "👑 Администраторы", "admin:admins"),
]


def main_menu_kb(role: str = "owner") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=callback)]
            for permission, label, callback in SECTIONS
            if has_permission(role, permission)
        ]
    )


def back_button(to: str = "admin:main") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=to)


def back_kb(to: str = "admin:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[back_button(to)]])


def fsm_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin:fsm:cancel")
        ]]
    )


def confirm_kb(action: str, payload: str, back_to: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:confirm:{action}:{payload}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=back_to),
        ]]
    )


def paginated_list_kb(
    items: list[tuple[str, str]],
    page: int,
    has_next: bool,
    list_callback_prefix: str,
    back_to: str = "admin:main",
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text[:60], callback_data=callback)] for text, callback in items]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"{list_callback_prefix}:{page - 1}"))
    if has_next:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"{list_callback_prefix}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([back_button(back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
