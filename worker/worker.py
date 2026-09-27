import asyncio
from aiogram import Bot
from aiogram.types import FSInputFile

from app.services.downloader import Downloader, DownloadError
from app.services.queue import QueueItem, download_queue

downloader = Downloader()


async def process_download(bot: Bot, item: QueueItem):
    try:
        status = await bot.send_message(
            item.user_id,
            "⏳ Начинаю скачивание..."
        )

        result = await downloader.download(
            item.url,
            quality="auto"
        )

        await status.edit_text("📤 Видео скачано. Отправляю...")

        video = FSInputFile(result.path)

        await bot.send_video(
            item.user_id,
            video=video,
            caption=f"🎬 {result.title}",
            supports_streaming=True
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
