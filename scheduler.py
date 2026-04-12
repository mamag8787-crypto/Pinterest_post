import os
import logging
import tempfile
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import database
from pinterest_client import PinterestClient
from content_generator import generate_pin_content

logger = logging.getLogger(__name__)

POST_START_HOUR   = int(os.getenv("POST_START_HOUR", "9"))
POSTS_PER_DAY     = int(os.getenv("POSTS_PER_DAY", "5"))
POST_WINDOW_HOURS = int(os.getenv("POST_WINDOW_HOURS", "10"))
TIMEZONE          = os.getenv("TIMEZONE", "Europe/Moscow")
ALLOWED_USER_ID   = int(os.getenv("ALLOWED_USER_ID", "0"))
MAX_RETRIES       = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_MIN   = int(os.getenv("RETRY_DELAY_MIN", "60"))
TELEGRAM_LINK     = os.getenv("TELEGRAM_CHANNEL_LINK", "https://t.me/yourchannel")


def get_post_times() -> list[tuple[int, int]]:
    if POSTS_PER_DAY == 1:
        return [(POST_START_HOUR, 0)]
    interval_min = (POST_WINDOW_HOURS * 60) / (POSTS_PER_DAY - 1)
    times = []
    for i in range(POSTS_PER_DAY):
        total = POST_START_HOUR * 60 + round(i * interval_min)
        times.append((total // 60, total % 60))
    return times


async def post_next_from_queue(bot, scheduler=None):
    # Пауза
    if await database.is_paused():
        logger.info("Постинг на паузе — пропускаю слот.")
        return

    items = await database.get_next_pending(count=1)
    if not items:
        logger.info("Очередь пуста.")
        await _notify(bot,
            "📭 <b>Очередь пуста!</b>\n\n"
            f"Загрузи видео на неделю вперёд — нужно минимум "
            f"<b>{POSTS_PER_DAY * 7} видео</b> ({POSTS_PER_DAY}/день × 7 дней)."
        )
        return

    item = items[0]
    file_id        = item["file_id"]
    file_unique_id = item["file_unique_id"]
    queue_id       = item["id"]
    retry_count    = item.get("retry_count", 0)

    logger.info(f"Публикую queue_id={queue_id}, попытка #{retry_count+1}")

    # Уникальная ссылка на TG для этого пина (без UTM, простой ref)
    pin_link = f"{TELEGRAM_LINK}?ref=p{queue_id}"

    # Скачиваем
    try:
        tg_file = await bot.get_file(file_id)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await _handle_error(bot, queue_id, file_unique_id, retry_count, str(e), scheduler, "скачивания")
        return

    # Генерируем контент
    try:
        title, description, hashtags = await generate_pin_content()
    except Exception as e:
        await _handle_error(bot, queue_id, file_unique_id, retry_count, str(e), scheduler, "генерации AI")
        import os as _os; _os.unlink(tmp_path)
        return

    # Публикуем в Pinterest
    try:
        result = await PinterestClient().create_video_pin(
            video_path=tmp_path,
            title=title,
            description=f"{description}\n\n{hashtags}",
            link=pin_link
        )
    except Exception as e:
        await _handle_error(bot, queue_id, file_unique_id, retry_count, str(e), scheduler, "публикации")
        import os as _os; _os.unlink(tmp_path)
        return
    finally:
        import os as _os
        if _os.path.exists(tmp_path):
            _os.unlink(tmp_path)

    if result["success"]:
        pin_id = result["pin_id"]
        await database.mark_posted(queue_id, file_unique_id, pin_id, title)

        stats  = await database.get_queue_stats()
        pending = stats["pending"]
        report = (
            f"✅ <b>Опубликовано!</b>\n\n"
            f"📌 <b>{title}</b>\n"
            f"🔗 https://www.pinterest.com/pin/{pin_id}/\n"
            f"🔁 Ссылка в пине: <code>{pin_link}</code>\n\n"
            f"📋 В очереди: <b>{pending}</b> видео"
        )
        week_threshold = POSTS_PER_DAY * 7
        if 0 < pending <= week_threshold:
            days_left = pending // POSTS_PER_DAY + (1 if pending % POSTS_PER_DAY else 0)
            report += (
                f"\n\n⚠️ <b>Запас на {days_left} дн.</b>\n"
                f"Нужно минимум {week_threshold} видео для недельного буфера."
            )
        await _notify(bot, report)
    else:
        await _handle_error(bot, queue_id, file_unique_id, retry_count,
                            result["error"], scheduler, "Pinterest API")


async def _handle_error(bot, queue_id, file_unique_id, retry_count, error, scheduler, stage):
    if retry_count < MAX_RETRIES:
        next_try = retry_count + 1
        await database.mark_retry(queue_id, error)
        if scheduler:
            run_at = datetime.now() + timedelta(minutes=RETRY_DELAY_MIN)
            scheduler.add_job(
                post_next_from_queue,
                trigger=DateTrigger(run_date=run_at),
                args=[bot, scheduler],
                id=f"retry_{queue_id}_{next_try}",
                replace_existing=True
            )
        await _notify(bot,
            f"⚠️ <b>Ошибка на этапе {stage}</b>\n\n{error}\n\n"
            f"🔄 Попытка {next_try}/{MAX_RETRIES} через {RETRY_DELAY_MIN} мин."
        )
    else:
        await database.mark_failed(queue_id, file_unique_id, error)
        await _notify(bot,
            f"❌ <b>Видео #{queue_id} провалилось</b>\n\n{error}\n\n"
            f"Исчерпаны все {MAX_RETRIES} попытки. /retry — попробовать снова."
        )


async def send_weekly_stats(bot):
    stats = await database.get_queue_stats()
    now   = datetime.now()
    w0    = (now - timedelta(days=7)).strftime("%d.%m")
    w1    = now.strftime("%d.%m")
    await _notify(bot,
        f"📊 <b>Недельный отчёт Pinterest</b>\n<i>{w0} — {w1}</i>\n\n"
        f"✅ Опубликовано: <b>{stats['week_posted']}</b>\n"
        f"❌ Ошибок: <b>{stats['week_errors']}</b>\n"
        f"📋 В очереди: <b>{stats['pending']}</b>\n"
        f"📌 Всего: <b>{stats['total_posted']}</b>"
    )


async def _notify(bot, text: str):
    if ALLOWED_USER_ID:
        try:
            await bot.send_message(chat_id=ALLOWED_USER_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление: {e}")


def setup_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    for h, m in get_post_times():
        scheduler.add_job(
            post_next_from_queue,
            CronTrigger(hour=h, minute=m, timezone=TIMEZONE),
            args=[bot, scheduler],
            id=f"post_{h}_{m}",
            misfire_grace_time=300
        )
        logger.info(f"Слот постинга: {h:02d}:{m:02d} {TIMEZONE}")

    scheduler.add_job(
        send_weekly_stats,
        CronTrigger(day_of_week="mon", hour=10, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="weekly_stats"
    )
    return scheduler
