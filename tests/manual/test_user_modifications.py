import asyncio
from playwright.async_api import async_playwright

async def verify_user_changes():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        print("=== Step 1: Open Dashboard in Default Dark Mode ===")
        await page.goto("http://localhost:8000")
        await page.wait_for_selector(".queue-page-wrapper")

        # 1. Verify Pure Black theme
        body_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        card_bg = await page.evaluate("getComputedStyle(document.querySelector('.lead-card')).backgroundColor")
        print(f"Dark Theme: body_bg={body_bg}, card_bg={card_bg}")
        assert body_bg == "rgb(0, 0, 0)", f"Expected pure black rgb(0, 0, 0), got {body_bg}"
        assert card_bg == "rgb(12, 12, 12)", f"Expected pure dark neutral rgb(12, 12, 12), got {card_bg}"

        # 2. Verify right side lead card container has NO duplicate queue tiles or sidebar counts
        right_panel_extra_tiles = await page.evaluate(
            "document.querySelectorAll('#queue-card-container #queue-tiles-container').length"
        )
        right_panel_extra_counts = await page.evaluate(
            "document.querySelectorAll('#queue-card-container #sidebar-counts').length"
        )
        print(f"Right panel duplicate tiles: {right_panel_extra_tiles}, duplicate counts: {right_panel_extra_counts}")
        assert right_panel_extra_tiles == 0, "Duplicate queue tiles found under lead card!"
        assert right_panel_extra_counts == 0, "Duplicate sidebar counts found under lead card!"

        # 3. Verify decluttered pitch cockpit (no psychology lecture blocks)
        psycho_explainer_count = await page.evaluate("document.querySelectorAll('.strategy-explainer').length")
        print(f"Psychology explainer blocks: {psycho_explainer_count}")
        assert psycho_explainer_count == 0, "Strategy explainer text was not removed!"

        # Verify tabs and message
        tabs = await page.query_selector_all(".approach-tab")
        print(f"Found {len(tabs)} approach tabs")
        assert len(tabs) == 3

        # Capture pure black screenshot
        await page.screenshot(path="screenshot_pure_black.png")
        print("Captured screenshot_pure_black.png")

        # 4. Switch to Light Mode (Warm Yellow Cream)
        print("\n=== Step 2: Switch to Light Mode ===")
        await page.click("#top-theme-btn-light")
        await asyncio.sleep(0.4)

        light_body_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        light_card_bg = await page.evaluate("getComputedStyle(document.querySelector('.lead-card')).backgroundColor")
        print(f"Light Theme: body_bg={light_body_bg}, card_bg={light_card_bg}")
        # #fbf7ee is rgb(251, 247, 238)
        assert light_body_bg == "rgb(251, 247, 238)", f"Expected rgb(251, 247, 238), got {light_body_bg}"
        # #fffdf9 is rgb(255, 253, 249)
        assert light_card_bg == "rgb(255, 253, 249)", f"Expected rgb(255, 253, 249), got {light_card_bg}"

        await page.screenshot(path="screenshot_warm_cream.png")
        print("Captured screenshot_warm_cream.png")

        # 5. Switch back to Dark Mode
        await page.click("#top-theme-btn-dark")
        await asyncio.sleep(0.3)

        print("\n=== ALL USER REQUEST MODIFICATIONS VERIFIED 100% ===")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_user_changes())
