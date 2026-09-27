import re

from aiogram import Router, F
from aiogram.types import FSInputFile

from app.services.downloader import Downloader, DownloadError

router = Router()
downloader = Downloader()

URL_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE)


@router.message(F.text.regexp(URL_RE))
async def handle_video_link(message):
    url = message.text.strip()

    status = await message.answer("⏳ Получил ссылку. Начинаю скачивание...")

    try:
        result = await downloader.download(url, quality="720", max_filesize=1024 * 1024 * 1024)

        await status.edit_text("📤 Видео скачано. Отправляю в Telegram...")

        video = FSInputFile(result.path)

        await message.answer_video(
            video=video,
            caption=f"🎬 {result.title}",
            supports_streaming=True,
        )

        Downloader.cleanup(result.path)

        await status.delete()

    except DownloadError as exc:
        await status.edit_text(f"❌ Не удалось скачать видео:\n{exc}")

    except Exception as exc:
        await status.edit_text(f"❌ Ошибка:\n{exc}")
