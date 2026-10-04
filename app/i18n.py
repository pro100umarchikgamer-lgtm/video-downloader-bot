"""Deterministic local translations for all ordinary-user UI."""

from __future__ import annotations

import logging
import string
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "ru"
SUPPORTED_LOCALES = ("ru", "kk", "uz_latn", "uz_cyrl", "en")
LOCALE_LABELS = {
    "ru": "🇷🇺 Русский",
    "kk": "🇰🇿 Қазақша",
    "uz_latn": "🇺🇿 O‘zbekcha",
    "uz_cyrl": "🇺🇿 Ўзбекча",
    "en": "🇬🇧 English",
}

_RU = {
    "cmd_start": "Начать работу",
    "cmd_settings": "Настройки",
    "start": "Отправьте ссылку на видео — я помогу скачать его.\n\nНастройки: /settings",
    "settings_title": "Настройки\n\nЯзык: {language}\nКачество по умолчанию: {quality}",
    "language": "Язык",
    "language_title": "Выберите язык интерфейса:",
    "language_saved": "Язык изменён.",
    "quality": "Качество по умолчанию",
    "quality_title": "Выберите качество по умолчанию:",
    "quality_saved": "Качество по умолчанию: {quality}",
    "quality_auto": "Авто",
    "back": "Назад",
    "cancel": "Отменить",
    "retry": "Повторить",
    "processing": "⏳ Обрабатываю видео…",
    "downloading": "⬇️ Загружаю видео…",
    "sending": "📤 Отправляю видео…",
    "accepted_one": "Ссылка принята.",
    "accepted_many": "Принято ссылок: {count}.",
    "batch_invalid": "Пропущено некорректных ссылок: {count}.",
    "batch_limit": "В одном сообщении можно отправить не более {limit} ссылок.",
    "queue_full": "Ваша очередь заполнена. Дождитесь завершения текущих загрузок.",
    "queue_partial": "Сейчас можно добавить ещё не более {available} ссылок.",
    "rate_limit": "Слишком много запросов. Попробуйте снова через {seconds} сек.",
    "maintenance": "Сервис временно на обслуживании. Попробуйте позже.",
    "blocked": "Доступ к загрузкам ограничен.",
    "group_disabled": "Загрузки в этой группе отключены.",
    "invalid_link": "Отправьте корректную публичную ссылку на видео.",
    "unsafe_link": "Эту ссылку нельзя обработать.",
    "unsupported": "Эта ссылка не поддерживается.",
    "unavailable": "Видео недоступно или было удалено.",
    "private_media": "Это видео недоступно без входа или разрешения владельца.",
    "quality_unavailable": "Выбранное качество недоступно для этого видео.",
    "too_large": "Файл слишком большой для текущего способа доставки.",
    "temporary_error": "Временная ошибка при обработке. Попробуйте позже.",
    "delivery_error": "Не удалось доставить видео. Попробуйте позже.",
    "disk_pressure": "Сервис временно перегружен. Попробуйте позже.",
    "cancelled": "Загрузка отменена.",
    "cancel_requested": "Отменяю загрузку…",
    "already_finished": "Эта загрузка уже завершена.",
    "stale_action": "Эта кнопка уже неактуальна.",
    "not_yours": "Эта кнопка относится к запросу другого пользователя.",
    "video_note": "Сделать кружочек",
    "video_note_processing": "⏳ Делаю кружочек…",
    "video_note_busy": "Кружочек уже создаётся.",
    "video_note_ready": "Кружочек готов.",
    "video_note_failed": "Не удалось сделать кружочек из этого видео.",
    "add_to_group": "Добавить бота в группу",
    "group_settings_denied": "Изменять настройки группы могут только её администраторы.",
    "group_settings_title": "Настройки группы\n\nЯзык: {language}\nКачество: {quality}",
    "group_language_saved": "Язык группы изменён.",
    "group_quality_saved": "Качество группы: {quality}",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": _RU,
    "kk": {
        "cmd_start": "Жұмысты бастау", "cmd_settings": "Баптаулар",
        "start": "Видео сілтемесін жіберіңіз — оны жүктеп беруге көмектесемін.\n\nБаптаулар: /settings",
        "settings_title": "Баптаулар\n\nТіл: {language}\nӘдепкі сапа: {quality}",
        "language": "Тіл", "language_title": "Интерфейс тілін таңдаңыз:", "language_saved": "Тіл өзгертілді.",
        "quality": "Әдепкі сапа", "quality_title": "Әдепкі видео сапасын таңдаңыз:", "quality_saved": "Әдепкі сапа: {quality}", "quality_auto": "Авто",
        "back": "Артқа", "cancel": "Бас тарту", "retry": "Қайталау",
        "processing": "⏳ Видеоны өңдеп жатырмын…", "downloading": "⬇️ Видеоны жүктеп жатырмын…", "sending": "📤 Видеоны жіберіп жатырмын…",
        "accepted_one": "Сілтеме қабылданды.", "accepted_many": "Қабылданған сілтемелер: {count}.",
        "batch_invalid": "Жарамсыз сілтемелер өткізіліп жіберілді: {count}.",
        "batch_limit": "Бір хабарламада ең көбі {limit} сілтеме жіберуге болады.",
        "queue_full": "Кезегіңіз толы. Ағымдағы жүктеулердің аяқталуын күтіңіз.", "queue_partial": "Қазір тағы ең көбі {available} сілтеме қосуға болады.",
        "rate_limit": "Сұрау тым көп. {seconds} секундтан кейін қайталап көріңіз.",
        "maintenance": "Қызмет уақытша техникалық қызмет көрсетуде. Кейінірек көріңіз.", "blocked": "Жүктеулерге қолжетімділік шектелген.", "group_disabled": "Бұл топта жүктеулер өшірілген.",
        "invalid_link": "Жарамды ашық видео сілтемесін жіберіңіз.", "unsafe_link": "Бұл сілтемені өңдеу мүмкін емес.", "unsupported": "Бұл сілтемеге қолдау көрсетілмейді.",
        "unavailable": "Видео қолжетімсіз немесе жойылған.", "private_media": "Бұл видеоға кіру немесе иесінің рұқсаты қажет.",
        "quality_unavailable": "Таңдалған сапа бұл видеода жоқ.", "too_large": "Файл ағымдағы жеткізу тәсілі үшін тым үлкен.",
        "temporary_error": "Өңдеу кезінде уақытша қате болды. Кейінірек көріңіз.", "delivery_error": "Видеоны жеткізу мүмкін болмады. Кейінірек көріңіз.", "disk_pressure": "Қызмет уақытша шамадан тыс жүктелген. Кейінірек көріңіз.",
        "cancelled": "Жүктеу тоқтатылды.", "cancel_requested": "Жүктеуді тоқтатып жатырмын…", "already_finished": "Бұл жүктеу аяқталған.", "stale_action": "Бұл батырма енді өзекті емес.", "not_yours": "Бұл батырма басқа пайдаланушының сұрауына тиесілі.",
        "video_note": "Дөңгелек видео жасау", "video_note_processing": "⏳ Дөңгелек видео жасап жатырмын…", "video_note_busy": "Дөңгелек видео жасалып жатыр.", "video_note_ready": "Дөңгелек видео дайын.", "video_note_failed": "Бұл видеодан дөңгелек видео жасау мүмкін болмады.",
        "add_to_group": "Ботты топқа қосу", "group_settings_denied": "Топ баптауларын тек оның әкімшілері өзгерте алады.",
        "group_settings_title": "Топ баптаулары\n\nТіл: {language}\nСапа: {quality}", "group_language_saved": "Топ тілі өзгертілді.", "group_quality_saved": "Топ сапасы: {quality}",
    },
    "uz_latn": {
        "cmd_start": "Ishni boshlash", "cmd_settings": "Sozlamalar",
        "start": "Video havolasini yuboring — uni yuklab olishga yordam beraman.\n\nSozlamalar: /settings",
        "settings_title": "Sozlamalar\n\nTil: {language}\nStandart sifat: {quality}",
        "language": "Til", "language_title": "Interfeys tilini tanlang:", "language_saved": "Til o‘zgartirildi.",
        "quality": "Standart sifat", "quality_title": "Standart video sifatini tanlang:", "quality_saved": "Standart sifat: {quality}", "quality_auto": "Avto",
        "back": "Orqaga", "cancel": "Bekor qilish", "retry": "Qayta urinish",
        "processing": "⏳ Video qayta ishlanmoqda…", "downloading": "⬇️ Video yuklanmoqda…", "sending": "📤 Video yuborilmoqda…",
        "accepted_one": "Havola qabul qilindi.", "accepted_many": "Qabul qilingan havolalar: {count}.",
        "batch_invalid": "Noto‘g‘ri havolalar o‘tkazib yuborildi: {count}.",
        "batch_limit": "Bitta xabarda ko‘pi bilan {limit} ta havola yuborish mumkin.", "queue_full": "Navbatingiz to‘la. Joriy yuklamalar tugashini kuting.", "queue_partial": "Hozir yana ko‘pi bilan {available} ta havola qo‘shish mumkin.",
        "rate_limit": "So‘rovlar juda ko‘p. {seconds} soniyadan keyin qayta urinib ko‘ring.", "maintenance": "Xizmatda vaqtincha texnik ishlar olib borilmoqda. Keyinroq urinib ko‘ring.", "blocked": "Yuklab olishga ruxsat cheklangan.", "group_disabled": "Bu guruhda yuklab olish o‘chirilgan.",
        "invalid_link": "To‘g‘ri ochiq video havolasini yuboring.", "unsafe_link": "Bu havolani qayta ishlab bo‘lmaydi.", "unsupported": "Bu havola qo‘llab-quvvatlanmaydi.", "unavailable": "Video mavjud emas yoki o‘chirilgan.", "private_media": "Bu videoga kirish yoki egasining ruxsati kerak.", "quality_unavailable": "Tanlangan sifat bu videoda mavjud emas.", "too_large": "Fayl joriy yetkazish usuli uchun juda katta.",
        "temporary_error": "Qayta ishlashda vaqtinchalik xato yuz berdi. Keyinroq urinib ko‘ring.", "delivery_error": "Videoni yetkazib bo‘lmadi. Keyinroq urinib ko‘ring.", "disk_pressure": "Xizmat vaqtincha band. Keyinroq urinib ko‘ring.",
        "cancelled": "Yuklab olish bekor qilindi.", "cancel_requested": "Yuklab olish bekor qilinmoqda…", "already_finished": "Bu yuklab olish tugagan.", "stale_action": "Bu tugma endi amal qilmaydi.", "not_yours": "Bu tugma boshqa foydalanuvchining so‘roviga tegishli.",
        "video_note": "Dumaloq video qilish", "video_note_processing": "⏳ Dumaloq video tayyorlanmoqda…", "video_note_busy": "Dumaloq video allaqachon tayyorlanmoqda.", "video_note_ready": "Dumaloq video tayyor.", "video_note_failed": "Bu videodan dumaloq video tayyorlab bo‘lmadi.",
        "add_to_group": "Botni guruhga qo‘shish", "group_settings_denied": "Guruh sozlamalarini faqat uning administratorlari o‘zgartira oladi.", "group_settings_title": "Guruh sozlamalari\n\nTil: {language}\nSifat: {quality}", "group_language_saved": "Guruh tili o‘zgartirildi.", "group_quality_saved": "Guruh sifati: {quality}",
    },
    "uz_cyrl": {
        "cmd_start": "Ишни бошлаш", "cmd_settings": "Созламалар",
        "start": "Видео ҳаволасини юборинг — уни юклаб олишга ёрдам бераман.\n\nСозламалар: /settings",
        "settings_title": "Созламалар\n\nТил: {language}\nСтандарт сифат: {quality}",
        "language": "Тил", "language_title": "Интерфейс тилини танланг:", "language_saved": "Тил ўзгартирилди.",
        "quality": "Стандарт сифат", "quality_title": "Стандарт видео сифатини танланг:", "quality_saved": "Стандарт сифат: {quality}", "quality_auto": "Авто",
        "back": "Орқага", "cancel": "Бекор қилиш", "retry": "Қайта уриниш",
        "processing": "⏳ Видео қайта ишланмоқда…", "downloading": "⬇️ Видео юкланмоқда…", "sending": "📤 Видео юборилмоқда…",
        "accepted_one": "Ҳавола қабул қилинди.", "accepted_many": "Қабул қилинган ҳаволалар: {count}.",
        "batch_invalid": "Нотўғри ҳаволалар ўтказиб юборилди: {count}.",
        "batch_limit": "Битта хабарда кўпи билан {limit} та ҳавола юбориш мумкин.", "queue_full": "Навбатингиз тўла. Жорий юкламалар тугашини кутинг.", "queue_partial": "Ҳозир яна кўпи билан {available} та ҳавола қўшиш мумкин.",
        "rate_limit": "Сўровлар жуда кўп. {seconds} сониядан кейин қайта уриниб кўринг.", "maintenance": "Хизматда вақтинча техник ишлар олиб борилмоқда. Кейинроқ уриниб кўринг.", "blocked": "Юклаб олишга рухсат чекланган.", "group_disabled": "Бу гуруҳда юклаб олиш ўчирилган.",
        "invalid_link": "Тўғри очиқ видео ҳаволасини юборинг.", "unsafe_link": "Бу ҳаволани қайта ишлаб бўлмайди.", "unsupported": "Бу ҳавола қўллаб-қувватланмайди.", "unavailable": "Видео мавжуд эмас ёки ўчирилган.", "private_media": "Бу видеога кириш ёки эгасининг рухсати керак.", "quality_unavailable": "Танланган сифат бу видеода мавжуд эмас.", "too_large": "Файл жорий етказиш усули учун жуда катта.",
        "temporary_error": "Қайта ишлашда вақтинча хато юз берди. Кейинроқ уриниб кўринг.", "delivery_error": "Видеони етказиб бўлмади. Кейинроқ уриниб кўринг.", "disk_pressure": "Хизмат вақтинча банд. Кейинроқ уриниб кўринг.",
        "cancelled": "Юклаб олиш бекор қилинди.", "cancel_requested": "Юклаб олиш бекор қилинмоқда…", "already_finished": "Бу юклаб олиш тугаган.", "stale_action": "Бу тугма энди амал қилмайди.", "not_yours": "Бу тугма бошқа фойдаланувчининг сўровига тегишли.",
        "video_note": "Думалоқ видео қилиш", "video_note_processing": "⏳ Думалоқ видео тайёрланмоқда…", "video_note_busy": "Думалоқ видео аллақачон тайёрланмоқда.", "video_note_ready": "Думалоқ видео тайёр.", "video_note_failed": "Бу видеодан думалоқ видео тайёрлаб бўлмади.",
        "add_to_group": "Ботни гуруҳга қўшиш", "group_settings_denied": "Гуруҳ созламаларини фақат унинг администраторлари ўзгартира олади.", "group_settings_title": "Гуруҳ созламалари\n\nТил: {language}\nСифат: {quality}", "group_language_saved": "Гуруҳ тили ўзгартирилди.", "group_quality_saved": "Гуруҳ сифати: {quality}",
    },
    "en": {
        "cmd_start": "Start", "cmd_settings": "Settings",
        "start": "Send me a video link and I’ll help you download it.\n\nSettings: /settings",
        "settings_title": "Settings\n\nLanguage: {language}\nDefault quality: {quality}",
        "language": "Language", "language_title": "Choose the interface language:", "language_saved": "Language changed.",
        "quality": "Default quality", "quality_title": "Choose the default video quality:", "quality_saved": "Default quality: {quality}", "quality_auto": "Auto",
        "back": "Back", "cancel": "Cancel", "retry": "Retry",
        "processing": "⏳ Processing video…", "downloading": "⬇️ Downloading video…", "sending": "📤 Sending video…",
        "accepted_one": "Link accepted.", "accepted_many": "Links accepted: {count}.",
        "batch_invalid": "Invalid links skipped: {count}.",
        "batch_limit": "You can send up to {limit} links in one message.", "queue_full": "Your queue is full. Wait for current downloads to finish.", "queue_partial": "You can add up to {available} more links now.",
        "rate_limit": "Too many requests. Try again in {seconds} seconds.", "maintenance": "The service is temporarily under maintenance. Try again later.", "blocked": "Access to downloads is restricted.", "group_disabled": "Downloads are disabled in this group.",
        "invalid_link": "Send a valid public video link.", "unsafe_link": "This link cannot be processed.", "unsupported": "This link is not supported.", "unavailable": "The video is unavailable or has been removed.", "private_media": "This video requires sign-in or the owner’s permission.", "quality_unavailable": "The selected quality is not available for this video.", "too_large": "The file is too large for the current delivery method.",
        "temporary_error": "A temporary processing error occurred. Try again later.", "delivery_error": "The video could not be delivered. Try again later.", "disk_pressure": "The service is temporarily busy. Try again later.",
        "cancelled": "Download cancelled.", "cancel_requested": "Cancelling download…", "already_finished": "This download has already finished.", "stale_action": "This button is no longer active.", "not_yours": "This button belongs to another user’s request.",
        "video_note": "Make video note", "video_note_processing": "⏳ Creating video note…", "video_note_busy": "The video note is already being created.", "video_note_ready": "Video note is ready.", "video_note_failed": "Could not create a video note from this video.",
        "add_to_group": "Add bot to group", "group_settings_denied": "Only group administrators can change group settings.", "group_settings_title": "Group settings\n\nLanguage: {language}\nQuality: {quality}", "group_language_saved": "Group language changed.", "group_quality_saved": "Group quality: {quality}",
    },
}


def normalize_locale(language_code: str | None) -> str:
    if not language_code:
        return DEFAULT_LOCALE
    code = language_code.strip().lower().replace("_", "-")
    if code.startswith("ru"):
        return "ru"
    if code.startswith("kk"):
        return "kk"
    if code.startswith("uz"):
        return "uz_latn"
    if code.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def t(locale: str | None, key: str, **values: Any) -> str:
    locale = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    template = TRANSLATIONS.get(locale, {}).get(key)
    if template is None:
        template = _RU.get(key)
    if template is None:
        logger.warning("Missing translation key: %s", key)
        return key
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        logger.exception("Invalid translation placeholders for key=%s locale=%s", key, locale)
        # SafeFormatter would hide programmer errors; Russian template with
        # supplied placeholders gives a deterministic non-crashing fallback.
        try:
            return _RU.get(key, template).format(**values)
        except Exception:
            fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
            for field in fields:
                values.setdefault(field, "—")
            return template.format(**values)


def quality_label(locale: str, quality: str | int) -> str:
    return t(locale, "quality_auto") if str(quality) == "auto" else f"{quality}p"
