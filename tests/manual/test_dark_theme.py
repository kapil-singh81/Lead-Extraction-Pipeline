import asyncio
from playwright.async_api import async_playwright

async def verify_no_white_in_dark():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        print("=== Navigate to Dashboard in Dark Mode ===")
        await page.goto("http://localhost:8000")
        await page.wait_for_selector(".queue-page-wrapper")

        # 1. Check body text color
        body_color = await page.evaluate("getComputedStyle(document.body).color")
        body_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        print(f"Body: color={body_color}, bg={body_bg}")
        assert body_bg == "rgb(0, 0, 0)", f"Expected rgb(0, 0, 0), got {body_bg}"
        assert body_color != "rgb(255, 255, 255)", "Body text should not be pure glaring white!"

        # 2. Check btn-open-site
        btn_open_bg = await page.evaluate("getComputedStyle(document.querySelector('.btn-open-site')).backgroundColor")
        btn_open_color = await page.evaluate("getComputedStyle(document.querySelector('.btn-open-site')).color")
        print(f"btn-open-site: bg={btn_open_bg}, color={btn_open_color}")
        assert btn_open_bg != "rgb(255, 255, 255)" and btn_open_bg != "rgb(245, 245, 245)", "Visit Website button must not be white!"

        # 3. Check queue-search-wrap .search-input
        search_bg = await page.evaluate("getComputedStyle(document.querySelector('.queue-search-wrap .search-input')).backgroundColor")
        print(f"Search input: bg={search_bg}")
        assert search_bg != "rgb(255, 255, 255)", "Search input must not be white!"

        # 4. Check queue-stepper-bar
        stepper_bg = await page.evaluate("getComputedStyle(document.querySelector('.queue-stepper-bar')).backgroundColor")
        print(f"Stepper bar: bg={stepper_bg}")
        assert stepper_bg != "rgb(255, 255, 255)", "Stepper bar must not be white!"

        # 5. Check if ANY element inside .main-wrapper has white background
        white_elements = await page.evaluate("""() => {
            const all = document.querySelectorAll('*');
            const found = [];
            for (let el of all) {
                const cs = getComputedStyle(el);
                if (cs.display !== 'none' && cs.visibility !== 'hidden' && (cs.backgroundColor === 'rgb(255, 255, 255)' || cs.backgroundColor === 'rgb(245, 245, 245)' || cs.backgroundColor === 'rgb(248, 250, 252)')) {
                    found.push({ tag: el.tagName, class: el.className, id: el.id, bg: cs.backgroundColor });
                }
            }
            return found;
        }""")
        print(f"White background elements found in dark mode: {len(white_elements)}")
        for w in white_elements:
            print("  ->", w)
        assert len(white_elements) == 0, f"Found {len(white_elements)} elements with white background in dark mode!"

        # Save screenshot
        await page.screenshot(path="screenshot_pure_black_no_white.png")
        print("Captured screenshot_pure_black_no_white.png")

        print("\n=== PURE BLACK WITH ZERO WHITE GLARE VERIFIED 100% ===")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_no_white_in_dark())
