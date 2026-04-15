import asyncio
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import database
import pinterest_client
from scheduler import POSTS_PER_DAY, TIMEZONE, get_post_times, setup_scheduler

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(exist_ok=True)


def _make_log_filename():
    return str(LOG_DIR / (datetime.now().strftime("%Y-%m") + ".log"))


class MonthlyFileHandler(logging.handlers.BaseRotatingHandler):
    def __init__(self):
        super().__init__(_make_log_filename(), "a", encoding="utf-8")

    def shouldRollover(self, record):
        return self.baseFilename != _make_log_filename()

    def doRollover(self):
        self.stream.close()
        self.baseFilename = _make_log_filename()
        self.stream = self._open()


fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s")
file_handler = MonthlyFileHandler()
file_handler.setFormatter(fmt)
console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)
logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
BATCH_TIMEOUT_SEC = int(os.getenv("BATCH_TIMEOUT_SEC", "300"))
MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "200"))

_batch: dict[int, dict] = {}

VIDEO_DOC_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpeg", ".mpg", ".wmv", ".3gp"
}


def is_allowed(uid: int) -> bool:
    return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


def _looks_like_video_document(document) -> bool:
    mime_type = getattr(document, "mime_type", "") or ""
    file_name = (getattr(document, "file_name", "") or "").lower()
    return mime_type.startswith("video/") or any(file_name.endswith(ext) for ext in VIDEO_DOC_EXTENSIONS)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    times = get_post_times()
    tstr = " / ".join(f"{h:02d}:{m:02d}" for h, m in times)
    stats = await database.get_queue_stats()
    paused = await database.is_paused()
    status = "⏸ <b>ПАУЗА</b>" if paused else "▶️ Работает"
    await update.message.reply_text(
        f"🤖 <b>Pinterest Auto-Poster</b>  {status}\n\n"
        f"Скидывай любое обычное видео — бот сам приведёт его к формату Pinterest, добавит описание и запостит.\n\n"
        f"🕐 {POSTS_PER_DAY} видео/день: {tstr} ({TIMEZONE})\n"
        f"📋 В очереди: {stats['pending']} / макс. {stats['queue_max']}\n"
        f"📦 Размер файла: до {MAX_VIDEO_MB} MB\n\n"
        f"/queue — статус очереди\n"
        f"/stats — статистика\n"
        f"/retry — повторить упавшие\n"
        f"/pause | /resume — пауза / возобновить\n"
        f"/log — последние логи",
        parse_mode="HTML",
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    stats = await database.get_queue_stats()
    pending = stats["pending"]
    days = (pending // POSTS_PER_DAY) + (1 if pending % POSTS_PER_DAY else 0)
    tstr = " / ".join(f"{h:02d}:{m:02d}" for h, m in get_post_times())
    paused = await database.is_paused()
    pause_tag = "  ⏸ ПАУЗА" if paused else ""
    await update.message.reply_text(
        f"📋 <b>Очередь{pause_tag}</b>\n\n"
        f"⏳ Ожидает: <b>{pending}</b> (~{days} дн.) / макс. {stats['queue_max']}\n"
        f"✅ Опубликовано: <b>{stats['posted_total_queue']}</b>\n"
        f"❌ Упало: <b>{stats['failed']}</b>\n\n"
        f"🕐 {tstr}",
        parse_mode="HTML",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    from datetime import timedelta

    stats = await database.get_queue_stats()
    now = datetime.now()
    w0 = (now - timedelta(days=7)).strftime("%d.%m")
    w1 = now.strftime("%d.%m")
    await update.message.reply_text(
        f"📊 <b>Статистика Pinterest</b>\n\n"
        f"За 7 дней ({w0}–{w1}):\n"
        f"✅ Опубликовано: <b>{stats['week_posted']}</b>\n"
        f"❌ Ошибок: <b>{stats['week_errors']}</b>\n\n"
        f"📋 В очереди: <b>{stats['pending']}</b>\n"
        f"📌 Всего опубликовано: <b>{stats['total_posted']}</b>",
        parse_mode="HTML",
    )


async def cmd_testpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    stats = await database.get_queue_stats()
    if stats["pending"] == 0:
        await update.message.reply_text("📭 Очередь пуста — добавь видео.")
        return
    await update.message.reply_text("🚀 Запускаю публикацию прямо сейчас...")
    from scheduler import post_next_from_queue

    await post_next_from_queue(context.bot)


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    count = await database.reset_failed_to_pending()
    if count == 0:
        await update.message.reply_text("✅ Упавших постов нет — всё чисто.")
    else:
        await update.message.reply_text(
            f"🔄 <b>{count} видео</b> возвращено в очередь.\nВыйдут по расписанию.",
            parse_mode="HTML",
        )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await database.set_state("paused", "1")
    await update.message.reply_text(
        "⏸ <b>Постинг приостановлен.</b>\n\nВидео копятся в очереди, не публикуются.\n/resume — возобновить.",
        parse_mode="HTML",
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await database.set_state("paused", "0")
    stats = await database.get_queue_stats()
    await update.message.reply_text(
        f"▶️ <b>Постинг возобновлён.</b>\n\nВ очереди: {stats['pending']} видео.",
        parse_mode="HTML",
    )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    log_file = Path(_make_log_filename())
    if not log_file.exists():
        await update.message.reply_text("📂 Лог-файл за этот месяц ещё пуст.")
        return
    lines = log_file.read_text(encoding="utf-8").splitlines()[-60:]
    text = "\n".join(lines) or "Лог пуст."
    if len(text) > 3900:
        text = "…(обрезано)\n" + text[-3900:]
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")


async def _flush_batch(user_id: int, bot):
    await asyncio.sleep(BATCH_TIMEOUT_SEC)
    data = _batch.pop(user_id, {})
    if not data:
        return

    stats = await database.get_queue_stats()
    pending = stats["pending"]
    days_left = (pending // POSTS_PER_DAY) + (1 if pending % POSTS_PER_DAY else 0)
    added = data["added"]
    skipped = data["skipped"]

    msg = f"📦 <b>Загрузка завершена</b>\n\n✅ Добавлено: <b>{added}</b> видео\n"
    if skipped:
        msg += f"⚠️ Пропущено (не видео / дубли / лимит): <b>{skipped}</b>\n"
    msg += f"\n📋 В очереди: <b>{pending}</b>  (~{days_left} дн.)"

    week_threshold = POSTS_PER_DAY * 7
    if pending <= week_threshold:
        msg += "\n\n⚠️ Запас менее недели — добавь ещё видео."

    await bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    media = update.message.video
    if media is None and update.message.document and _looks_like_video_document(update.message.document):
        media = update.message.document

    if media is None:
        await update.message.reply_text("❌ Отправь видео как видео или документом.")
        return

    file_size_mb = getattr(media, "file_size", 0) / 1024 / 1024
    if file_size_mb > MAX_VIDEO_MB:
        await update.message.reply_text(
            f"⚠️ Файл {file_size_mb:.1f} MB — лимит {MAX_VIDEO_MB} MB."
        )
        return

    result = await database.add_to_queue(media.file_id, media.file_unique_id)

    if uid not in _batch:
        _batch[uid] = {"added": 0, "skipped": 0, "task": None}

    buf = _batch[uid]

    if result["added"]:
        buf["added"] += 1
    else:
        buf["skipped"] += 1
        if result["reason"] == "limit":
            stats = await database.get_queue_stats()
            if buf["task"]:
                buf["task"].cancel()
            _batch.pop(uid, None)
            await update.message.reply_text(
                f"🚫 <b>Очередь заполнена</b> ({stats['queue_max']} видео)\n\n"
                f"Дождись публикации части и загрузи снова.",
                parse_mode="HTML",
            )
            return

    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(_flush_batch(uid, context.bot))


async def post_init(app):
    await database.init_db()
    pinterest_client.set_bot(app.bot)
    scheduler = setup_scheduler(app.bot)
    scheduler.start()
    logger.info("БД готова, планировщик запущен.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("testpost", cmd_testpost))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
