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
PINTEREST_USERNAME = os.getenv("PINTEREST_USERNAME", "")
SESSION_FILE       = os.getenv("SESSION_FILE", "/data/pinterest_session.json")

_bot_instance = None
_owner_id = int(os.getenv("ALLOWED_USER_ID", "0"))

def set_bot(bot):
    global _bot_instance
    _bot_instance = bot

async def _send_screenshot(page, label="debug"):
    if not _bot_instance or not _owner_id:
        return
    try:
        path = f"/tmp/pinterest_{label}.png"
        await page.screenshot(path=path, full_page=False)
        with open(path, "rb") as f:
            await _bot_instance.send_photo(chat_id=_owner_id, photo=f,
                caption=f"🔍 {label}\n{page.url}")
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")


class PinterestClient:
    def __init__(self, **kwargs):
        pass

    async def create_video_pin(self, video_path, title, description, link="") -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="ru-RU", timezone_id="Europe/Moscow",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
            )

            if Path(SESSION_FILE).exists():
                try:
                    await context.add_cookies(json.loads(Path(SESSION_FILE).read_text()))
                    logger.info("Сессия загружена")
                except Exception as e:
                    logger.warning(f"Сессия не загружена: {e}")

            page = await context.new_page()

            try:
                # Логин
                await page.goto("https://www.pinterest.com/", wait_until="commit", timeout=30000)
                await asyncio.sleep(3)
                if not await _is_logged_in(page):
                    await _login(page)
                    cookies = await context.cookies()
                    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                    Path(SESSION_FILE).write_text(json.dumps(cookies))
                await _send_screenshot(page, "1_logged_in")

                # Username из env или автодетект
                username = PINTEREST_USERNAME or await _get_username(page)
                logger.info(f"Username: {username}")

                # Открываем страницу доски напрямую
                board_slug = PINTEREST_BOARD.lower().replace(" ", "-").replace("_", "-")
                board_url = f"https://www.pinterest.com/{username}/{board_slug}/"
                logger.info(f"Открываем доску: {board_url}")
                await page.goto(board_url, wait_until="commit", timeout=30000)
                await asyncio.sleep(3)
                await _send_screenshot(page, "2_board_page")

                # Ищем кнопку "+" / "Добавить пин"
                file_input = None
                try:
                    plus_btn = await page.wait_for_selector(
                        '[data-test-id="board-add-pin-button"], '
                        '[aria-label="Добавить пин"], '
                        '[aria-label="Add Pin"], '
                        'button[aria-label="Add pin"]',
                        timeout=8000
                    )
                    await plus_btn.click()
                    await asyncio.sleep(2)
                    await _send_screenshot(page, "3_after_plus")
                    # После клика может появиться меню — выбираем "Создать пин"
                    try:
                        create_pin = await page.wait_for_selector(
                            'a:has-text("Создать пин"), a:has-text("Create Pin"), '
                            '[data-test-id="create-pin-button"]',
                            timeout=4000
                        )
                        await create_pin.click()
                        await asyncio.sleep(3)
                    except PlaywrightTimeout:
                        pass  # Возможно сразу открылась форма
                except PlaywrightTimeout:
                    logger.warning("Кнопка '+' не найдена, пробуем через верхнее меню")
                    # Запасной: меню "Создать" в хедере
                    try:
                        create_menu = await page.wait_for_selector(
                            '[data-test-id="header-create-menu-trigger"], '
                            'button:has-text("Создать"), a:has-text("Создать")',
                            timeout=8000
                        )
                        await create_menu.click()
                        await asyncio.sleep(1)
                        pin_item = await page.wait_for_selector(
                            'a:has-text("Создать пин"), [data-test-id="create-pin"]',
                            timeout=5000
                        )
                        await pin_item.click()
                        await asyncio.sleep(3)
                    except PlaywrightTimeout:
                        pass

                await _send_screenshot(page, "4_create_form")

                # Ищем input[type=file] — делаем его видимым через JS если скрыт
                await page.evaluate("""() => {
                    document.querySelectorAll('input[type="file"]').forEach(el => {
                        el.style.display = 'block';
                        el.style.visibility = 'visible';
                        el.style.opacity = '1';
                        el.style.width = '100px';
                        el.style.height = '100px';
                    });
                }""")
                await asyncio.sleep(0.5)

                try:
                    file_input = await page.wait_for_selector('input[type="file"]', timeout=15000)
                    logger.info("File input найден")
                except PlaywrightTimeout:
                    await _send_screenshot(page, "error_no_file_input")
                    return {"success": False, "error": "Не найден input[type=file]"}

                # Загружаем видео
                await file_input.set_input_files(video_path)
                logger.info("Видео загружено в input")
                await asyncio.sleep(5)
                await _send_screenshot(page, "5_after_upload")

                # Ждём полей формы
                logger.info("Ждём полей формы...")
                title_field = None
                for sel in [
                    '[data-test-id="pin-draft-title"]',
                    'textarea[placeholder*="название"], textarea[placeholder*="Title"]',
                    'textarea[placeholder*="Добавьте название"]',
                    '[aria-label*="название"], [aria-label*="Title"]',
                    'div[contenteditable][data-test-id*="title"]',
                ]:
                    try:
                        await page.wait_for_selector(sel, timeout=60000)
                        title_field = sel
                        logger.info(f"Поле заголовка: {sel}")
                        break
                    except PlaywrightTimeout:
                        continue

                if not title_field:
                    await _send_screenshot(page, "error_no_title")
                    return {"success": False, "error": "Поле заголовка не найдено"}

                await asyncio.sleep(2)
                await _send_screenshot(page, "6_form_ready")

                # Заполняем
                tf = await page.query_selector(title_field)
                if tf:
                    await tf.click(); await asyncio.sleep(0.3)
                    await tf.type(title[:100], delay=50)

                for ds in ['[data-test-id="pin-draft-description"]',
                           'textarea[placeholder*="описание"], textarea[placeholder*="описание пина"]',
                           '[aria-label*="описание"]']:
                    df = await page.query_selector(ds)
                    if df:
                        await df.click(); await asyncio.sleep(0.3)
                        await df.type(description[:500], delay=30)
                        break

                if link:
                    for ls in ['[data-test-id="pin-draft-link"]',
                               'input[placeholder*="ссылк"], input[placeholder*="URL"]']:
                        lf = await page.query_selector(ls)
                        if lf:
                            await lf.click(); await asyncio.sleep(0.3)
                            await lf.type(link, delay=50)
                            break

                await _send_screenshot(page, "7_filled")

                # Публикуем
                for ps in ['[data-test-id="board-dropdown-save-button"]',
                           'button:has-text("Опубликовать")', 'button:has-text("Publish")']:
                    try:
                        btn = await page.wait_for_selector(ps, timeout=5000)
                        await btn.click()
                        logger.info(f"Публикация нажата: {ps}")
                        break
                    except PlaywrightTimeout:
                        continue

                await asyncio.sleep(5)
                await _send_screenshot(page, "8_final")
                pin_id = _extract_pin_id(page.url)
                return {"success": True, "pin_id": pin_id or "unknown"}

            except Exception as e:
                logger.error(f"Ошибка: {e}")
                try: await _send_screenshot(page, "error_final")
                except Exception: pass
                return {"success": False, "error": str(e)}
            finally:
                await browser.close()


async def _get_username(page) -> str:
    """Извлекает username из URL или страницы."""
    try:
        # Из URL business/hub или профиля
        url = page.url
        # Переходим на профиль
        await page.goto("https://www.pinterest.com/me/", wait_until="commit", timeout=15000)
        await asyncio.sleep(2)
        username = page.url.rstrip("/").split("/")[-1]
        if username and username != "me":
            logger.info(f"Username из URL: {username}")
            return username
    except Exception as e:
        logger.warning(f"_get_username error: {e}")
    return "me"


async def _is_logged_in(page) -> bool:
    url = page.url
    if any(x in url for x in ["business/hub", "/me/", "pinterest.com/pin"]):
        return True
    if "login" in url:
        return False
    try:
        await page.wait_for_selector(
            '[data-test-id="header-avatar"],[data-test-id="homefeed-feed"]', timeout=4000
        )
        return True
    except PlaywrightTimeout:
        return False


async def _login(page):
    logger.info("Логинимся...")
    try:
        await page.goto("https://www.pinterest.com/login/", wait_until="commit", timeout=30000)
    except Exception: pass
    await asyncio.sleep(3)
    email_input = await page.wait_for_selector('#email', timeout=15000)
    await email_input.click(); await asyncio.sleep(0.5)
    await email_input.type(PINTEREST_EMAIL, delay=80)
    await asyncio.sleep(0.7)
    pwd = await page.wait_for_selector('#password', timeout=10000)
    await pwd.click(); await asyncio.sleep(0.5)
    await pwd.type(PINTEREST_PASSWORD, delay=80)
    await asyncio.sleep(0.7)
    await page.keyboard.press("Enter")
    try: await page.wait_for_url("**/", timeout=30000)
    except Exception: pass
    await asyncio.sleep(3)
    logger.info(f"Логин: {page.url}")


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
