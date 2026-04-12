import os
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

PINTEREST_EMAIL    = os.getenv("PINTEREST_EMAIL")
PINTEREST_PASSWORD = os.getenv("PINTEREST_PASSWORD")
PINTEREST_BOARD    = os.getenv("PINTEREST_BOARD_NAME")   # точное название доски как на сайте
SESSION_FILE       = os.getenv("SESSION_FILE", "pinterest_session.json")


class PinterestClient:
    def __init__(self, **kwargs):
        # Аргументы оставлены для совместимости с вызовами в scheduler.py,
        # но теперь берём креды из env
        pass

    async def create_video_pin(
        self,
        video_path: str,
        title: str,
        description: str,
        link: str = ""
    ) -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )

            # Загружаем сохранённую сессию если есть
            if Path(SESSION_FILE).exists():
                await context.add_cookies(_load_session())

            page = await context.new_page()

            try:
                # Проверяем авторизацию
                await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded")
                await asyncio.sleep(2)

                if not await _is_logged_in(page):
                    logger.info("Сессия истекла — логинимся заново")
                    await _login(page)
                    _save_session(await context.cookies())

                # Открываем страницу создания пина
                await page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded")
                await asyncio.sleep(3)

                # Загружаем видео
                logger.info("Загружаем видео файл...")
                file_input = await page.wait_for_selector(
                    'input[type="file"]', timeout=15000
                )
                await file_input.set_input_files(video_path)

                # Ждём загрузку и обработку видео Pinterest (прогресс-бар исчезнет)
                logger.info("Ждём обработки видео...")
                await page.wait_for_selector(
                    '[data-test-id="pin-draft-title"]',
                    timeout=120000
                )
                await asyncio.sleep(2)

                # Заголовок
                title_input = await page.wait_for_selector(
                    '[data-test-id="pin-draft-title"]', timeout=10000
                )
                await title_input.click()
                await title_input.fill(title[:100])

                # Описание
                desc_input = await page.query_selector('[data-test-id="pin-draft-description"]')
                if desc_input:
                    await desc_input.click()
                    await desc_input.fill(description[:500])

                # Ссылка
                if link:
                    link_input = await page.query_selector('[data-test-id="pin-draft-link"]')
                    if link_input:
                        await link_input.click()
                        await link_input.fill(link)

                # Выбор доски
                await _select_board(page, PINTEREST_BOARD)

                # Публикуем
                publish_btn = await page.wait_for_selector(
                    '[data-test-id="board-dropdown-save-button"]',
                    timeout=10000
                )
                await publish_btn.click()

                # Ждём подтверждения
                await page.wait_for_selector(
                    '[data-test-id="pin-save-success"], .successMessage, [class*="success"]',
                    timeout=30000
                )
                logger.info("Пин успешно опубликован")

                # Пробуем вытащить ID пина из URL
                await asyncio.sleep(2)
                pin_id = _extract_pin_id(page.url)

                return {"success": True, "pin_id": pin_id or "unknown"}

            except PlaywrightTimeout as e:
                screenshot = f"/tmp/pinterest_error_{int(asyncio.get_event_loop().time())}.png"
                await page.screenshot(path=screenshot)
                logger.error(f"Таймаут: {e}. Скриншот: {screenshot}")
                return {"success": False, "error": f"Таймаут: {e}"}
            except Exception as e:
                logger.error(f"Ошибка Playwright: {e}")
                return {"success": False, "error": str(e)}
            finally:
                await browser.close()


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
    logger.info("Логинимся в Pinterest...")
    await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    email_input = await page.wait_for_selector('#email', timeout=15000)
    await email_input.fill(PINTEREST_EMAIL)
    await asyncio.sleep(0.5)

    password_input = await page.wait_for_selector('#password', timeout=10000)
    await password_input.fill(PINTEREST_PASSWORD)
    await asyncio.sleep(0.5)

    await page.keyboard.press("Enter")

    # Ждём редиректа на главную
    await page.wait_for_url("**/", timeout=30000)
    await asyncio.sleep(2)
    logger.info("Логин успешен")


async def _select_board(page, board_name: str):
    """Выбирает доску в дропдауне."""
    board_btn = await page.wait_for_selector(
        '[data-test-id="board-dropdown-select-button"]',
        timeout=10000
    )
    await board_btn.click()
    await asyncio.sleep(1)

    # Ищем нужную доску по названию
    search_input = await page.query_selector('[data-test-id="board-search-input"]')
    if search_input:
        await search_input.fill(board_name)
        await asyncio.sleep(1)

    board_option = await page.wait_for_selector(
        f'[data-test-id="board-row"]:has-text("{board_name}")',
        timeout=10000
    )
    await board_option.click()
    await asyncio.sleep(0.5)


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""


def _load_session() -> list:
    import json
    with open(SESSION_FILE, "r") as f:
        return json.load(f)


def _save_session(cookies: list):
    import json
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f)
