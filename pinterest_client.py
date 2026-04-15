import os
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

PINTEREST_EMAIL = os.getenv("PINTEREST_EMAIL")
PINTEREST_PASSWORD = os.getenv("PINTEREST_PASSWORD")
PINTEREST_BOARD = os.getenv("PINTEREST_BOARD_NAME", "").strip()
SESSION_FILE = os.getenv("SESSION_FILE", "/data/pinterest_state.json")
PINTEREST_CREATE_URL = os.getenv("PINTEREST_CREATE_URL", "https://www.pinterest.com/pin-creation-tool/")
PINTEREST_FALLBACK_CREATE_URL = os.getenv("PINTEREST_FALLBACK_CREATE_URL", "https://www.pinterest.com/pin-builder/")

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
            await _bot_instance.send_photo(
                chat_id=_owner_id,
                photo=f,
                caption=f"🔍 {label}\n{page.url}",
            )
    except Exception as e:
        logger.error("Screenshot failed: %s", e)


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
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
            )

            if Path(SESSION_FILE).exists():
                try:
                    await context.storage_state(path=SESSION_FILE)
                except Exception:
                    pass
                try:
                    state = Path(SESSION_FILE).read_text(encoding="utf-8")
                    await context.add_cookies(__import__("json").loads(state).get("cookies", []))
                    logger.info("Storage/cookies loaded")
                except Exception as e:
                    logger.warning("State not loaded: %s", e)

            page = await context.new_page()

            try:
                await self._ensure_logged_in(page)
                await _send_screenshot(page, "1_logged_in")

                await self._open_pin_builder(page)
                await _send_screenshot(page, "2_builder_opened")

                file_input = await self._find_file_input(page)
                if not file_input:
                    await _send_screenshot(page, "error_no_file_input")
                    return {"success": False, "error": "Не найден input[type=file] в pin builder"}

                await file_input.set_input_files(video_path)
                logger.info("Video uploaded into file input")
                await asyncio.sleep(6)
                await _send_screenshot(page, "3_after_upload")

                await self._fill_text_fields(page, title, description, link)
                await self._select_board(page, PINTEREST_BOARD)
                await _send_screenshot(page, "4_filled")

                await self._publish(page)
                await asyncio.sleep(6)
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
                    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=SESSION_FILE)
                except Exception as e:
                    logger.warning("Could not save storage state: %s", e)
                await browser.close()

    async def _ensure_logged_in(self, page):
        await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        if await _is_logged_in(page):
            return
        if not PINTEREST_EMAIL or not PINTEREST_PASSWORD:
            raise RuntimeError("Нет PINTEREST_EMAIL или PINTEREST_PASSWORD")
        await _login(page)
        if not await _is_logged_in(page):
            raise RuntimeError("Pinterest login не прошёл")

    async def _open_pin_builder(self, page):
        urls = [PINTEREST_CREATE_URL, PINTEREST_FALLBACK_CREATE_URL]
        last_err = None
        for url in urls:
            try:
                logger.info("Open pin builder: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(4)
                if await self._page_has_builder(page):
                    return
            except Exception as e:
                last_err = e

        # last fallback via UI navigation
        await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)

        # Open left nav or hamburger if needed
        for sel in [
            'button[aria-label*="menu"]',
            'button[aria-label*="Меню"]',
            'div[role="button"][aria-label*="menu"]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=3000)
                await asyncio.sleep(1)
                break
            except Exception:
                pass

        for locator in [
            page.get_by_role("link", name="Create Pin"),
            page.get_by_role("link", name="Создать пин"),
            page.get_by_role("button", name="Create Pin"),
            page.get_by_role("button", name="Создать пин"),
            page.get_by_role("link", name="Create"),
            page.get_by_role("button", name="Create"),
            page.get_by_role("button", name="Создать"),
        ]:
            try:
                await locator.first.click(timeout=5000)
                await asyncio.sleep(3)
                if await self._page_has_builder(page):
                    return
            except Exception:
                pass

        raise RuntimeError(f"Не удалось открыть pin builder. Последняя ошибка: {last_err}")

    async def _page_has_builder(self, page) -> bool:
        candidates = [
            'input[type="file"]',
            'input[accept*="video"]',
            'input[accept*="image"]',
            '[data-test-id="pin-draft-title"]',
            'div[data-test-id*="pin-creation"]',
            'button:has-text("Publish")',
            'button:has-text("Опубликовать")',
        ]
        for sel in candidates:
            try:
                await page.locator(sel).first.wait_for(timeout=2500)
                return True
            except Exception:
                continue
        return "pin-builder" in page.url or "pin-creation-tool" in page.url

    async def _find_file_input(self, page):
        await page.evaluate(
            """() => {
                document.querySelectorAll('input[type="file"]').forEach(el => {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                    el.style.width = '1px';
                    el.style.height = '1px';
                });
            }"""
        )
        selectors = [
            'input[type="file"]',
            'input[accept*="video"]',
            'input[accept*="image"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(timeout=10000)
                return loc
            except Exception:
                continue

        # Sometimes the builder opens after clicking upload area
        for click_sel in [
            'button:has-text("Select files")',
            'button:has-text("Выбрать файлы")',
            'div:has-text("drag and drop")',
            'div:has-text("перетащите")',
        ]:
            try:
                await page.locator(click_sel).first.click(timeout=3000)
                await asyncio.sleep(1)
                loc = page.locator('input[type="file"]').first
                await loc.wait_for(timeout=5000)
                return loc
            except Exception:
                continue
        return None

    async def _fill_text_fields(self, page, title, description, link):
        await _fill_best_effort(
            page,
            title[:100],
            [
                '[data-test-id="pin-draft-title"]',
                'textarea[placeholder*="title" i]',
                'textarea[placeholder*="назв" i]',
                'input[placeholder*="title" i]',
                'input[placeholder*="назв" i]',
                '[aria-label*="title" i]',
                '[aria-label*="назв" i]',
                'div[contenteditable="true"][data-test-id*="title"]',
            ],
        )
        await _fill_best_effort(
            page,
            description[:500],
            [
                '[data-test-id="pin-draft-description"]',
                'textarea[placeholder*="description" i]',
                'textarea[placeholder*="опис" i]',
                '[aria-label*="description" i]',
                '[aria-label*="опис" i]',
                'div[contenteditable="true"][data-test-id*="description"]',
            ],
        )
        if link:
            await _fill_best_effort(
                page,
                link,
                [
                    '[data-test-id="pin-draft-link"]',
                    'input[placeholder*="link" i]',
                    'input[placeholder*="ссыл" i]',
                    '[aria-label*="link" i]',
                    '[aria-label*="ссыл" i]',
                ],
            )

    async def _select_board(self, page, board_name: str):
        if not board_name:
            return
        # Open board dropdown if visible
        triggers = [
            'button[aria-label*="board" i]',
            'button[aria-label*="доск" i]',
            '[data-test-id*="board-picker"]',
            '[data-test-id*="board-dropdown"]',
        ]
        opened = False
        for sel in triggers:
            try:
                await page.locator(sel).first.click(timeout=3000)
                opened = True
                await asyncio.sleep(1)
                break
            except Exception:
                continue

        # Some builders already show a combobox or search box
        for sel in [
            'input[placeholder*="search" i]',
            'input[placeholder*="иск" i]',
            'input[aria-label*="board" i]',
            'input[aria-label*="доск" i]',
        ]:
            try:
                loc = page.locator(sel).first
                await loc.fill(board_name, timeout=3000)
                await asyncio.sleep(1)
                break
            except Exception:
                continue

        options = [
            page.get_by_role("option", name=board_name),
            page.get_by_role("button", name=board_name),
            page.get_by_role("link", name=board_name),
            page.locator(f'text="{board_name}"'),
        ]
        for loc in options:
            try:
                await loc.first.click(timeout=5000)
                await asyncio.sleep(1)
                return
            except Exception:
                continue

        if opened:
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
                continue
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
            continue


async def _is_logged_in(page) -> bool:
    if "login" in page.url:
        return False
    for sel in [
        '[data-test-id="header-avatar"]',
        '[data-test-id="homefeed-feed"]',
        '[data-test-id="profile-menu-button"]',
        'a[href*="/settings/"]',
    ]:
        try:
            await page.locator(sel).first.wait_for(timeout=2500)
            return True
        except Exception:
            continue
    return False


async def _login(page):
    await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(3)
    email_input = page.locator('#email').first
    password_input = page.locator('#password').first
    await email_input.fill(PINTEREST_EMAIL)
    await password_input.fill(PINTEREST_PASSWORD)

    for loc in [
        page.get_by_role("button", name="Log in"),
        page.get_by_role("button", name="Войти"),
        page.locator('button[type="submit"]'),
    ]:
        try:
            await loc.first.click(timeout=5000)
            break
        except Exception:
            continue

    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(5)


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
