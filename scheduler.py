import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import database
from content_generator import generate_pin_content
from media_utils import MediaError, transcode_for_pinterest
from pinterest_client import PinterestClient

logger = logging.getLogger(__name__)

POST_START_HOUR = int(os.getenv("POST_START_HOUR", "9"))
POSTS_PER_DAY = int(os.getenv("POSTS_PER_DAY", "5"))
POST_WINDOW_HOURS = int(os.getenv("POST_WINDOW_HOURS", "10"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_MIN = int(os.getenv("RETRY_DELAY_MIN", "60"))
TELEGRAM_LINK = os.getenv("TELEGRAM_CHANNEL_LINK", "https://t.me/yourchannel")


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
    if await database.is_paused():
        logger.info("Постинг на паузе — пропускаю слот.")
        return

    items = await database.get_next_pending(count=1)
    if not items:
        logger.info("Очередь пуста.")
        return

    item = items[0]
    file_id = item["file_id"]
    file_unique_id = item["file_unique_id"]
    queue_id = item["id"]
    retry_count = item.get("retry_count", 0)

    logger.info("Публикую queue_id=%s, попытка #%s", queue_id, retry_count + 1)

    pin_link = f"{TELEGRAM_LINK}?ref=p{queue_id}"

    downloaded_path = None
    pinterest_ready_path = None

    try:
        tg_file = await bot.get_file(file_id)
        suffix = Path(getattr(tg_file, "file_path", "") or "").suffix or ".mp4"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            downloaded_path = tmp.name

        await tg_file.download_to_drive(downloaded_path)
        logger.info("Видео скачано: %s", downloaded_path)

    except Exception as e:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=str(e),
            scheduler=scheduler,
            stage="скачивания",
        )
        return

    try:
        pinterest_ready_path, media_info = await transcode_for_pinterest(downloaded_path)
        logger.info("Видео обработано: %s", media_info)

    except MediaError as e:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=str(e),
            scheduler=scheduler,
            stage="обработки видео",
        )
        _cleanup(downloaded_path, pinterest_ready_path)
        return

    except Exception as e:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=str(e),
            scheduler=scheduler,
            stage="обработки видео",
        )
        _cleanup(downloaded_path, pinterest_ready_path)
        return

    try:
        title, description, hashtags = await generate_pin_content()
        logger.info("Контент готов: %s", title)

    except Exception as e:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=str(e),
            scheduler=scheduler,
            stage="генерации контента",
        )
        _cleanup(downloaded_path, pinterest_ready_path)
        return

    try:
        result = await PinterestClient().create_video_pin(
            video_path=pinterest_ready_path,
            title=title,
            description=f"{description}\n\n{hashtags}",
            link=pin_link,
        )

    except Exception as e:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=str(e),
            scheduler=scheduler,
            stage="публикации",
        )
        _cleanup(downloaded_path, pinterest_ready_path)
        return

    finally:
        _cleanup(downloaded_path, pinterest_ready_path)

    if result.get("success"):
        pin_id = result.get("pin_id", "unknown")
        await database.mark_posted(queue_id, file_unique_id, pin_id, title)

        stats = await database.get_queue_stats()
        pending = stats["pending"]
        days_left = pending // POSTS_PER_DAY + (1 if pending % POSTS_PER_DAY else 0)

        logger.info(
            "Post success -> pin_id=%s pending=%s days_left=%s",
            pin_id,
            pending,
            days_left,
        )

        report = (
            f"✅ Опубликовано\n"
            f"В очереди: {pending}\n"
            f"Хватит примерно на: {days_left} дн."
        )

        await _notify(bot, report)

    else:
        await _handle_error(
            bot=bot,
            queue_id=queue_id,
            file_unique_id=file_unique_id,
            retry_count=retry_count,
            error=result.get("error", "Неизвестная ошибка Pinterest"),
            scheduler=scheduler,
            stage="Pinterest",
        )


async def _handle_error(bot, queue_id, file_unique_id, retry_count, error, scheduler, stage):
    stats = await database.get_queue_stats()
    pending = stats["pending"]
    days_left = pending // POSTS_PER_DAY + (1 if pending % POSTS_PER_DAY else 0)

    logger.error(
        "Ошибка на этапе %s для queue_id=%s: %s",
        stage,
        queue_id,
        error,
    )

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
                replace_existing=True,
            )

        report = (
            f"⚠️ Не опубликовано\n"
            f"В очереди: {pending}\n"
            f"Хватит примерно на: {days_left} дн.\n"
            f"Повтор: {next_try}/{MAX_RETRIES}"
        )

        await _notify(bot, report)

    else:
        await database.mark_failed(queue_id, file_unique_id, error)

        report = (
            f"❌ Не опубликовано\n"
            f"В очереди: {pending}\n"
            f"Хватит примерно на: {days_left} дн."
        )

        await _notify(bot, report)


async def send_weekly_stats(bot):
    stats = await database.get_queue_stats()

    report = (
        f"📊 За 7 дней\n"
        f"Опубликовано: {stats['week_posted']}\n"
        f"Ошибок: {stats['week_errors']}\n"
        f"В очереди: {stats['pending']}"
    )

    await _notify(bot, report)


async def _notify(bot, text: str):
    if not ALLOWED_USER_ID:
        logger.warning("Notify skipped: ALLOWED_USER_ID is empty")
        return

    try:
        logger.info("Notify start -> user=%s text=%s", ALLOWED_USER_ID, text.replace("\n", " | "))
        msg = await bot.send_message(chat_id=ALLOWED_USER_ID, text=text)
        logger.info("Notify success -> message_id=%s", getattr(msg, "message_id", "unknown"))
    except Exception as e:
        logger.exception("Notify failed: %s", e)


def _cleanup(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def setup_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    for h, m in get_post_times():
        scheduler.add_job(
            post_next_from_queue,
            CronTrigger(hour=h, minute=m, timezone=TIMEZONE),
            args=[bot, scheduler],
            id=f"post_{h}_{m}",
            misfire_grace_time=300,
        )
        logger.info("Слот постинга: %02d:%02d %s", h, m, TIMEZONE)

    scheduler.add_job(
        send_weekly_stats,
        CronTrigger(day_of_week="mon", hour=10, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="weekly_stats",
    )

    return scheduler
