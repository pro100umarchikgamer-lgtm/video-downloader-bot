from __future__ import annotations

import re

from aiogram import Router, F
from aiogram.types import FSInputFile

from app.services.downloader import Downloader, DownloadError
from app.services.queue import (
    MAX_BATCH_SIZE,
    MAX_USER_QUEUE,
    QueueItem,
    download_queue,
)


router = Router()
downloader = Downloader()


URL_RE = re.compile(
    r"https?://[^\s<>\"]+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """
    Находит URL в сообщении и удаляет дубликаты,
    сохраняя исходный порядок.
    """
    found = URL_RE.findall(text)

    result = []
    seen = set()

    for url in found:
        url = url.rstrip(".,!?;:)")

        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


@router.message(F.text)
async def handle_video_links(message):
    text = message.text or ""
    urls = extract_urls(text)

    if not urls:
        return

    user_id = message.from_user.id

    # Защита от слишком частых отдельных сообщений.
    allowed, remaining = await download_queue.check_message_allowed(
        user_id
    )

    if not allowed:
        await message.answer(
            f"⏳ Слишком быстро.\n"
            f"Подожди ещё примерно {remaining} сек. "
            f"перед следующей отправкой."
        )
        return

    # Максимальный размер одного batch.
    if len(urls) > MAX_BATCH_SIZE:
        await message.answer(
            f"❌ В одном сообщении можно отправить "
            f"не более {MAX_BATCH_SIZE} ссылок.\n\n"
            f"Ты отправил: {len(urls)}"
        )
        return

    # Проверяем персональную очередь пользователя.
    allowed, available = await download_queue.can_add(
        user_id,
        len(urls),
    )

    if not allowed:
        if available == 0:
            await message.answer(
                f"⏳ У тебя уже максимальная очередь "
                f"({MAX_USER_QUEUE} задач).\n"
                f"Дождись завершения текущих загрузок."
            )
        else:
            await message.answer(
                f"❌ Можно добавить ещё максимум "
                f"{available} задач."
            )
        return

    # Добавляем каждую ссылку как отдельную задачу.
    for url in urls:
        await download_queue.put(
            QueueItem(
                user_id=user_id,
                url=url,
            )
        )

    if len(urls) == 1:
        await message.answer(
            "📥 Ссылка принята и добавлена в очередь."
        )
    else:
        await message.answer(
            f"📥 Принято ссылок: {len(urls)}\n"
            f"Все добавлены в очередь."
        )


async def process_download(item: QueueItem) -> None:
    """
    Обработчик одной задачи очереди.
    """
    # Пока отправляем результат непосредственно пользователю
    # через сохранённый Bot instance будет подключено
    # следующим шагом.
    #
    # Здесь оставляем функцию как основу очереди.
    pass
