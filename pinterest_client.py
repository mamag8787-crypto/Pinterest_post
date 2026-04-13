import os
import asyncio
import logging
import json
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

PINTEREST_EMAIL    = os.getenv("PINTEREST_EMAIL")
PINTEREST_PASSWORD = os.getenv("PINTEREST_PASSWORD")
PINTEREST_BOARD    = os.getenv("PINTEREST_BOARD_NAME")
SESSION_FILE       = os.getenv("SESSION_FILE", "/data/pinterest_session.json")

# Будет установлен из бота для отправки скриншотов
_bot_instance = None
_owner_id = int(os.getenv("ALLOWED_USER_ID", "0"))

def set_bot(bot):
    global _bot_instance
    _bot_instance = bot

async def _send_screenshot(page, label="debug"):
    """Шлёт скриншот владельцу в TG для отладки."""
    if not _bot_instance or not _owner_id:
        return
    try:
        path = f"/tmp/pinterest_{label}.png"
        await page.screenshot(path=path, full_page=False)
        with open(path, "rb") as f:
            await _bot_instance.send_photo(
                chat_id=_owner_id,
                photo=f,
                caption=f"🔍 Pinterest debug: {label}\nURL: {page.url}"
            )
    except Exception as e:
        logger.error(f"Не удалось отправить скриншот: {e}")


class PinterestClient:
    def __init__(self, **kwargs):
        pass

    async def create_video_pin(self, video_path, title, description, link="") -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900",
                ]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)

            # Загружаем сессию
            if Path(SESSION_FILE).exists():
                try:
                    cookies = json.loads(Path(SESSION_FILE).read_text())
                    await context.add_cookies(cookies)
                    logger.info("Сессия загружена")
                except Exception as e:
                    logger.warning(f"Сессия не загружена: {e}")

            page = await context.new_page()

            try:
                # Шаг 1: главная
                logger.info("Открываем главную Pinterest...")
                try:
                    await page.goto("https://www.pinterest.com/", wait_until="commit", timeout=30000)
                except Exception as e:
                    logger.warning(f"goto main: {e}")
                await asyncio.sleep(3)
                await _send_screenshot(page, "1_main")

                # Шаг 2: проверяем логин
                if not await _is_logged_in(page):
                    logger.info("Нужен логин...")
                    await _login(page)
                    await _send_screenshot(page, "2_after_login")
                    cookies = await context.cookies()
                    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                    Path(SESSION_FILE).write_text(json.dumps(cookies))
                    logger.info("Сессия сохранена")
                else:
                    logger.info("Уже залогинен")
                    await _send_screenshot(page, "2_logged_in")

                # Шаг 3: создание пина
                logger.info("Переходим к созданию пина...")
                try:
                    await page.goto("https://www.pinterest.com/pin-builder/", wait_until="commit", timeout=30000)
                except Exception as e:
                    logger.warning(f"goto pin-builder: {e}")
                await asyncio.sleep(4)
                await _send_screenshot(page, "3_pin_builder")

                # Шаг 4: загружаем файл
                logger.info("Ищем input[type=file]...")
                try:
                    file_input = await page.wait_for_selector('input[type="file"]', timeout=20000)
                    await file_input.set_input_files(video_path)
                    logger.info("Файл загружен")
                except PlaywrightTimeout:
                    await _send_screenshot(page, "4_no_file_input")
                    return {"success": False, "error": "Не найдено поле загрузки файла. Проверь скриншот в TG."}

                await asyncio.sleep(3)
                await _send_screenshot(page, "4_file_uploaded")

                # Шаг 5: ждём полей
                logger.info("Ждём обработки видео...")
                try:
                    await page.wait_for_selector('[data-test-id="pin-draft-title"]', timeout=180000)
                except PlaywrightTimeout:
                    await _send_screenshot(page, "5_no_title_field")
                    return {"success": False, "error": "Поле заголовка не появилось. Проверь скриншот в TG."}

                await asyncio.sleep(2)

                # Заголовок
                title_field = await page.query_selector('[data-test-id="pin-draft-title"]')
                if title_field:
                    await title_field.click()
                    await asyncio.sleep(0.4)
                    await title_field.type(title[:100], delay=60)

                # Описание
                desc_field = await page.query_selector('[data-test-id="pin-draft-description"]')
                if desc_field:
                    await desc_field.click()
                    await asyncio.sleep(0.4)
                    await desc_field.type(description[:500], delay=40)

                # Ссылка
                if link:
                    link_field = await page.query_selector('[data-test-id="pin-draft-link"]')
                    if link_field:
                        await link_field.click()
                        await asyncio.sleep(0.4)
                        await link_field.type(link, delay=60)

                await _send_screenshot(page, "5_fields_filled")

                # Доска
                await _select_board(page, PINTEREST_BOARD)
                await asyncio.sleep(1)

                # Публикация
                publish_btn = await page.wait_for_selector(
                    '[data-test-id="board-dropdown-save-button"]', timeout=10000
                )
                await publish_btn.click()
                logger.info("Нажали публикацию...")

                await page.wait_for_selector(
                    '[data-test-id="pin-save-success"], [class*="successToast"], [class*="success"]',
                    timeout=30000
                )
                await asyncio.sleep(2)
                await _send_screenshot(page, "6_success")

                pin_id = _extract_pin_id(page.url)
                return {"success": True, "pin_id": pin_id or "unknown"}

            except Exception as e:
                logger.error(f"Ошибка: {e}")
                try:
                    await _send_screenshot(page, "error_final")
                except Exception:
                    pass
                return {"success": False, "error": str(e)}
            finally:
                await browser.close()


async def _dismiss_onboarding(page):
    """Закрывает онбординговый туториал Pinterest (1 из 4 шагов)."""
    try:
        # Пробуем кликнуть X (закрыть)
        close_btn = await page.query_selector('[data-test-id="modal-close"], [aria-label="Закрыть"], button[aria-label="Close"]')
        if close_btn:
            await close_btn.click()
            await asyncio.sleep(1)
            logger.info("Онбординг закрыт через X")
            return
        # Пробуем пройти все 4 шага кликая "Далее"
        for step in range(4):
            next_btn = await page.query_selector('button:has-text("Далее"), button:has-text("Next")')
            if not next_btn:
                break
            await next_btn.click()
            await asyncio.sleep(0.8)
            logger.info(f"Онбординг шаг {step+1} пройден")
        # После последнего шага может быть кнопка "Начать" или "Done"
        done_btn = await page.query_selector('button:has-text("Начать"), button:has-text("Done"), button:has-text("Got it")')
        if done_btn:
            await done_btn.click()
            await asyncio.sleep(1)
    except Exception as e:
        logger.info(f"Онбординг не найден или уже закрыт: {e}")


async def _is_logged_in(page) -> bool:
    try:
        await page.wait_for_selector(
            '[data-test-id="header-avatar"], [data-test-id="homefeed-feed"]',
            timeout=5000
        )
        return True
    except PlaywrightTimeout:
        return False


async def _login(page):
    logger.info("Выполняем логин...")
    try:
        await page.goto("https://www.pinterest.com/login/", wait_until="commit", timeout=30000)
    except Exception as e:
        logger.warning(f"goto login: {e}")
    await asyncio.sleep(3)

    email_input = await page.wait_for_selector('#email', timeout=15000)
    await email_input.click()
    await asyncio.sleep(0.5)
    await email_input.type(PINTEREST_EMAIL, delay=80)
    await asyncio.sleep(0.7)

    password_input = await page.wait_for_selector('#password', timeout=10000)
    await password_input.click()
    await asyncio.sleep(0.5)
    await password_input.type(PINTEREST_PASSWORD, delay=80)
    await asyncio.sleep(0.7)

    await page.keyboard.press("Enter")

    try:
        await page.wait_for_url("**/", timeout=30000)
    except Exception as e:
        logger.warning(f"wait_for_url after login: {e}")
    await asyncio.sleep(3)
    logger.info(f"После логина URL: {page.url}")


async def _select_board(page, board_name: str):
    board_btn = await page.wait_for_selector(
        '[data-test-id="board-dropdown-select-button"]', timeout=10000
    )
    await board_btn.click()
    await asyncio.sleep(1)
    search = await page.query_selector('[data-test-id="board-search-input"]')
    if search:
        await search.type(board_name, delay=60)
        await asyncio.sleep(1)
    board_option = await page.wait_for_selector(
        f'[data-test-id="board-row"]:has-text("{board_name}")', timeout=10000
    )
    await board_option.click()


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
