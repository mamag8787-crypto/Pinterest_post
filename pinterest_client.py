import asyncio
import logging
import os
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "0") == "1"
logger = logging.getLogger(__name__)

PINTEREST_EMAIL = os.getenv("PINTEREST_EMAIL")
PINTEREST_PASSWORD = os.getenv("PINTEREST_PASSWORD")
PINTEREST_BOARD = os.getenv("PINTEREST_BOARD_NAME", "").strip()
SESSION_FILE = Path(os.getenv("SESSION_FILE", "/data/pinterest_state.json"))
LEGACY_SESSION_FILE = Path("/data/pinterest_session.json")
PINTEREST_CREATE_URL = os.getenv("PINTEREST_CREATE_URL", "https://www.pinterest.com/pin-creation-tool/")
PINTEREST_FALLBACK_CREATE_URL = os.getenv("PINTEREST_FALLBACK_CREATE_URL", "https://www.pinterest.com/pin-builder/")
BOOTSTRAP_LOGIN = os.getenv("PINTEREST_BOOTSTRAP_LOGIN", "0") == "1"
BROWSER_CHANNEL = os.getenv("PINTEREST_BROWSER_CHANNEL", "chrome").strip() or "chrome"
BROWSER_EXECUTABLE_PATH = os.getenv("PINTEREST_EXECUTABLE_PATH", "").strip() or None

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
            await _bot_instance.send_photo(chat_id=_owner_id, photo=f, caption=f"🔍 {label}\n{page.url}")
    except Exception as e:
        logger.error("Screenshot failed: %s", e)


def _existing_session_file() -> Path | None:
    for candidate in (SESSION_FILE, LEGACY_SESSION_FILE):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


class PinterestClient:
    async def create_video_pin(self, video_path, title, description, link="") -> dict:
        async with async_playwright() as p:
            launch_kwargs = {
                "headless": not BOOTSTRAP_LOGIN,
                "channel": BROWSER_CHANNEL,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if BROWSER_EXECUTABLE_PATH:
                launch_kwargs["executable_path"] = BROWSER_EXECUTABLE_PATH

            logger.info("Запускаю браузер Playwright через channel=%s", BROWSER_CHANNEL)
            browser = await p.chromium.launch(**launch_kwargs)

            context_kwargs = dict(
                viewport={"width": 1440, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )

            session_path = _existing_session_file()
            if session_path:
                context_kwargs["storage_state"] = str(session_path)
                logger.info("Загружаю storage_state из %s", session_path)

            context = await browser.new_context(**context_kwargs)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
            )
            page = await context.new_page()

            try:
                await self._ensure_logged_in(page, context)
                await _send_screenshot(page, "1_logged_in")

                await self._open_pin_builder(page)
                await _send_screenshot(page, "2_builder_opened")

                file_input = await self._find_file_input(page)
                if not file_input:
                    await _send_screenshot(page, "error_no_file_input")
                    return {"success": False, "error": "Не найден input[type=file] в pin builder"}

                await file_input.set_input_files(video_path)
                logger.info("Видео загружено в form input: %s", video_path)
                await asyncio.sleep(10)
                await self._raise_if_upload_error(page)
                await _send_screenshot(page, "3_after_upload")

                await self._fill_text_fields(page, title, description, link)
                await self._select_board(page, PINTEREST_BOARD)
                await _send_screenshot(page, "4_filled")

                await self._publish(page)
                await asyncio.sleep(8)
                await _send_screenshot(page, "5_published")

                pin_id = _extract_pin_id(page.url)
                return {"success": True, "pin_id": pin_id or "unknown"}
            except Exception as e:
                logger.exception("Pinterest browser publish failed")
                try:
                    await _send_screenshot(page, "error_final")
                except Exception:
                    pass
                return {"success": False, "error": str(e)}
            finally:
                try:
                    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(SESSION_FILE))
                    logger.info("Сохранил storage_state в %s", SESSION_FILE)
                except Exception as e:
                    logger.warning("Не удалось сохранить storage state: %s", e)
                await browser.close()

    async def _ensure_logged_in(self, page, context):
        await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)
        if await _is_logged_in(page):
            return

        if BOOTSTRAP_LOGIN:
            logger.info("Режим bootstrap-login включён. Жду ручной вход.")
            await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(60000)
            if await _is_logged_in(page):
                await context.storage_state(path=str(SESSION_FILE))
                return
            raise RuntimeError("Ручной вход не завершён. Сохрани сессию локально и загрузи файл состояния.")

        if not PINTEREST_EMAIL or not PINTEREST_PASSWORD:
            raise RuntimeError("Нет PINTEREST_EMAIL или PINTEREST_PASSWORD")

        await _login(page)
        if not await _is_logged_in(page):
            raise RuntimeError(
                "Pinterest login не прошёл. Pinterest режет headless-логин. Нужна сохранённая сессия SESSION_FILE."
            )

    async def _open_pin_builder(self, page):
        for url in [PINTEREST_CREATE_URL, PINTEREST_FALLBACK_CREATE_URL]:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(4)
                if await self._page_has_builder(page):
                    return
            except Exception:
                pass
        raise RuntimeError("Не удалось открыть pin builder")

    async def _page_has_builder(self, page) -> bool:
        for sel in [
            'input[type="file"]',
            '[data-test-id="pin-draft-title"]',
            'button:has-text("Publish")',
            'button:has-text("Опубликовать")',
        ]:
            try:
                await page.locator(sel).first.wait_for(timeout=3000)
                return True
            except Exception:
                pass
        return "pin-builder" in page.url or "pin-creation-tool" in page.url

    async def _find_file_input(self, page):
        await page.evaluate(
            """() => {
                document.querySelectorAll('input[type="file"]').forEach(el => {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                });
            }"""
        )
        for sel in ['input[type="file"]', 'input[accept*="video"]', 'input[accept*="image"]']:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(timeout=12000)
                return loc
            except Exception:
                pass
        return None

    async def _raise_if_upload_error(self, page):
        error_selectors = [
            'text="В этом видео не используется кодировка H.264 или H.265"',
            'text="H.264 or H.265"',
            'text="используйте браузер Safari"',
            'text="use browser Safari"',
            'text="Something went wrong"',
            'text="Что-то пошло не так"',
        ]
        for _ in range(8):
            for sel in error_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1200):
                        text = (await loc.text_content()) or sel
                        raise RuntimeError(text.strip())
                except PlaywrightTimeoutError:
                    continue
            await asyncio.sleep(1)

    async def _fill_text_fields(self, page, title, description, link):
        await _fill_best_effort(page, title[:100], [
            '[data-test-id="pin-draft-title"]',
            'textarea[placeholder*="title" i]',
            'textarea[placeholder*="назв" i]',
            'input[placeholder*="title" i]',
            'input[placeholder*="назв" i]',
            '[aria-label*="title" i]',
            '[aria-label*="назв" i]',
            'div[contenteditable="true"][data-test-id*="title"]',
        ])
        await _fill_best_effort(page, description[:500], [
            '[data-test-id="pin-draft-description"]',
            'textarea[placeholder*="description" i]',
            'textarea[placeholder*="опис" i]',
            '[aria-label*="description" i]',
            '[aria-label*="опис" i]',
            'div[contenteditable="true"][data-test-id*="description"]',
        ])
        if link:
            await _fill_best_effort(page, link, [
                '[data-test-id="pin-draft-link"]',
                'input[placeholder*="link" i]',
                'input[placeholder*="ссыл" i]',
                '[aria-label*="link" i]',
                '[aria-label*="ссыл" i]',
            ])

    async def _select_board(self, page, board_name: str):
        if not board_name:
            raise RuntimeError("Не задан PINTEREST_BOARD_NAME")
        for sel in [
            'button[aria-label*="board" i]',
            'button[aria-label*="доск" i]',
            '[data-test-id*="board-picker"]',
            '[data-test-id*="board-dropdown"]',
            'div[role="button"]:has-text("Выберите доску")',
            'div[role="button"]:has-text("Choose board")',
        ]:
            try:
                await page.locator(sel).first.click(timeout=3000)
                await asyncio.sleep(1)
                break
            except Exception:
                pass

        for sel in [
            'input[placeholder*="search" i]',
            'input[placeholder*="иск" i]',
            'input[aria-label*="board" i]',
            'input[aria-label*="доск" i]',
        ]:
            try:
                await page.locator(sel).first.fill(board_name, timeout=3000)
                await asyncio.sleep(1)
                break
            except Exception:
                pass

        for loc in [
            page.get_by_role("option", name=board_name),
            page.get_by_role("button", name=board_name),
            page.get_by_role("link", name=board_name),
            page.locator(f'text="{board_name}"'),
        ]:
            try:
                await loc.first.click(timeout=5000)
                await asyncio.sleep(1)
                return
            except Exception:
                pass
        raise RuntimeError(f"Не нашёл доску '{board_name}' в списке")

    async def _publish(self, page):
        for loc in [
            page.get_by_role("button", name="Publish"),
            page.get_by_role("button", name="Опубликовать"),
            page.get_by_role("button", name="Save"),
            page.locator('[data-test-id="board-dropdown-save-button"]'),
            page.locator('button:has-text("Publish")'),
            page.locator('button:has-text("Опубликовать")'),
            page.locator('button:has-text("Save")'),
        ]:
            try:
                await loc.first.click(timeout=5000)
                return
            except Exception:
                pass
        raise RuntimeError("Кнопка публикации не найдена")


async def _fill_best_effort(page, value: str, selectors: list[str]):
    if not value:
        return
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(timeout=5000)
            tag_name = await loc.evaluate("el => el.tagName.toLowerCase()")
            if tag_name in {"input", "textarea"}:
                await loc.fill(value)
            else:
                await loc.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.type(value)
            return
        except Exception:
            pass


async def _is_logged_in(page) -> bool:
    if "login" in page.url:
        return False
    for sel in [
        '[data-test-id="header-avatar"]',
        '[data-test-id="homefeed-feed"]',
        '[data-test-id="profile-menu-button"]',
        'div[data-test-id="header-profile"]',
        'button[aria-label*="profile" i]',
    ]:
        try:
            await page.locator(sel).first.wait_for(timeout=2500)
            return True
        except Exception:
            pass
    return False


async def _login(page):
    await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(3)
    await page.locator('#email').first.fill(PINTEREST_EMAIL)
    await page.locator('#password').first.fill(PINTEREST_PASSWORD)
    for loc in [
        page.get_by_role("button", name="Log in"),
        page.get_by_role("button", name="Войти"),
        page.locator('button[type="submit"]'),
    ]:
        try:
            await loc.first.click(timeout=5000)
            break
        except Exception:
            pass
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(8)


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
