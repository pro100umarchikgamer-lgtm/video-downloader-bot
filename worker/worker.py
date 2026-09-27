import os

from aiogram import Bot
from aiogram.types import FSInputFile

from app.database import get_cached_video, save_cached_video
from app.services.cache import get_content_id
from app.services.downloader import Downloader, DownloadError
from app.services.queue import QueueItem, download_queue

downloader = Downloader()


def get_cache_type(filesize: int) -> str:
    mb = filesize / 1024 / 1024

    if mb <= 10:
        return "small"
    if mb <= 45:
        return "medium"
    return "large"


async def process_download(bot: Bot, item: QueueItem):
    try:
        status = await bot.send_message(
            item.user_id,
            "🔎 Проверяю кэш..."
        )

        content_id = get_content_id(item.url)

        for quality in (1440, 1080, 720, 480, 360):
            cached = get_cached_video(content_id, quality)

            if cached and cached["telegram_file_id"]:
                await status.edit_text(
                    f"⚡ Нашёл видео в кэше ({quality}p). Отправляю..."
                )

                await bot.send_video(
                    item.user_id,
                    video=cached["telegram_file_id"],
                    caption=f"🎬 Кэш: {quality}p",
                    supports_streaming=True
                )

                await status.delete()
                return

        await status.edit_text("⏳ Начинаю скачивание...")

        result = await downloader.download(
            item.url,
            quality="auto"
        )

        await status.edit_text("📤 Видео скачано. Отправляю...")

        video = FSInputFile(result.path)

        sent_message = await bot.send_video(
            item.user_id,
            video=video,
            caption=f"🎬 {result.title}",
            supports_streaming=True
        )

        telegram_file_id = sent_message.video.file_id

        save_cached_video(
            content_id=content_id,
            source="yt-dlp",
            original_url=item.url,
            quality=result.quality,
            filesize=result.filesize,
            cache_type=get_cache_type(result.filesize),
            telegram_chat_id=item.user_id,
            telegram_message_id=sent_message.message_id,
            telegram_file_id=telegram_file_id
        )

        Downloader.cleanup(result.path)
        await status.delete()

    except DownloadError as exc:
        await bot.send_message(
            item.user_id,
            f"❌ Не удалось скачать видео:\n{exc}"
        )

    except Exception as exc:
        await bot.send_message(
            item.user_id,
            f"❌ Ошибка обработки:\n{exc}"
        )


async def worker_loop(bot: Bot):
    await download_queue.run(
        lambda item: process_download(bot, item)
    )
