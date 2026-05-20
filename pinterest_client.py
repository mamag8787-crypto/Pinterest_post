import asyncio
import logging
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "0") == "1"
logger = logging.getLogger(__name__)
logger.warning("PINTEREST_CLIENT_VERSION=final_clean_v3")

PINTEREST_EMAIL = os.getenv("PINTEREST_EMAIL")
PINTEREST_PASSWORD = os.getenv("PINTEREST_PASSWORD")
PINTEREST_BOARD = os.getenv("PINTEREST_BOARD_NAME", "").strip()

SESSION_FILE = Path(os.getenv("SESSION_FILE", "/data/pinterest_state.json"))
LEGACY_SESSION_FILE = Path("/data/pinterest_session.json")

PINTEREST_CREATE_URL = os.getenv(
    "PINTEREST_CREATE_URL",
    "https://www.pinterest.com/pin-creation-tool/",
)
PINTEREST_FALLBACK_CREATE_URL = os.getenv(
    "PINTEREST_FALLBACK_CREATE_URL",
    "https://www.pinterest.com/pin-builder/",
)

BOOTSTRAP_LOGIN = os.getenv("PINTEREST_BOOTSTRAP_LOGIN", "0") == "1"
BROWSER_CHANNEL = os.getenv("PINTEREST_BROWSER_CHANNEL", "chrome").strip() or "chrome"
BROWSER_EXECUTABLE_PATH = os.getenv("PINTEREST_EXECUTABLE_PATH", "").strip() or None

_bot_instance = None
_owner_id = int(os.getenv("ALLOWED_USER_ID", "0"))


def set_bot(bot):
    global _bot_instance
    _bot_instance = bot


async def _send_screenshot(page, label="debug"):
    if not DEBUG_SCREENSHOTS:
        return
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


def _existing_session_file() -> Path | None:
    for candidate in (SESSION_FILE, LEGACY_SESSION_FILE):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _norm_text(value: str) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    value = value.replace("_", " ")
    value = value.replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def _is_bad_board_text(norm: str) -> bool:
    if not norm:
        return True

    bad_tokens = (
        "черновик",
        "черновики",
        "черновики пина",
        "срок действия",
        "дней до истечения",
        "дня до истечения",
        "день до истечения",
        "выбрать все",
        "создать доску",
        "создать",
        "найдена доска",
        "publish",
        "опубликовать",
        "save",
        "сохранить",
    )

    if any(token in norm for token in bad_tokens):
        return True

    if re.fullmatch(r"\d+:\d+", norm):
        return True

    return False


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

                # Чистим старые черновики ДО загрузки нового видео
                deleted_before = await self._purge_drafts(page, limit=60)
                if deleted_before:
                    logger.info("Перед стартом удалено черновиков: %s", deleted_before)
                    await page.wait_for_timeout(1200)
                    await self._open_pin_builder(page)

                file_input = await self._find_file_input(page)
                if not file_input:
                    await _send_screenshot(page, "error_no_file_input")
                    return {
                        "success": False,
                        "error": "Не найден input[type=file] в pin builder [final_clean_v3]",
                    }

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

                # если что-то упало после загрузки — удаляем созданный черновик
                try:
                    deleted_after_error = await self._purge_drafts(page, limit=10)
                    if deleted_after_error:
                        logger.info("После ошибки удалено черновиков: %s", deleted_after_error)
                except Exception as cleanup_error:
                    logger.warning("Не удалось почистить черновики после ошибки: %s", cleanup_error)

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
            await page.goto(
                "https://www.pinterest.com/login/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_timeout(60000)

            if await _is_logged_in(page):
                await context.storage_state(path=str(SESSION_FILE))
                return

            raise RuntimeError(
                "Ручной вход не завершён. Сохрани сессию локально и загрузи файл состояния. [final_clean_v3]"
            )

        if not PINTEREST_EMAIL or not PINTEREST_PASSWORD:
            raise RuntimeError("Нет PINTEREST_EMAIL или PINTEREST_PASSWORD [final_clean_v3]")

        await _login(page)

        if not await _is_logged_in(page):
            raise RuntimeError(
                "Pinterest login не прошёл. Pinterest режет headless-логин. Нужна сохранённая сессия SESSION_FILE. [final_clean_v3]"
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

        raise RuntimeError("Не удалось открыть pin builder [final_clean_v3]")

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

        for sel in [
            'input[type="file"]',
            'input[accept*="video"]',
            'input[accept*="image"]',
        ]:
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
            raise RuntimeError("Не задан PINTEREST_BOARD_NAME [final_clean_v3]")

        target = _norm_text(board_name)
        logger.info("Ищу доску Pinterest: %s", board_name)

        async def _in_form_area(loc) -> bool:
            try:
                if await loc.count() == 0:
                    return False
                if not await loc.is_visible():
                    return False
                box = await loc.bounding_box()
                if not box:
                    return False
                return box["x"] >= 450 and box["y"] >= 120
            except Exception:
                return False

        async def _click_if_good(loc, label: str) -> bool:
            try:
                if await _in_form_area(loc):
                    await loc.click(timeout=4000)
                    logger.info("Открыл выбор доски через %s", label)
                    return True
            except Exception:
                pass
            return False

        already_selected = [
            page.get_by_text(board_name, exact=True).first,
            page.locator(f'xpath=//*[normalize-space()="{board_name}"]').first,
        ]

        for loc in already_selected:
            try:
                if await _in_form_area(loc):
                    logger.info("Доска уже выбрана: %s", board_name)
                    return
            except Exception:
                pass

        openers = [
            ("xpath-target", page.locator(f'xpath=//*[normalize-space()="{board_name}"]').first),
            ("xpath-choose-ru", page.locator('xpath=//*[normalize-space()="Выберите доску"]').first),
            ("xpath-choose-en", page.locator('xpath=//*[normalize-space()="Select board"]').first),
            ("data-test-board-picker", page.locator('[data-test-id*="board-picker"]').first),
            ("data-test-board-dropdown", page.locator('[data-test-id*="board-dropdown"]').first),
            ("text-choose-ru", page.get_by_text("Выберите доску", exact=True).first),
            ("text-choose-en", page.get_by_text("Select board", exact=True).first),
        ]

        opened = False
        for label, loc in openers:
            if await _click_if_good(loc, label):
                opened = True
                break

        if not opened:
            opened = await page.evaluate(
                """
                ({ target }) => {
                    function norm(v) {
                        return (v || "")
                            .replace(/\\u00a0/g, " ")
                            .replace(/_/g, " ")
                            .replace(/-/g, " ")
                            .replace(/\\s+/g, " ")
                            .trim()
                            .toLowerCase();
                    }

                    function clickable(el) {
                        let cur = el;
                        for (let i = 0; i < 6 && cur; i++) {
                            const role = (cur.getAttribute("role") || "").toLowerCase();
                            const tag = (cur.tagName || "").toLowerCase();
                            if (
                                tag === "button" ||
                                role === "button" ||
                                role === "combobox" ||
                                cur.getAttribute("aria-haspopup") === "listbox"
                            ) {
                                return cur;
                            }
                            cur = cur.parentElement;
                        }
                        return el;
                    }

                    const wanted = [target, "выберите доску", "select board"];
                    const nodes = Array.from(document.querySelectorAll("div, button, span"));

                    for (const el of nodes) {
                        const rect = el.getBoundingClientRect();
                        if (rect.left < 450 || rect.top < 120) continue;

                        const style = window.getComputedStyle(el);
                        if (style.display === "none" || style.visibility === "hidden") continue;

                        const txt = norm(el.innerText || el.textContent || "");
                        if (!txt) continue;

                        if (wanted.includes(txt)) {
                            clickable(el).click();
                            return true;
                        }
                    }

                    return false;
                }
                """,
                {"target": target},
            )

        if not opened:
            raise RuntimeError("Не нашёл кнопку выбора доски [final_clean_v3]")

        await page.wait_for_timeout(1200)

        search_selectors = [
            'input[placeholder*="Поиск" i]',
            'input[placeholder*="Search" i]',
            'input[aria-label*="Поиск" i]',
            'input[aria-label*="Search" i]',
            'input[aria-label*="board" i]',
            'input[aria-label*="доск" i]',
            'input[role="searchbox"]',
            'input[type="text"]',
        ]

        search_filled = False
        for sel in search_selectors:
            try:
                loc = page.locator(sel).first
                if await _in_form_area(loc):
                    await loc.click(timeout=2000)
                    await loc.fill("")
                    await loc.fill(board_name)
                    await page.wait_for_timeout(1200)
                    search_filled = True
                    logger.info("Поиск доски заполнен через %s", sel)
                    break
            except Exception:
                pass

        if not search_filled:
            logger.warning("Не нашёл поле поиска доски, продолжаю без него [final_clean_v3]")

        seen = []
        candidate_selectors = [
            '[role="option"]',
            '[data-test-id*="board"]',
            'div[role="button"]',
            'button',
            'li',
            'div',
            'span',
        ]

        for sel in candidate_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
            except Exception:
                continue

            for i in range(min(count, 200)):
                item = loc.nth(i)
                try:
                    if not await _in_form_area(item):
                        continue

                    txt = await item.inner_text(timeout=1000)
                    norm = _norm_text(txt)

                    if not norm or _is_bad_board_text(norm):
                        continue

                    if norm not in seen:
                        seen.append(norm)

                    if norm == target:
                        await item.click(timeout=3000)
                        logger.info("Доска выбрана exact match: %s", txt)
                        await page.wait_for_timeout(800)
                        return
                except Exception:
                    continue

        for sel in candidate_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
            except Exception:
                continue

            for i in range(min(count, 200)):
                item = loc.nth(i)
                try:
                    if not await _in_form_area(item):
                        continue

                    txt = await item.inner_text(timeout=1000)
                    norm = _norm_text(txt)

                    if not norm or _is_bad_board_text(norm):
                        continue

                    if norm not in seen:
                        seen.append(norm)

                    if target in norm and len(norm) < 120:
                        await item.click(timeout=3000)
                        logger.info("Доска выбрана contains match: %s", txt)
                        await page.wait_for_timeout(800)
                        return
                except Exception:
                    continue

        clicked = await page.evaluate(
            """
            ({ target, badTokens }) => {
                function norm(v) {
                    return (v || "")
                        .replace(/\\u00a0/g, " ")
                        .replace(/_/g, " ")
                        .replace(/-/g, " ")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
                }

                const nodes = Array.from(document.querySelectorAll("[role='option'], div, button, span, li"));

                for (const el of nodes) {
                    const rect = el.getBoundingClientRect();
                    if (rect.left < 450 || rect.top < 120) continue;

                    const style = window.getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden") continue;

                    const txt = norm(el.innerText || el.textContent || "");
                    if (!txt) continue;
                    if (badTokens.some(token => txt.includes(token))) continue;
                    if (/^\\d+:\\d+$/.test(txt)) continue;

                    if (txt === target) {
                        el.click();
                        return txt;
                    }
                }

                for (const el of nodes) {
                    const rect = el.getBoundingClientRect();
                    if (rect.left < 450 || rect.top < 120) continue;

                    const style = window.getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden") continue;

                    const txt = norm(el.innerText || el.textContent || "");
                    if (!txt) continue;
                    if (badTokens.some(token => txt.includes(token))) continue;
                    if (/^\\d+:\\d+$/.test(txt)) continue;

                    if (txt.includes(target) && txt.length < 120) {
                        el.click();
                        return txt;
                    }
                }

                return null;
            }
            """,
            {
                "target": target,
                "badTokens": [
                    "черновик",
                    "черновики",
                    "черновики пина",
                    "срок действия",
                    "дней до истечения",
                    "дня до истечения",
                    "день до истечения",
                    "выбрать все",
                    "создать доску",
                    "создать",
                    "найдена доска",
                    "publish",
                    "опубликовать",
                    "save",
                    "сохранить",
                ],
            },
        )

        if clicked:
            logger.info("Доска выбрана JS fallback: %s", clicked)
            await page.wait_for_timeout(800)
            return

        preview = ", ".join(seen[:20])
        raise RuntimeError(
            f"Не нашёл доску '{board_name}' в списке. Чистые варианты: {preview} [final_clean_v3]"
        )

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

        raise RuntimeError("Кнопка публикации не найдена [final_clean_v3]")

    async def _purge_drafts(self, page, limit=60):
        await page.wait_for_timeout(700)

        # если панели черновиков нет — просто выходим
        try:
            sidebar = page.locator('text=Черновики пина').first
            if await sidebar.count() == 0 or not await sidebar.is_visible():
                return 0
        except Exception:
            return 0

        deleted = 0

        for _ in range(limit):
            menu_btn = await self._find_draft_menu_button(page)
            if menu_btn is None:
                break

            try:
                await menu_btn.click(timeout=1500)
                await page.wait_for_timeout(300)
            except Exception:
                break

            delete_clicked = False
            delete_locators = [
                page.get_by_role("menuitem", name="Удалить").first,
                page.get_by_role("menuitem", name="Delete").first,
                page.get_by_text("Удалить", exact=True).first,
                page.get_by_text("Delete", exact=True).first,
            ]

            for loc in delete_locators:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1500)
                        delete_clicked = True
                        break
                except Exception:
                    pass

            if not delete_clicked:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                break

            await page.wait_for_timeout(500)

            confirm_locators = [
                page.get_by_role("button", name="Удалить").first,
                page.get_by_role("button", name="Delete").first,
                page.get_by_text("Удалить", exact=True).last,
                page.get_by_text("Delete", exact=True).last,
            ]

            for loc in confirm_locators:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1200)
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(800)
            deleted += 1

        if deleted:
            logger.info("Удалено черновиков: %s", deleted)

        return deleted

    async def _find_draft_menu_button(self, page):
        selectors = ['button', '[role="button"]']

        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
            except Exception:
                continue

            for i in range(min(count, 150)):
                item = loc.nth(i)
                try:
                    if not await item.is_visible():
                        continue

                    box = await item.bounding_box()
                    if not box:
                        continue

                    # левая панель черновиков, район трёх точек
                    if not (250 <= box["x"] <= 340 and 170 <= box["y"] <= 1200):
                        continue

                    if box["width"] > 60 or box["height"] > 60:
                        continue

                    return item
                except Exception:
                    continue

        return None


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

    await page.locator("#email").first.fill(PINTEREST_EMAIL)
    await page.locator("#password").first.fill(PINTEREST_PASSWORD)

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
    m = re.search(r"/pin/(\\d+)/", url)
    return m.group(1) if m else ""
