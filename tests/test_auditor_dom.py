import json
import pytest
from playwright.async_api import async_playwright

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

AUDITOR_CFG = CONFIG["auditor"]
EVAL_CFG = {
    "embed_domains": AUDITOR_CFG["form_embed_domains"],
    "form_keywords": AUDITOR_CFG["form_keywords"],
    "credit_patterns": AUDITOR_CFG["agency_credit_patterns"]
}


@pytest.mark.asyncio
async def test_footer_agency_credit_preservation():
    """
    P1-M04: Container-bounded isAgencyCredit check must mark the agency link as credit
    while preserving sibling social and contact links elsewhere in <footer>.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        html = """
        <!DOCTYPE html>
        <html>
        <body>
          <main>
            <h1>Contractor Business</h1>
          </main>
          <footer>
            <div class="socials">
              <a id="fb" href="https://facebook.com/mybiz">Facebook Page</a>
              <a id="contact" href="/contact-us">Contact Us</a>
            </div>
            <p class="credit">
              Website by <a id="agency" href="https://agency.com">Top Agency</a>
            </p>
          </footer>
        </body>
        </html>
        """
        await page.set_content(html)

        res = await page.evaluate(r"""(cfg) => {
            const creditPatterns = cfg.credit_patterns || [];
            const BLOCK_CONTAINER_TAGS = new Set(['BODY', 'MAIN', 'FOOTER', 'HEADER', 'NAV', 'SECTION', 'ASIDE']);
            function isAgencyCredit(a) {
                let curr = a;
                for (let i = 0; i < 3; i++) {
                    if (!curr) break;
                    if (i > 0 && BLOCK_CONTAINER_TAGS.has((curr.tagName || '').toUpperCase())) {
                        break;
                    }
                    const pText = (curr.innerText || curr.textContent || '').trim().toLowerCase();
                    const linkCount = curr.querySelectorAll ? curr.querySelectorAll('a[href]').length : 0;
                    if (i > 0 && (pText.length > 140 || linkCount > 3)) {
                        break;
                    }
                    if (creditPatterns.some(pat => pText.includes(pat))) {
                        return true;
                    }
                    curr = curr.parentElement;
                }
                return false;
            }

            const rawLinks = Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href,
                id: a.id,
                is_credit: isAgencyCredit(a)
            }));
            return rawLinks;
        }""", EVAL_CFG)

        links_by_id = {item["id"]: item["is_credit"] for item in res}
        assert links_by_id["agency"] is True, "Agency credit link must have is_credit=True"
        assert links_by_id["fb"] is False, "Facebook social link must have is_credit=False"
        assert links_by_id["contact"] is False, "Contact link must have is_credit=False"

        await browser.close()


@pytest.mark.asyncio
async def test_lead_capture_accuracy():
    """
    P1-M11:
    1. A homepage with ONLY a tel: link and no form/CTA must have has_tel_link=1 and has_lead_capture=0.
    2. A subpage with ONLY a WordPress search bar must have has_form=False.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # 1. Homepage with tel: only
        tel_html = """
        <!DOCTYPE html>
        <html>
        <body>
          <h1>Bare Roofing LLC</h1>
          <p>Call our office:</p>
          <a href="tel:2145551234">Click to Call (214) 555-1234</a>
        </body>
        </html>
        """
        await page.set_content(tel_html)

        home_res = await page.evaluate(r"""(cfg) => {
            const embedDomains = cfg.embed_domains || [];
            const formKeywords = cfg.form_keywords || [];

            const telLinks = Array.from(document.querySelectorAll('a[href^="tel:"]'));
            const hasTelLink = telLinks.length > 0;

            let formIframeSrc = null;
            for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
                const src = (ifr.src || '').toLowerCase();
                if (embedDomains.some(d => src.includes(d))) {
                    formIframeSrc = ifr.src;
                    break;
                }
            }

            const forms = Array.from(document.querySelectorAll('form'));
            const hasRealForm = forms.some(f => {
                const action = (f.getAttribute('action') || '').toLowerCase();
                const role = (f.getAttribute('role') || '').toLowerCase();
                if (role === 'search' || action.includes('search')) return false;
                const inputs = Array.from(f.querySelectorAll('input, textarea, select'));
                const nonSearchInputs = inputs.filter(inp => {
                    const t = (inp.getAttribute('type') || 'text').toLowerCase();
                    const n = (inp.getAttribute('name') || '').toLowerCase();
                    return t !== 'search' && t !== 'hidden' && !n.includes('search') && n !== 's';
                });
                return nonSearchInputs.length > 0;
            });

            const hasDirectInputs = document.querySelectorAll('input[type="tel"], input[type="email"], textarea').length > 0;
            const hasOnPageFormOrEmbed = hasRealForm || hasDirectInputs || (formIframeSrc !== null);

            const interactive = Array.from(document.querySelectorAll('a, button, input[type="submit"]')).map(el => ((el.innerText || el.value || '')).toLowerCase());
            const hasCta = interactive.some(t => formKeywords.some(kw => t.includes(kw)));
            const hasLeadCapture = (hasOnPageFormOrEmbed || hasCta);

            return {
                has_tel_link: hasTelLink ? 1 : 0,
                has_lead_capture: hasLeadCapture ? 1 : 0
            };
        }""", EVAL_CFG)

        assert home_res["has_tel_link"] == 1
        assert home_res["has_lead_capture"] == 0, "Bare tel: link without form or CTA must not satisfy has_lead_capture"

        # 2. Subpage with ONLY a WordPress search form
        search_html = """
        <!DOCTYPE html>
        <html>
        <body>
          <form role="search" method="get" action="https://example.com/search">
            <label for="s">Search our site:</label>
            <input type="search" id="s" name="s" value="" placeholder="Search...">
            <input type="submit" value="Search">
          </form>
        </body>
        </html>
        """
        await page.set_content(search_html)

        sub_res = await page.evaluate(r"""(cfg) => {
            const embedDomains = cfg.embed_domains || [];

            let formIframe = false;
            for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
                const src = (ifr.src || '').toLowerCase();
                if (embedDomains.some(d => src.includes(d))) {
                    formIframe = true;
                    break;
                }
            }

            const forms = Array.from(document.querySelectorAll('form'));
            const hasRealForm = forms.some(f => {
                const action = (f.getAttribute('action') || '').toLowerCase();
                const role = (f.getAttribute('role') || '').toLowerCase();
                if (role === 'search' || action.includes('search')) return false;
                const inputs = Array.from(f.querySelectorAll('input, textarea, select'));
                const nonSearchInputs = inputs.filter(inp => {
                    const t = (inp.getAttribute('type') || 'text').toLowerCase();
                    const n = (inp.getAttribute('name') || '').toLowerCase();
                    return t !== 'search' && t !== 'hidden' && !n.includes('search') && n !== 's';
                });
                return nonSearchInputs.length > 0;
            });

            const hasDirectInputs = Array.from(document.querySelectorAll('input[type="tel"], input[type="email"], textarea')).some(el => {
                const parentForm = el.closest('form');
                if (parentForm) {
                    const action = (parentForm.getAttribute('action') || '').toLowerCase();
                    const role = (parentForm.getAttribute('role') || '').toLowerCase();
                    if (role === 'search' || action.includes('search')) return false;
                }
                return true;
            });

            const hasForm = hasRealForm || hasDirectInputs || formIframe;
            return { has_form: hasForm };
        }""", EVAL_CFG)

        assert sub_res["has_form"] is False, "Search bar on subpage must not evaluate to has_form=True"

        await browser.close()


@pytest.mark.asyncio
async def test_embed_and_keyword_precision():
    """
    P2-M10:
    1. An iframe with 'platform-performance.html' and an anchor '/free-ebook' must NOT trigger has_lead_capture or form_url.
    2. A jotform iframe or an anchor '/book-now' MUST trigger has_lead_capture.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # False positive test: 'platform' (from former 'form') and 'free-ebook' (from former 'book')
        fp_html = """
        <!DOCTYPE html>
        <html>
        <body>
          <iframe src="https://cdn.example.com/platform-performance.html"></iframe>
          <a href="/free-ebook">Download Ebook</a>
        </body>
        </html>
        """
        await page.set_content(fp_html)

        fp_res = await page.evaluate(r"""(cfg) => {
            const embedDomains = cfg.embed_domains || [];
            const formKeywords = cfg.form_keywords || [];

            let formIframeSrc = null;
            for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
                const src = (ifr.src || '').toLowerCase();
                if (embedDomains.some(d => src.includes(d))) {
                    formIframeSrc = ifr.src;
                    break;
                }
            }

            const forms = Array.from(document.querySelectorAll('form'));
            const hasRealForm = forms.some(f => {
                const action = (f.getAttribute('action') || '').toLowerCase();
                const role = (f.getAttribute('role') || '').toLowerCase();
                if (role === 'search' || action.includes('search')) return false;
                return true;
            });

            const hasDirectInputs = document.querySelectorAll('input[type="tel"], input[type="email"], textarea').length > 0;
            const hasOnPageFormOrEmbed = hasRealForm || hasDirectInputs || (formIframeSrc !== null);

            const interactive = Array.from(document.querySelectorAll('a, button, input[type="submit"]')).map(el => ((el.innerText || el.value || '')).toLowerCase());
            const hasCta = interactive.some(t => formKeywords.some(kw => t.includes(kw)));
            const hasLeadCapture = (hasOnPageFormOrEmbed || hasCta);

            let resolvedFormUrl = null;
            for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                const href = (a.getAttribute('href') || '').toLowerCase();
                const text = (a.innerText || '').toLowerCase();
                if (formKeywords.some(kw => href.includes(kw) || text.includes(kw))) {
                    resolvedFormUrl = a.href;
                    break;
                }
            }

            return {
                has_lead_capture: hasLeadCapture ? 1 : 0,
                resolved_form_url: resolvedFormUrl
            };
        }""", EVAL_CFG)

        assert fp_res["has_lead_capture"] == 0, "platform-performance.html and free-ebook must not trigger has_lead_capture"
        assert fp_res["resolved_form_url"] is None, "free-ebook must not be matched as resolved_form_url"

        # True positive test: jotform embed and /book-now link
        tp_html = """
        <!DOCTYPE html>
        <html>
        <body>
          <iframe src="https://form.jotform.com/123456789"></iframe>
          <a href="/book-now">Book a Visit</a>
        </body>
        </html>
        """
        await page.set_content(tp_html)

        tp_res = await page.evaluate(r"""(cfg) => {
            const embedDomains = cfg.embed_domains || [];
            const formKeywords = cfg.form_keywords || [];

            let formIframeSrc = null;
            for (const ifr of Array.from(document.querySelectorAll('iframe'))) {
                const src = (ifr.src || '').toLowerCase();
                if (embedDomains.some(d => src.includes(d))) {
                    formIframeSrc = ifr.src;
                    break;
                }
            }

            const hasOnPageFormOrEmbed = (formIframeSrc !== null);
            const interactive = Array.from(document.querySelectorAll('a, button, input[type="submit"]')).map(el => ((el.innerText || el.value || '')).toLowerCase());
            const hasCta = interactive.some(t => formKeywords.some(kw => t.includes(kw)));
            const hasLeadCapture = (hasOnPageFormOrEmbed || hasCta);

            let resolvedFormUrl = null;
            for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                const href = (a.getAttribute('href') || '').toLowerCase();
                const text = (a.innerText || '').toLowerCase();
                if (formKeywords.some(kw => href.includes(kw) || text.includes(kw))) {
                    resolvedFormUrl = a.href;
                    break;
                }
            }

            return {
                has_lead_capture: hasLeadCapture ? 1 : 0,
                has_form_iframe: formIframeSrc !== null,
                resolved_form_url: resolvedFormUrl
            };
        }""", EVAL_CFG)

        assert tp_res["has_lead_capture"] == 1
        assert tp_res["has_form_iframe"] is True
        assert tp_res["resolved_form_url"] is not None and "book-now" in tp_res["resolved_form_url"]

        await browser.close()
