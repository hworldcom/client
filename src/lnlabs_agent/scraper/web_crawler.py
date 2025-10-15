# src/lnlabs_agent/scraper/web_crawler.py
from __future__ import annotations
import asyncio, json, os, random, time, subprocess
from typing import List, Tuple, Optional, Callable
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime
import re
from contextlib import asynccontextmanager
import pyautogui
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

class WebCrawler:
    def __init__(
        self,
        base_url: str = "https://www.linkedin.com/",
        cookie_file: str = "cookies.json",
        window_offset: int = 90,
        browser_exe: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
        artifacts_dir: Optional[str] = None,
    ):
        self.URL = base_url.rstrip('/') + '/'
        self.COOKIE_FILE = cookie_file
        self.WINDOW_OFFSET = window_offset
        self.browser_exe = browser_exe
        self.log = logger or (lambda s: None)
        self.artifacts_dir = Path(artifacts_dir or ".artifacts")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        # ...

    async def _shot(self, name: str) -> None:
        """Save a quick screenshot for debugging."""
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.artifacts_dir / f"{ts}-{name}.png"
            if self.page:
                await self.page.screenshot(path=str(path), full_page=False)
                self.log(f"[shot] {path}")
        except Exception as e:
            self.log(f"[shot] failed: {e}")

    @asynccontextmanager
    async def session(self, headless: bool = False):
        """
        Opens playwright → browser → context → page, yields the crawler with page ready,
        and *reliably* tears everything down with timeouts.
        """
        self.playwright = self.browser = self.context = self.page = None
        try:
            self.playwright = await async_playwright().start()
            launch_kwargs = {"headless": headless}
            if self.browser_exe:
                launch_kwargs["executable_path"] = self.browser_exe
            self.browser = await self.playwright.chromium.launch(**launch_kwargs)
            self.context = await self.browser.new_context()
            await self._load_cookies()
            self.page = await self.context.new_page()

            # Optional console piping for debugging
            self.page.on("console", lambda msg: self.log(f"[page.console] {msg.type} {msg.text}"))

            self.log("[session] started")
            yield self
        finally:
            # teardown with timeouts so we never hang
            async def _close_safely(coro, name: str, sec: float = 3.0):
                try:
                    await asyncio.wait_for(coro, timeout=sec)
                    self.log(f"[session] closed {name}")
                except Exception as e:
                    self.log(f"[session] close {name} skipped/failed: {e}")

            if self.page and not self.page.is_closed():
                try:
                    # don’t block here — closing context will close pages
                    pass
                except Exception:
                    pass
            if self.context:
                await _close_safely(self.context.close(), "context", 3.0)
            if self.browser:
                await _close_safely(self.browser.close(), "browser", 3.0)
            if self.playwright:
                try:
                    await asyncio.wait_for(self.playwright.stop(), timeout=2.0)
                except Exception:
                    self.log("[session] playwright.stop timeout/ignored")
            self.log("[session] ended")


    # -------- auth helpers --------
    async def login_if_needed(self, wait_ms: int = 30_000) -> bool:
        assert self.page
        self.log("[auth] checking login")
        await self.safe_login()
        await self.page.wait_for_timeout(2_000)
        await self._shot("after-safe-login")

        if await self.is_authwall() or "login" in self.page.url or "checkpoint" in self.page.url:
            self.log("[auth] not logged in → manual flow")
            await self.page.goto(self.URL + "login/")
            self.log("[auth] waiting for manual login…")
            await self.page.wait_for_timeout(wait_ms)  # user time
            await self._save_cookies()
            self.log("[auth] cookies saved")
            await self._shot("after-manual-login")
            return True

        self.log("[auth] already logged in (cookies worked)")
        return True


    async def safe_login(self, max_retries: int = 3) -> bool:
        assert self.page
        for attempt in range(max_retries):
            try:
                await self.page.goto(self.URL + "login", timeout=6_000)
                await self.page.wait_for_load_state("domcontentloaded")
                if any(k in self.page.url for k in ("linkedin.com/feed", "linkedin.com/in", "linkedin.com/login")):
                    return True
                print(f"[!] Unexpected URL: {self.page.url}, retrying…")
            except Exception as e:
                print(f"[!] Error loading {self.page.url}: {e}, retrying…")
            await asyncio.sleep(2 + attempt * 2)
        return False

    async def is_authwall(self) -> bool:
        assert self.page
        try:
            meta_tag = await self.page.locator('meta[name="pageKey"]').get_attribute('content')
            return bool(meta_tag and meta_tag.startswith("auth_wall"))
        except Exception:
            return False

    async def _load_cookies(self) -> None:
        assert self.context
        if os.path.exists(self.COOKIE_FILE):
            try:
                with open(self.COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                await self.context.add_cookies(cookies)
                print("Loaded cookies.")
            except Exception as e:
                print(f"Cookie load failed: {e}")

    async def _save_cookies(self) -> None:
        assert self.context
        try:
            cookies = await self.context.cookies()
            with open(self.COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f)
            print("Saved cookies.")
        except Exception as e:
            print(f"Cookie save failed: {e}")

    # -------- navigation primitives --------
    async def safe_goto(self, url: str, max_retries: int = 3) -> bool:
        assert self.page
        for attempt in range(max_retries):
            try:
                await self.page.goto(url, timeout=6_000)
                await self.page.wait_for_load_state("domcontentloaded")
                if "linkedin.com/feed" in self.page.url:
                    print("[!] Redirected to feed. Trying again.")
                    continue
                return True
            except Exception as e:
                print(f"[!] Error loading {url}: {e}, retrying…")
            await asyncio.sleep(2 + attempt * 2)
        return False

    async def wait_for_any(self, selectors: List[str], timeout: int = 15_000) -> str:
        assert self.page
        for sel in selectors:
            try:
                await self.page.wait_for_selector(sel, timeout=timeout)
                return sel
            except Exception:
                continue
        raise TimeoutError(f"None of selectors appeared: {selectors}")

    # -------- your search routines (kept) --------
    async def locate(self, sel: str, timeout: int = 10_000):
        assert self.page
        loc = self.page.locator(sel)
        await loc.wait_for(timeout=timeout)
        return loc

    async def locate_no_wait(self, sel: str):
        assert self.page
        return self.page.locator(sel)

    async def locate_all(self, sel: str, text: str | None = None):
        assert self.page
        delay = random.uniform(2.5, 4) * 1000
        print(f"Sleeping for {delay:.2f} ms…")
        await self.page.wait_for_timeout(delay)
        loc = self.page.locator(sel)
        return (await loc.filter(has_text=text).all()) if text else (await loc.all())

    async def locate_all_within(self, root, sel: str):
        return await root.locator(sel).all()

    async def click(self, loc) -> None:
        await loc.click()

    async def wait_to_appear(self, sel: str) -> None:
        assert self.page
        await self.page.wait_for_selector(sel, timeout=10_000)

    async def type(self, text: str, delay_ms: int = 150) -> None:
        assert self.page
        await self.page.keyboard.type(text, delay=delay_ms)

    async def press_enter(self) -> None:
        assert self.page
        await self.page.keyboard.press("Enter")

    async def start_company_flow(self, company: str):
        self.log(f"[flow] company={company}")
        await self._shot("before-search")
        names: list[str] = []
        urls: list[str] = []
        await self._extract_data_urls_names_company(company, names, urls)
        self.log(f"[flow] company done: {len(urls)} urls")
        await self._shot("after-company-flow")
        return names, urls

    # ====== pasted/minimally adapted from your functions ======
    async def _extract_data_urls_names_company(self, company: str, out_names: list[str], out_urls: list[str]):
        self.log("[step] locate search box")
        search_box = await self.locate(".search-global-typeahead input")
        await self._shot("search-box")
        await self.click(search_box)

        self.log(f"[step] typing query: {company}")
        await self.type(company)
        await self.press_enter()
        await self.page.wait_for_timeout(5_000)
        # after you submit the search query:
        await self._shot("after-enter")
        await self._click_companies_tab_simple()    # ← replaces the old ambiguous locator
        await self._shot("companies-tab")

        self.log("[step] wait results container")
        await self.wait_to_appear("div.search-results-container")
        await self._shot("results-container")

        self.log("[step] find first company link")
        ul_lists = await self.locate_all("ul[role='list']")
        all_items = []
        for ul in ul_lists:
            items = await self.locate_all_within(ul, "li")
            all_items.extend(items)

        a_tag = None
        for item in all_items:
            try:
                cand = item.locator("a[href*='/company/']").first
                href = await cand.get_attribute("href")
                if href and "/company/" in href:
                    a_tag = cand
                    break
            except:
                continue
        if not a_tag:
            await self._shot("no-company-link")
            raise RuntimeError("No valid company link found.")

        self.log("[step] open company page")
        await a_tag.click()
        await self.page.wait_for_load_state("domcontentloaded")
        await self._shot("company-opened")

        self.log("[step] open employees")
        employee_button = await self.locate("div.org-top-card-summary-info-list div.inline-block >> a:has(span:has-text('employees'))")
        await self._shot("employees-link")
        await self.click(employee_button)
        await self.page.wait_for_load_state("domcontentloaded")

        self.log("[step] filter 2nd-degree")

        # Prefer the toolbar radio (your DOM variant)
        try:
            await self._select_second_degree_toolbar_first()
        except Exception as e:
            self.log(f"[filters] toolbar path failed: {e} → trying filter panel")
            # Fallbacks for other UIs:
            await self._open_connections_filter()
            await self._select_second_degree()
            await self._apply_filters_if_present()

        await self.page.wait_for_timeout(800)

        self.log("[step] extract names/urls (paged)")
        await self._extract_data_names_urls(out_names, out_urls)
        self.log(f"[step] extracted {len(out_urls)} urls")

    async def _extract_data_names_urls(self, out_names: list[str], out_urls: list[str]):
        page = self.page; assert page
        page_i = 1
        while True:
            self.log(f"[page{page_i}] wait results")
            await page.wait_for_selector('[data-view-name="people-search-result"]', timeout=15_000)
            await self._shot(f"page-{page_i}-results")
            cards = await page.locator('[data-view-name="people-search-result"]').all()
            self.log(f"[page{page_i}] cards={len(cards)}")

            # ... (existing loop)
            for i, card in enumerate(cards):
                try:
                    # Prefer the explicit title link
                    title_link = card.locator('a[data-view-name="search-result-lockup-title"]').first
                    if await title_link.count() > 0:
                        name = (await title_link.inner_text()).strip()
                        href = await title_link.get_attribute("href")
                    else:
                        # Fallback: any link to /in/...
                        any_profile = card.locator('a[href*="linkedin.com/in/"]').first
                        href = await any_profile.get_attribute("href")
                        # Fallback name
                        name_p = card.locator('p >> a[href*="linkedin.com/in/"]').first
                        name = (await name_p.inner_text()).strip() if await name_p.count() > 0 else "(unknown)"

                    if href:
                        # ✅ correct order: name → out_names, href → out_urls
                        out_names.append(name)
                        out_urls.append(href)
                        self.log(f"[✓] {name} → {href}")
                except Exception as e:
                    self.log(f"[!] Error on card {i}: {e}")

            # Pagination (no networkidle here)
            clicked = await self._click_next_or_stop()
            if not clicked:
                break
            self.log(f"[page{page_i}] next → {page_i+1}")
            await page.wait_for_timeout(600)  # tiny settle, optional
            page_i += 1

    async def _open_connections_filter(self) -> None:
        """Open the 'Connections' filter panel, regardless of UI variant."""
        p = self.page
        assert p

        self.log("[filters] try open 'Connections' pill by role")
        # Variant A: a pill/tab button named "Connections"
        try:
            btn = p.get_by_role("button", name=lambda n: n and "connections" in n.lower())
            await btn.wait_for(timeout=3000)
            await btn.click()
            await self._shot("connections-pill-open")
            return
        except Exception:
            pass

        # ✅ Variant A2: data attribute used by some UIs
        try:
            pill = p.locator("[data-test-reusables-filters__filter-pill='CONNECTIONS']")
            await pill.wait_for(timeout=2000)
            await pill.click()
            await self._shot("connections-pill-dataattr")
            return
        except Exception:
            pass

        # Variant B: an input/menu button with aria-label
        try:
            btn = p.locator("button[aria-label*='Connections' i]")
            await btn.first.wait_for(timeout=3000)
            await btn.first.click()
            await self._shot("connections-aria-open")
            return
        except Exception:
            pass

        # Variant C: open "All filters", then ensure 'Connections' section is visible
        self.log("[filters] try via 'All filters'")
        try:
            allf = p.get_by_role("button", name=lambda n: n and "all filters" in n.lower())
            await allf.click()
            await self._shot("all-filters-open")
            await p.get_by_role("heading", name=lambda n: n and "connections" in n.lower()).wait_for(timeout=5000)
            await self._shot("all-filters-connections-visible")
            return
        except Exception as e:
            self.log(f"[filters] could not open connections: {e}")
            await self._shot("connections-open-failed")
            raise



    async def _select_second_degree(self) -> None:
        """Select '2nd' connections using multiple UI fallbacks, then apply if needed."""
        p = self.page
        assert p

        self.log("[filters] selecting 2nd-degree")

        # Fallback 1: ARIA radio by name
        try:
            radio = p.get_by_role("radio", name=lambda n: n and n.strip().startswith("2nd"))
            await radio.wait_for(timeout=2500)
            await radio.click()  # or .check() if available
            await self._shot("2nd-selected-radio")
            return
        except Exception:
            pass

        # Fallback 2: a simple button with aria-label='2nd'
        try:
            btn = p.locator("button[aria-label='2nd']")
            await btn.first.wait_for(timeout=2500)
            await btn.first.click()
            await self._shot("2nd-selected-button")
            return
        except Exception:
            pass

        # Fallback 3: label '2nd' tied to an <input> (your DOM sample)
        try:
            lab = p.get_by_label("2nd")
            await lab.wait_for(timeout=2500)
            await lab.click()
            await self._shot("2nd-selected-label")
            return
        except Exception:
            pass

        # Fallback 4: generic label text search inside filters panel
        try:
            lab = p.locator("label", has_text="2nd")
            await lab.first.wait_for(timeout=2500)
            await lab.first.click()
            await self._shot("2nd-selected-generic-label")
            return
        except Exception:
            pass

        await self._shot("2nd-not-found")
        raise TimeoutError("Could not find '2nd' option in Connections filter")


    async def _apply_filters_if_present(self) -> None:
        p = self.page
        assert p
        try:
            btn = p.get_by_role("button", name=lambda n: n and ("show results" in n.lower() or "apply" in n.lower()))
            await btn.wait_for(timeout=2000)
            await btn.click()
            await self._shot("filters-applied")
        except Exception:
            # Not all variants need an explicit Apply
            pass

    async def _select_second_degree_toolbar_first(self) -> None:
        """
        Your current UI: radios for 1st / 2nd / 3rd+ appear directly in a top toolbar.
        Click the '2nd' radio there.
        """
        p = self.page
        assert p
        self.log("[filters] try toolbar radios (1st/2nd/3rd+)")

        # Scope to the first filter toolbar (role="toolbar")
        toolbar = p.locator("div[role='toolbar']").first
        await toolbar.wait_for(timeout=5000)
        await self._shot("toolbar-present")

        # EITHER click <label>2nd</label> …
        try:
            lab = toolbar.get_by_text(re.compile(r"^\s*2nd\s*$"))
            await lab.first.scroll_into_view_if_needed()
            await lab.first.wait_for(timeout=2500)
            await lab.first.click()
            await self._shot("2nd-selected-toolbar-label")
            return
        except Exception:
            pass

        # … OR click radio by role/name
        try:
            radio = toolbar.get_by_role("radio", name=re.compile(r"^\s*2nd\s*$", re.I))
            await radio.first.scroll_into_view_if_needed()
            await radio.first.wait_for(timeout=2500)
            await radio.first.click()
            await self._shot("2nd-selected-toolbar-radio")
            return
        except Exception:
            pass

        # … OR final fallback: any label '2nd' within toolbar
        try:
            lab = toolbar.locator("label", has_text=re.compile(r"^\s*2nd\s*$", re.I))
            await lab.first.scroll_into_view_if_needed()
            await lab.first.wait_for(timeout=2500)
            await lab.first.click()
            await self._shot("2nd-selected-toolbar-generic")
            return
        except Exception:
            pass

        await self._shot("2nd-not-found-toolbar")
        raise TimeoutError("Toolbar radios present but could not click '2nd'")

    async def _click_companies_tab_simple(self) -> None:
        """
        Click the 'Companies' pill in the Search filters toolbar—no hydration tricks,
        no fallbacks—just find the pill in the toolbar you shared and click it.
        """
        p = self.page; assert p
        self.log("[companies] simple click in toolbar")

        # Scope to the toolbar you pasted
        toolbar = p.locator("section.scaffold-layout-toolbar nav[aria-label='Search filters']").first
        await toolbar.wait_for(timeout=10000)

        # Find the 'Companies' pill INSIDE the toolbar filter list
        companies = toolbar.locator(
            "ul.search-reusables__filter-list li button"
        ).filter(has_text=re.compile(r"^\s*Companies\s*$", re.I)).first

        # Wait until that button is visible/clickable, then click
        await companies.wait_for(state="visible", timeout=10000)
        await companies.scroll_into_view_if_needed()
        await companies.click()

        # (Optional) tiny settle
        await p.wait_for_timeout(500)


    async def locate_within_scroll(self, text, MAX_SCROLLS=5, DELAY=1):

        for i in range(MAX_SCROLLS):
            # Try to locate the 'Next' button
            next_button = self.page.locator(text)
            #self.random_mouse_movement()

            if await next_button.is_visible():
                # Found the button
                print(f"[✓] Found {text} after {i+1} scrolls.")
                return next_button

            await self.page.mouse.wheel(0, 1000)  # Scroll down
            await self.page.wait_for_timeout(DELAY * 1000)


    # inside WebCrawler

    async def _find_next_button(self):
        p = self.page

        # A. Preferred: the data-testid variant you pasted
        btn = p.locator("button[data-testid='pagination-controls-next-button-visible']").first
        if await btn.count():
            return btn

        # B. Fallback: a button that *contains* “Next” (works on your DOM too)
        # (kept as a fallback because of localization)
        btn2 = p.get_by_role("button", name="Next").first
        if await btn2.count():
            return btn2

        return None


    async def _click_next_or_stop(self) -> bool:
        """
        Click 'Next' if present & enabled. Return True if page is moving, False if last page.
        Uses UI-change waits instead of networkidle.
        """
        p = self.page

        # Hidden → last page
        if await p.locator("button[data-testid='pagination-controls-next-button-hidden']").count():
            self.log("[page] next is hidden → last page")
            return False

        btn = await self._find_next_button()
        if not btn:
            self.log("[page] next not found → stop")
            return False

        # Snapshot state before click
        prev_page = await self._current_page_label()
        prev_key  = await self._first_result_key()

        try:
            await btn.scroll_into_view_if_needed()
            await btn.wait_for(state="visible", timeout=3000)
            # If LinkedIn toggles disabled, respect it
            try:
                if await btn.get_attribute("disabled") is not None:
                    self.log("[page] next disabled → stop")
                    return False
            except Exception:
                pass

            await btn.click()

            # Wait for either page badge or first-result to change (no networkidle)
            try:
                await p.wait_for_function(
                    """
                    ([prevPage, prevKey]) => {
                      const pageEl = document.querySelector('button[aria-current="true"][aria-label^="Page"] span');
                      const curPage = pageEl ? pageEl.textContent.trim() : null;
    
                      const card = document.querySelector('[data-view-name="people-search-result'], [data-view-name="search-result"]');
                      let curKey = null;
                      if (card) {
                        const link = card.querySelector('a[href*="linkedin.com/in/"]');
                        curKey = link?.getAttribute('href') || (card.textContent || '').trim().slice(0, 200);
                      }
    
                      const pageChanged = !!curPage && curPage !== prevPage;
                      const keyChanged  = !!curKey  && curKey  !== prevKey;
                      return pageChanged || keyChanged;
                    }
                    """,
                    arg=[prev_page, prev_key],
                    timeout=12_000,
                )
            except Exception:
                # soft settle—don’t blow up the loop
                await p.wait_for_timeout(800)

            return True

        except Exception as e:
            self.log(f"[page] next click failed (stop): {e}")
            return False


    async def _current_page_label(self) -> str | None:
        # e.g. aria-current="true" and aria-label="Page 3"
        el = self.page.locator('button[aria-current="true"][aria-label^="Page"] span').first
        if await el.count():
            try:
                return (await el.text_content() or "").strip()
            except Exception:
                return None
        return None

    async def _first_result_key(self) -> str | None:
        """
        Something stable-ish to detect that results changed.
        Tries: first profile card's href or text.
        """
        card = self.page.locator('[data-view-name="people-search-result"], [data-view-name="search-result"]').first
        if await card.count():
            try:
                link = card.locator('a[href*="linkedin.com/in/"]').first
                if await link.count():
                    href = await link.get_attribute("href")
                    if href:
                        return href
                # fallback to text blob
                txt = await card.text_content()
                return (txt or "").strip()[:200]
            except Exception:
                return None
        return None
