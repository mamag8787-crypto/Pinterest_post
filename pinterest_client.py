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
                chat_id=_owner_id, photo=f,
                caption=f"🔍 {label}\n{page.url}"
            )
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")


class PinterestClient:
    def __init__(self, **kwargs):
        pass

    async def create_video_pin(self, video_path, title, description, link="") -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
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
                # Шаг 1: открываем главную
                await page.goto("https://www.pinterest.com/", wait_until="commit", timeout=30000)
                await asyncio.sleep(3)

                if not await _is_logged_in(page):
                    await _login(page)
                    cookies = await context.cookies()
                    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
                    Path(SESSION_FILE).write_text(json.dumps(cookies))

                await _send_screenshot(page, "1_logged_in")

                # Шаг 2: переходим на создание пина (pin-creation-tool для видео)
                try:
                    await page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(4)
                    logger.info(f"pin-creation-tool URL: {page.url}")
                except Exception as e:
                    logger.warning(f"pin-creation-tool failed: {e}")
                    # Запасной: кликаем Создать в меню
                    await page.goto("https://www.pinterest.com/", wait_until="commit", timeout=30000)
                    await asyncio.sleep(3)
                    try:
                        create_btn = await page.wait_for_selector(
                            "a[href*=pin-creation], [data-test-id='header-create-menu-trigger'], button:has-text('Создать')",
                            timeout=8000
                        )
                        await create_btn.click()
                        await asyncio.sleep(2)
                        pin_option = await page.wait_for_selector(
                            "a:has-text('Создать пин'), [href*=pin-creation]", timeout=5000
                        )
                        await pin_option.click()
                        await asyncio.sleep(3)
                    except Exception as e2:
                        logger.warning(f"Menu click failed: {e2}")
                await _send_screenshot(page, "2_pin_builder")

                # Шаг 3: закрываем онбординг
                await _dismiss_onboarding(page)
                await asyncio.sleep(2)
                await _send_screenshot(page, "3_after_dismiss")

                # Шаг 4: ищем input[type=file] всеми способами
                logger.info("Ищем file input...")
                file_input = None

                # Способ A: стандартный поиск
                try:
                    file_input = await page.wait_for_selector('input[type="file"]', timeout=10000)
                    logger.info("File input найден стандартно")
                except PlaywrightTimeout:
                    pass

                # Способ B: через evaluate
                if not file_input:
                    try:
                        await page.evaluate("""() => {
                            const inp = document.querySelector('input[type="file"]');
                            if (inp) {
                                inp.style.display = 'block';
                                inp.style.visibility = 'visible';
                                inp.style.opacity = '1';
                            }
                        }""")
                        file_input = await page.query_selector('input[type="file"]')
                        logger.info("File input найден через JS")
                    except Exception as e:
                        logger.warning(f"JS file input: {e}")

                if not file_input:
                    await _send_screenshot(page, "4_no_file_input")
                    # Логируем HTML для диагностики
                    html = await page.content()
                    logger.error(f"HTML (первые 3000 символов): {html[:3000]}")
                    return {"success": False, "error": "Не найден input[type=file]. Скриншот в TG."}

                # Загружаем файл
                await file_input.set_input_files(video_path)
                logger.info("Файл передан в input")
                await asyncio.sleep(5)
                await _send_screenshot(page, "4_after_upload")

                # Шаг 5: ждём полей формы — пробуем разные селекторы
                logger.info("Ждём полей формы...")
                title_selector = None
                selectors_to_try = [
                    '[data-test-id="pin-draft-title"]',
                    '[data-test-id="pin-title-input"]',
                    'textarea[placeholder*="заголовок"], textarea[placeholder*="title"], textarea[placeholder*="Title"]',
                    'input[name="title"]',
                    '[aria-label*="заголовок"], [aria-label*="Title"]',
                ]
                for sel in selectors_to_try:
                    try:
                        await page.wait_for_selector(sel, timeout=30000)
                        title_selector = sel
                        logger.info(f"Найден селектор заголовка: {sel}")
                        break
                    except PlaywrightTimeout:
                        logger.info(f"Селектор не найден: {sel}")
                        continue

                if not title_selector:
                    await _send_screenshot(page, "5_no_title")
                    html = await page.content()
                    logger.error(f"HTML после upload (первые 3000): {html[:3000]}")
                    return {"success": False, "error": "Поле заголовка не найдено. Скриншот в TG."}

                await asyncio.sleep(2)
                await _send_screenshot(page, "5_form_ready")

                # Заполняем форму
                title_field = await page.query_selector(title_selector)
                if title_field:
                    await title_field.click()
                    await asyncio.sleep(0.4)
                    await title_field.type(title[:100], delay=50)

                # Описание — ищем похожим образом
                for desc_sel in ['[data-test-id="pin-draft-description"]',
                                  'textarea[placeholder*="описание"], textarea[placeholder*="description"]',
                                  '[aria-label*="описание"], [aria-label*="Description"]']:
                    desc_field = await page.query_selector(desc_sel)
                    if desc_field:
                        await desc_field.click()
                        await asyncio.sleep(0.3)
                        await desc_field.type(description[:500], delay=30)
                        break

                # Ссылка
                if link:
                    for link_sel in ['[data-test-id="pin-draft-link"]',
                                      'input[placeholder*="ссылк"], input[placeholder*="link"]',
                                      '[aria-label*="ссылк"], [aria-label*="Link"]']:
                        link_field = await page.query_selector(link_sel)
                        if link_field:
                            await link_field.click()
                            await asyncio.sleep(0.3)
                            await link_field.type(link, delay=50)
                            break

                # Доска
                await _select_board(page, PINTEREST_BOARD)
                await asyncio.sleep(1)
                await _send_screenshot(page, "6_before_publish")

                # Публикуем
                for pub_sel in ['[data-test-id="board-dropdown-save-button"]',
                                 'button:has-text("Опубликовать")',
                                 'button:has-text("Publish")',
                                 'button:has-text("Save")']:
                    try:
                        btn = await page.wait_for_selector(pub_sel, timeout=5000)
                        await btn.click()
                        logger.info(f"Нажали публикацию: {pub_sel}")
                        break
                    except PlaywrightTimeout:
                        continue

                # Ждём успеха
                try:
                    await page.wait_for_selector(
                        '[data-test-id="pin-save-success"], [class*="successToast"], [class*="success"]',
                        timeout=30000
                    )
                except PlaywrightTimeout:
                    pass

                await asyncio.sleep(3)
                await _send_screenshot(page, "7_final")

                pin_id = _extract_pin_id(page.url)
                return {"success": True, "pin_id": pin_id or "unknown"}

            except Exception as e:
                logger.error(f"Ошибка: {e}")
                try:
                    await _send_screenshot(page, "error")
                except Exception:
                    pass
                return {"success": False, "error": str(e)}
            finally:
                await browser.close()


async def _dismiss_onboarding(page):
    # Escape
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
    except Exception:
        pass
    # JS клик по кнопкам
    for _ in range(5):
        try:
            clicked = await page.evaluate("""() => {
                const targets = ['Далее', 'Next', 'Начать', 'Done', 'Got it', 'Понятно', 'Закрыть'];
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => targets.some(t => b.textContent.trim().includes(t)));
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            if not clicked:
                break
            await asyncio.sleep(0.8)
        except Exception:
            break
    # Ядерный: удаляем все диалоги
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('[role="dialog"],[data-test-id*="modal"],[class*="overlay"],[class*="Modal"]')
                .forEach(el => el.remove());
        }""")
        await asyncio.sleep(0.5)
    except Exception:
        pass


async def _is_logged_in(page) -> bool:
    url = page.url
    if "business/hub" in url or ("pinterest.com" in url and "/login" not in url and url != "https://www.pinterest.com/"):
        logger.info(f"Залогинен (URL: {url})")
        return True
    try:
        await page.wait_for_selector('[data-test-id="header-avatar"],[data-test-id="homefeed-feed"]', timeout=5000)
        return True
    except PlaywrightTimeout:
        return False


async def _login(page):
    logger.info("Логинимся...")
    try:
        await page.goto("https://www.pinterest.com/login/", wait_until="commit", timeout=30000)
    except Exception:
        pass
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
    except Exception:
        pass
    await asyncio.sleep(3)
    logger.info(f"Логин завершён, URL: {page.url}")


async def _select_board(page, board_name: str):
    for sel in ['[data-test-id="board-dropdown-select-button"]',
                'button:has-text("Красив")', '[data-test-id*="board"]']:
        try:
            btn = await page.wait_for_selector(sel, timeout=8000)
            await btn.click()
            await asyncio.sleep(1)
            break
        except PlaywrightTimeout:
            continue

    search = await page.query_selector('[data-test-id="board-search-input"]')
    if search:
        await search.type(board_name, delay=60)
        await asyncio.sleep(1)

    try:
        option = await page.wait_for_selector(
            f'[data-test-id="board-row"]:has-text("{board_name}")', timeout=8000
        )
        await option.click()
    except PlaywrightTimeout:
        logger.warning(f"Доска '{board_name}' не найдена")


def _extract_pin_id(url: str) -> str:
    import re
    m = re.search(r'/pin/(\d+)/', url)
    return m.group(1) if m else ""
