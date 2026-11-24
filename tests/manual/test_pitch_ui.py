import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def verify_pitch_ui():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        print("=== Step 1: Navigate to Dashboard ===")
        await page.goto("http://localhost:8000")
        await page.wait_for_selector(".pitch-cockpit-section")
        print("Pitch cockpit section found on page!")

        # Verify tabs
        tabs = await page.query_selector_all(".approach-tab")
        print(f"Found {len(tabs)} approach tabs on first card")
        assert len(tabs) >= 3, "Expected at least 3 tabs"

        # Check visible message
        visible_msg = await page.query_selector(".pitch-strategy-card:not([style*='display: none']) .message-bubble")
        msg_text = await visible_msg.inner_text()
        print(f"\nDefault Visible Message:\n{msg_text}")
        assert "Hey" in msg_text
        assert "screenshot" in msg_text or "radar" in msg_text

        # Test switching to No-Oriented tab
        tab_no = await page.query_selector(".approach-tab[data-approach='no_oriented']")
        if tab_no:
            await tab_no.click()
            await asyncio.sleep(0.3)
            pane_no = await page.query_selector(".pitch-strategy-card[data-approach='no_oriented']")
            is_visible = await pane_no.is_visible()
            print(f"No-Oriented pane visible: {is_visible}")
            assert is_visible is True

        # Test switching to Loss Aversion tab
        tab_loss = await page.query_selector(".approach-tab[data-approach='loss_aversion']")
        if tab_loss:
            await tab_loss.click()
            await asyncio.sleep(0.3)
            pane_loss = await page.query_selector(".pitch-strategy-card[data-approach='loss_aversion']")
            is_visible = await pane_loss.is_visible()
            print(f"Loss Aversion pane visible: {is_visible}")
            assert is_visible is True
            msg_loss = await pane_loss.query_selector(".message-bubble")
            text_loss = await msg_loss.inner_text()
            print(f"Loss Aversion text: {text_loss[:60]}...")
            assert "heads up" in text_loss
            assert "web guy" in text_loss

        # Test switching to Reputation tab
        tab_rep = await page.query_selector(".approach-tab[data-approach='reputation']")
        if tab_rep:
            await tab_rep.click()
            await asyncio.sleep(0.3)
            pane_rep = await page.query_selector(".pitch-strategy-card[data-approach='reputation']")
            is_visible = await pane_rep.is_visible()
            print(f"Reputation pane visible: {is_visible}")
            assert is_visible is True
            msg_rep = await pane_rep.query_selector(".message-bubble")
            text_rep = await msg_rep.inner_text()
            print(f"Reputation text: {text_rep[:60]}...")
            assert "Google reviews" in text_rep or "reputation" in text_rep

        # Test Copy Button
        copy_btn = await page.query_selector(".pitch-strategy-card[data-approach='reputation'] .btn-copy-pitch")
        await copy_btn.click()
        await asyncio.sleep(0.2)
        btn_html = await copy_btn.inner_text()
        print(f"Copy button after click: '{btn_html}'")
        assert "Copied" in btn_html

        # Take Dark Mode Screenshot
        await page.screenshot(path="screenshot_pitch_dark.png")
        print("\nSaved screenshot_pitch_dark.png")

        # Switch to Light Mode and Screenshot
        await page.click("#top-theme-btn-light")
        await asyncio.sleep(0.4)
        await page.screenshot(path="screenshot_pitch_light.png")
        print("Saved screenshot_pitch_light.png")

        print("\n=== ALL PITCH UI TESTS PASSED SUCCESSFULLY! ===")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_pitch_ui())
