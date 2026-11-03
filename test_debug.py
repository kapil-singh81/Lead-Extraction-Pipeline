import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        query = "plumbers Dallas TX"
        await page.goto(f"https://www.google.com/maps/search/{query}?hl=en", wait_until="domcontentloaded")
        await page.wait_for_selector("div[role='feed']", timeout=15000)
        
        feed_selector = "div[role='feed']"
        listing_card_sel = "div[role='feed'] div.Nv2PK, div[role='feed'] div[role='article']"

        cards = await page.locator(listing_card_sel).all()
        print(f"Pass 0: found {len(cards)} cards")

        # Let's click the first card
        card = cards[0]
        anchor = card.locator("a.hfpxzc").first
        print("Clicking card 0...")
        await anchor.click()
        await page.wait_for_timeout(2000)

        # Check detail pane title
        h1 = page.locator("h1.DUwDvf").first
        if await h1.count() > 0:
            print("Detail pane opened:", await h1.inner_text())

        # Press Escape
        print("Pressing Escape...")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)

        # Check if feed is still there and what active element is
        feed = page.locator("div[role='feed']")
        print("Feed count after Escape:", await feed.count())
        print("Feed visible?:", await feed.first.is_visible() if await feed.count() > 0 else False)

        # Check scrolling
        print("Attempting scrollBy on feed...")
        res = await page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (!el) return 'no el';
            const before = el.scrollTop;
            el.scrollBy(0, 800);
            return {before, after: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight};
        }""", feed_selector)
        print("Scroll result:", res)

        await page.wait_for_timeout(2000)
        new_cards = await page.locator(listing_card_sel).all()
        print(f"Cards after scroll: {len(new_cards)}")

        await browser.close()

asyncio.run(test())