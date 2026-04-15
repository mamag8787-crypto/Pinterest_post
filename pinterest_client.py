import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SESSION_FILE = Path('pinterest_state.json')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU")
        page = await context.new_page()
        await page.goto('https://www.pinterest.com/login/', wait_until='domcontentloaded')
        print('Войди в Pinterest вручную в открывшемся окне.')
        print('После входа и открытия главной страницы нажми Enter здесь в терминале.')
        input()
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(SESSION_FILE))
        print(f'Сессия сохранена в {SESSION_FILE.resolve()}')
        await browser.close()

asyncio.run(main())
