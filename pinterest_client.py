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
                    "--disable-infobars",
                    "--window-size=1280,900",
                    "--start-maximized",
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
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                }
            )

            # Скрываем признаки автоматизации
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US'] });
                window.chrome = { runtime: {} };
            """)

            # Загружаем сохранённую сессию
            session_loaded = False
            if Path(SESSION_FILE).exists():
                try:
                    cookies = json.loads(Path(SESSION_FILE).read_text())
                    await context.add_cookies(cookies)
                    session_loaded = True
                    logger.info("Сессия загружена из файла")
                except Exception as e:
                    logger.warning(f"Не удалось загрузить сессию: {e}")

            page = await context.new_page()

            try:
                # Всегда идём через логин-проверку
                logger.info("Открываем Pinterest...")
                await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=30000)
                await _human_delay(2, 3)

                if not await _is_logged_in(page):
                    logger.info("Не залогинен — выполняем логин")
                    await _login(page)
                    # Сохраняем сессию
                    cookies = await context.cookies()
                    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                    Path(SESSION_FILE).write_text(json.dumps(cookies))
                    logger.info("Сессия сохранена")
                else:
                    logger.info("Уже залогинен")

                # Открываем создание пина
                logger.info("Переходим к созданию пина...")
                await page.goto("https://www.pinterest.com/pin-builder/", wait_until="domcontentloaded", timeout=30000)
                await _human_delay(3, 4)

                # Если редиректнуло — пробуем альтернативный URL
                if "pin-builder" not in page.url and "pin-creation" not in page.url:
                    logger.info(f"Редирект на {page.url}, пробуем прямой URL")
                    await page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded", timeout=30000)
                    await _human_delay(3, 4)

                # Загружаем видео
                logger.info("Ищем поле загрузки файла...")
                file_input = await page.wait_for_selector('input[type="file"]', timeout=20000)
                await file_input.set_input_files(video_path)
                logger.info("Видео загружено, ждём обработки...")

                # Ждём появления полей редактирования (видео обработано)
                await page.wait_for_selector(
                    '[data-test-id="pin-draft-title"]',
                    timeout=180000  # 3 минуты — видео может долго обрабатываться
                )
                await _human_delay(2, 3)

                # Заголовок
                title_field = await page.wait_for_selector('[data-test-id="pin-draft-title"]')
                await title_field.click()
                await _human_delay(0.3, 0.6)
                await title_field.fill(title[:100])

                # Описание
                desc_field = await page.query_selector('[data-test-id="pin-draft-description"]')
                if desc_field:
                    await desc_field.click()
                    await _human_delay(0.3, 0.6)
                    await desc_field.fill(description[:500])

                # Ссылка
                if link:
                    link_field = await page.query_selector('[data-test-id="pin-draft-link"]')
                    if link_field:
                        await link_field.click()
                        await _human_delay(0.3, 0.6)
                        await link_field.fill(link)

                # Выбор доски
                await _select_board(page, PINTEREST_BOARD)
                await _human_delay(1, 2)

                # Публикуем
                publish_btn = await page.wait_for_selector(
                    '[data-test-id="board-dropdown-save-button"]',
                    timeout=10000
                )
                await publish_btn.click()
                logger.info("Нажали публикацию, ждём подтверждения...")

                # Ждём успеха
                await page.wait_for_selector(
                    '[data-test-id="pin-save-success"], [class*="successToast"], [class*="success"]',
                    timeout=30000
                )
                await _human_delay(2, 3)

                pin_id = _extract_pin_id(page.url)
                logger.info(f"Пин опубликован: {pin_id}")
                return {"success": True, "pin_id": pin_id or "unknown"}

            except PlaywrightTimeout as e:
                try:
                    screenshot = f"/tmp/pinterest_error_{int(asyncio.get_event_loop().time())}.png"
                    await page.screenshot(path=screenshot, full_page=True)
                    logger.error(f"Таймаут. URL: {page.url}. Скриншот: {screenshot}")
                except Exception:
                    pass
                return {"success": False, "error": f"Таймаут: {e}"}
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                return {"success": False, "error": str(e)}
            finally:
                await browser.close()


async def _is_logged_in(page) -> bool:
    try:
        await page.wait_for_selector(
            '[data-test-id="header-avatar"], [data-test-id="homefeed-feed"], [aria-label="Ваш аккаунт"]',
            timeout=5000
        )
        return True
    except PlaywrightTimeout:
        return False


async def _login(page):
    logger.info("Логин в Pinterest...")
    await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=30000)
    await _human_delay(2, 3)

    email_input = await page.wait_for_selector('#email', timeout=15000)
    await email_input.click()
    await _human_delay(0.5, 1)
    await email_input.type(PINTEREST_EMAIL, delay=80)
    await _human_delay(0.5, 1)

    password_input = await page.wait_for_selector('#password', timeout=10000)
    await password_input.click()
    await _human_delay(0.5, 1)
    await password_input.type(PINTEREST_PASSWORD, delay=80)
    await _human_delay(0.5, 1)

    await page.keyboard.press("Enter")
    await page.wait_for_url("**/", timeout=30000)
    await _human_delay(2, 3)
    logger.info("Логин успешен")


async def _select_board(page, board_name: str):
    board_btn = await page.wait_for_selector(
        '[data-test-id="board-dropdown-select-button"]', timeout=10000
    )
    await board_btn.click()
    await _human_delay(1, 2)

    search = await page.query_selector('[data-test-id="board-search-input"]')
    if search:
        await search.type(board_name, delay=60)
        await _human_delay(1, 2)

    board_option = await page.wait_for_selector(
        f'[data-test-id="board-row"]:has-text("{board_name}")', timeout=10000
    )
    await board_option.click()


async def _human_delay(min_sec: float, max_sec: float = None):
    import random
    if max_sec is None:
        max_sec = min_sec
    await asyncio.sleep(random.uniform(min_sec, max_sec))


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
