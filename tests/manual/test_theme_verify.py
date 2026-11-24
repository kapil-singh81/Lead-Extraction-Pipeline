import asyncio
from playwright.async_api import async_playwright

async def verify_theme_pipeline():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        print("=== Step 1: Fresh Visit (Default should be Dark Mode) ===")
        await page.goto("http://localhost:8000")
        await page.wait_for_selector(".queue-page-wrapper")

        html_class = await page.evaluate("document.documentElement.className")
        body_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        card_bg = await page.evaluate("getComputedStyle(document.querySelector('.lead-card')).backgroundColor")
        dark_btn_active = await page.evaluate("document.getElementById('top-theme-btn-dark').classList.contains('active')")
        light_btn_active = await page.evaluate("document.getElementById('top-theme-btn-light').classList.contains('active')")
        print(f"Initial: html_class='{html_class}', body_bg={body_bg}, card_bg={card_bg}")
        print(f"Buttons: dark_active={dark_btn_active}, light_active={light_btn_active}")
        assert "dark" in html_class, "Expected html to have 'dark' class"
        assert body_bg == "rgb(9, 13, 22)", f"Expected rgb(9, 13, 22), got {body_bg}"
        assert dark_btn_active is True
        assert light_btn_active is False

        await page.screenshot(path="screenshot_dark_verified.png")
        print("Captured screenshot_dark_verified.png")

        print("\n=== Step 2: Click 'Light' in Top Bar ===")
        await page.click("#top-theme-btn-light")
        await asyncio.sleep(0.3)
        html_class_light = await page.evaluate("document.documentElement.className")
        body_bg_light = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        card_bg_light = await page.evaluate("getComputedStyle(document.querySelector('.lead-card')).backgroundColor")
        dark_btn_active = await page.evaluate("document.getElementById('top-theme-btn-dark').classList.contains('active')")
        light_btn_active = await page.evaluate("document.getElementById('top-theme-btn-light').classList.contains('active')")
        storage_val = await page.evaluate("localStorage.getItem('theme')")
        print(f"After Light Click: html_class='{html_class_light}', body_bg={body_bg_light}, card_bg={card_bg_light}, storage={storage_val}")
        print(f"Buttons: dark_active={dark_btn_active}, light_active={light_btn_active}")
        assert "dark" not in html_class_light, "Expected dark class removed"
        assert body_bg_light == "rgb(248, 250, 252)", f"Expected rgb(248, 250, 252), got {body_bg_light}"
        assert light_btn_active is True

        await page.screenshot(path="screenshot_light_verified.png")
        print("Captured screenshot_light_verified.png")

        print("\n=== Step 3: Page Reload (Must persist Light Mode) ===")
        await page.reload()
        await page.wait_for_selector(".queue-page-wrapper")
        html_class_reload = await page.evaluate("document.documentElement.className")
        body_bg_reload = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        print(f"After Reload: html_class='{html_class_reload}', body_bg={body_bg_reload}")
        assert "dark" not in html_class_reload

        print("\n=== Step 4: Click 'Dark' in Sidebar Footer ===")
        await page.click("#side-theme-btn-dark")
        await asyncio.sleep(0.3)
        html_class_side = await page.evaluate("document.documentElement.className")
        body_bg_side = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        storage_side = await page.evaluate("localStorage.getItem('theme')")
        print(f"After Sidebar Dark Click: html_class='{html_class_side}', body_bg={body_bg_side}, storage={storage_side}")
        assert "dark" in html_class_side
        assert body_bg_side == "rgb(9, 13, 22)"

        print("\n=== Step 5: Test Alt+D Keyboard Shortcut ===")
        await page.keyboard.press("Alt+d")
        await asyncio.sleep(0.3)
        html_class_shortcut = await page.evaluate("document.documentElement.className")
        print(f"After Alt+D: html_class='{html_class_shortcut}'")
        assert "dark" not in html_class_shortcut, "Expected light mode via Alt+D"

        await page.keyboard.press("Alt+d")
        await asyncio.sleep(0.3)
        html_class_shortcut2 = await page.evaluate("document.documentElement.className")
        print(f"After second Alt+D: html_class='{html_class_shortcut2}'")
        assert "dark" in html_class_shortcut2, "Expected dark mode via Alt+D"

        print("\n=== ALL THEME VERIFICATION CHECKS PASSED PERFECTLY ===")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_theme_pipeline())
