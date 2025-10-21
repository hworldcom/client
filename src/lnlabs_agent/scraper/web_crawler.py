# src/lnlabs_agent/scraper/web_crawler.py
from __future__ import annotations

import os, json, re, random, asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Callable, List
from datetime import datetime

from playwright.async_api import async_playwright
from platformdirs import user_log_dir, user_config_dir

from lnlabs_agent.secure_cookies import SecureCookieMixin


class WebCrawler(SecureCookieMixin):
    def __init__(
        self,
        base_url: str = "https://www.linkedin.com/",
        cookie_file: str = "cookies.json",
        window_offset: int = 90,
        browser_exe: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
        artifacts_dir: Optional[str] = None,
        audit_log_path: Optional[str] = None,
        verbose_network: bool = True,
    ):
        self.URL = base_url.rstrip("/") + "/"
        self.WINDOW_OFFSET = window_offset
        self.browser_exe = browser_exe
        self.log = logger or (lambda s: None)
        self.verbose_network = verbose_network

        # --- cookie path (absolute) ---
        app_conf = Path(user_config_dir("LNLabsAgent", "LNLabs"))
        app_conf.mkdir(parents=True, exist_ok=True)
        self.COOKIE_FILE = (
            str(Path(cookie_file).expanduser().resolve())
            if os.path.isabs(cookie_file)
            else str((app_conf / cookie_file).resolve())
        )

        # screenshots / small artifacts
        self.artifacts_dir = Path(artifacts_dir or (app_conf / "artifacts"))
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        # file audit log (blocked/failed requests, filtered console)
        default_dir = Path(user_log_dir("LNLabsAgent", "LNLabs"))
        default_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_path = Path(audit_log_path or (default_dir / "network.log"))
        self._audit_max_bytes = 5 * 1024 * 1024  # 5MB roll

        # Only surface console logs from these origins (everything else goes to audit file)
        self._console_allow_origin = re.compile(r"https?://([^/]*\.)?linkedin\.com/", re.I)

        # Suppress these noisy console messages in the UI (but still record to audit)
        self._suppress_console_re = re.compile(
            r"(?:"
            r"ERR_BLOCKED_BY_CLIENT|ERR_FAILED|MEDIA_ERR_SRC_NOT_SUPPORTED|Failed to load resource|"
            r"VIDEOJS|%c\s|console\.groupEnd|EvalError"
            r")",
            re.I,
        )

        # runtime objects
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def _abs(self, p: os.PathLike | str) -> Path:
        return Path(p).expanduser().resolve()

    # ---------- screenshots ----------
    async def _shot(self, name: str) -> None:
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.artifacts_dir / f"{ts}-{name}.png"
            if self.page:
                await self.page.screenshot(path=str(path), full_page=False)
                self.log(f"[shot] {path}")
        except Exception as e:
            self.log(f"[shot] failed: {e}")

    # ---------- public session helpers ----------
    @asynccontextmanager
    async def session(self, headless: bool = False):
        """Use as: `async with crawler.session(): ...`"""
        await self._prepare_context(headless=headless)
        try:
            yield self
        finally:
            await self._teardown_context()

    async def __aenter__(self):
        await self._prepare_context(headless=False)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._teardown_context()

    # ---------- audit (file-only) ----------
    def _audit_write(self, line: str) -> None:
        try:
            path = self._abs(self.audit_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > self._audit_max_bytes:
                b = path.with_suffix(".log.1")
                try: b.unlink(missing_ok=True)
                except Exception: pass
                try: path.rename(b)
                except Exception: pass
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            path.open("a", encoding="utf-8").write(f"{ts} {line}\n")
        except Exception:
            pass

    def audit(self, line: str) -> None:
        if self.verbose_network:
            self._audit_write(line)

    # ---------- attach page listeners ----------
    def _attach_page_logging(self) -> None:
        assert self.page

        # --- requestfailed → always audit ---
        def _on_request_failed(req):
            try:
                failure = getattr(req, "failure", None)
                if failure:
                    err = (
                        getattr(failure, "error_text", "")
                        or getattr(failure, "errorText", "")
                        or str(failure)
                    )
                else:
                    err = ""
                # best-effort referer
                headers = {}
                try:
                    headers = req.headers or {}
                except Exception:
                    pass
                ref = headers.get("referer") or headers.get("referrer") or "-"
                self.audit(f"[failed] {req.method} {req.url} :: {err} ref={ref}")
            except Exception as e:
                self.audit(f"[failed:handler-error] {e!r}")

        # --- console → UI only for LinkedIn origin AND not noisy; always audit ---
        def _on_console(msg):
            txt = (msg.text or "").strip()
            loc = {}
            try:
                loc = msg.location or {}
            except Exception:
                pass
            url = (loc.get("url") or "").strip()
            line = loc.get("lineNumber")
            col  = loc.get("columnNumber")

            # Always write full console lines to audit with origin/position
            self.audit(f"[console] {msg.type} {url}:{line}:{col} :: {txt}")

            # 1) If console did not originate from LinkedIn → hide from UI
            if url and not self._console_allow_origin.search(url):
                return

            # 2) If text matches known noisy patterns → hide from UI
            if self._suppress_console_re.search(txt):
                return

            # Otherwise show to UI
            self.log(f"[page.console] {msg.type} {txt}")

        self.page.on("requestfailed", _on_request_failed)
        self.page.on("console", _on_console)
        self.log("[session] browser ready (handlers attached)")


    # ---------- playwright context ----------
    async def _prepare_context(self, headless: bool):
        # announce paths up front (visible in GUI log)
        self.log(f"[paths] cookies file: {self._abs(self.COOKIE_FILE)}")
        self.log(f"[paths] artifacts dir: {self._abs(self.artifacts_dir)}")
        self.log(f"[paths] network log: {self._abs(self.audit_log_path)}")

        self.playwright = await async_playwright().start()
        kw = {"headless": headless}
        if self.browser_exe:
            kw["executable_path"] = self.browser_exe
        self.browser = await self.playwright.chromium.launch(**kw)
        self.context = await self.browser.new_context()

        # block noisy trackers; audit anything we block
        BLOCK = [
            "doubleclick.net",
            "googletagmanager.com",
            "google-analytics.com",
            "px.ads.linkedin.com",
            "ads.linkedin.com",
        ]

        async def router(route, request):
            if any(p in request.url for p in BLOCK):
                self.audit(f"[blocked] {request.resource_type} {request.url}")
                await route.abort()
            else:
                await route.continue_()

        await self.context.route("**/*", router)

        # cookies (secure mixin)
        await self._load_cookies()

        self.page = await self.context.new_page()
        self._attach_page_logging()

    async def _teardown_context(self):
        try:
            if self.context:
                # secure save via mixin (with perms + path logging)
                try:
                    await self._save_cookies()
                except Exception as e:
                    self.log(f"[cookies] save failed: {e}")
                await self.context.close()
        finally:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None
        self.log("[session] closed")

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
                if any(
                    k in self.page.url
                    for k in ("linkedin.com/feed", "linkedin.com/in", "linkedin.com/login")
                ):
                    return True
                print(f"[!] Unexpected URL: {self.page.url}, retrying…")
            except Exception as e:
                print(f"[!] Error loading {self.page.url}: {e}, retrying…")
            await asyncio.sleep(2 + attempt * 2)
        return False

    async def is_authwall(self) -> bool:
        assert self.page
        try:
            meta_tag = await self.page.locator('meta[name="pageKey"]').get_attribute("content")
            return bool(meta_tag and meta_tag.startswith("auth_wall"))
        except Exception:
            return False

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

    # -------- basic helpers --------
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

    # -------- main flows --------
    async def start_company_flow(self, company: str):
        self.log(f"[flow] company={company}")
        await self._shot("before-search")
        names: list[str] = []
        urls: list[str] = []
        await self._extract_data_urls_names_company(company, names, urls)
        self.log(f"[flow] company done: {len(urls)} urls")
        await self._shot("after-company-flow")
        return names, urls

    async def _extract_data_urls_names_company(self, company: str, out_names: list[str], out_urls: list[str]):

        self.log("[step] go home before search")
        await self._go_home()

        self.log("[step] locate search box")
        search_box = await self._find_global_search_input()
        await self._shot("search-box")
        # Focus & clear any previous query
        try:
            await search_box.click()
        except Exception:
            # if covered by overlay, try to ensure it’s open and click again
            await self._ensure_search_box_open()
            await search_box.click()

        # Clear text robustly (fill is best; fallback to Ctrl/Meta+A + Backspace)
        try:
            await search_box.fill("")
        except Exception:
            try:
                # macOS uses Meta; Windows/Linux use Control—send both safely
                await self.page.keyboard.press("Meta+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass

        self.log(f"[step] typing query: {company}")
        await search_box.type(company, delay=50)
        await self.press_enter()
        await self.page.wait_for_timeout(1500)
        await self._shot("after-enter")

        await self._click_companies_tab_simple()
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
            except Exception:
                continue
        if not a_tag:
            await self._shot("no-company-link")
            raise RuntimeError("No valid company link found.")

        self.log("[step] open company page")
        await a_tag.click()
        await self.page.wait_for_load_state("domcontentloaded")
        await self._shot("company-opened")

        self.log("[step] open employees")
        employee_button = await self.locate(
            "div.org-top-card-summary-info-list div.inline-block >> a:has(span:has-text('employees'))"
        )
        await self._shot("employees-link")
        await self.click(employee_button)
        await self.page.wait_for_load_state("domcontentloaded")

        self.log("[step] filter 2nd-degree]")
        try:
            await self._select_second_degree_toolbar_first()
        except Exception as e:
            self.log(f"[filters] toolbar path failed: {e} → trying filter panel")
            await self._open_connections_filter()
            await self._select_second_degree()
            await self._apply_filters_if_present()

        await self.page.wait_for_timeout(800)

        self.log("[step] extract names/urls (paged)")
        await self._extract_data_names_urls(out_names, out_urls)
        self.log(f"[step] extracted {len(out_urls)} urls")

    async def _go_home(self) -> None:
        """
        Navigate to the LinkedIn feed to normalize the header/search UI
        before starting a new search.
        """
        p = self.page; assert p
        # Prefer the canonical feed URL (keeps existing query params harmless)
        target = self.URL + "feed/?doFeedRefresh=true&nis=true"

        # Use your robust loader
        ok = await self.safe_goto(target, max_retries=3)
        if not ok:
            # Fall back to plain /feed/ if needed
            await self.safe_goto(self.URL + "feed/", max_retries=2)

        # Wait for either classic or new header to be present
        await p.wait_for_selector(
            "header#global-nav, div[role='search'] input[data-testid='typeahead-input']",
            timeout=10_000
        )

        # Expand collapsed search if present and take a shot for debugging
        try:
            await self._ensure_search_box_open()
        finally:
            await self._shot("home-loaded")

    async def _extract_data_names_urls(self, out_names: list[str], out_urls: list[str]):
        page = self.page
        assert page
        page_i = 1
        while True:
            self.log(f"[page{page_i}] wait results")
            await page.wait_for_selector('[data-view-name="people-search-result"]', timeout=15_000)
            await self._shot(f"page-{page_i}-results")
            cards = await page.locator('[data-view-name="people-search-result"]').all()
            self.log(f"[page{page_i}] cards={len(cards)}")

            for i, card in enumerate(cards):
                try:
                    title_link = card.locator('a[data-view-name="search-result-lockup-title"]').first
                    if await title_link.count() > 0:
                        name = (await title_link.inner_text()).strip()
                        href = await title_link.get_attribute("href")
                    else:
                        any_profile = card.locator('a[href*="linkedin.com/in/"]').first
                        href = await any_profile.get_attribute("href")
                        name_p = card.locator('p >> a[href*="linkedin.com/in/"]').first
                        name = (await name_p.inner_text()).strip() if await name_p.count() > 0 else "(unknown)"

                    if href:
                        out_names.append(name)
                        out_urls.append(href)
                        self.log(f"[✓] {name} → {href}")
                except Exception as e:
                    self.log(f"[!] Error on card {i}: {e}")

            clicked = await self._click_next_or_stop()
            if not clicked:
                break
            self.log(f"[page{page_i}] next → {page_i + 1}")
            await page.wait_for_timeout(600)
            page_i += 1

    async def _ensure_search_box_open(self) -> None:
        """If the search box is collapsed, expand it."""
        p = self.page; assert p
        try:
            # Classic header: collapsed button
            btn = p.locator("button.search-global-typeahead__collapsed-search-button").first
            if await btn.count() and not await p.locator("#global-nav-search input").first.is_visible():
                await btn.click()
                await p.wait_for_timeout(200)
                return
        except Exception:
            pass
        # New UI doesn't always collapse; nothing to do.

    async def _find_global_search_input(self):
        """
        Return a locator for the search input across both nav variants.
        Prefers *visible* inputs.
        """
        p = self.page; assert p

        # Candidate selectors, ordered by likelihood.
        candidates = [
            # Home / classic header
            "#global-nav-search input.search-global-typeahead__input",
            "header#global-nav input.search-global-typeahead__input",
            "header#global-nav input[role='combobox'][aria-autocomplete='list']",
            "input[data-view-name='search-global-typeahead-input']",

            # Results page / new header
            "div[role='search'] input[data-testid='typeahead-input']",
            "input[data-testid='typeahead-input']",
            "div[role='search'] input[aria-autocomplete='list']",

            # Fallback generic
            "input[placeholder='Search']",
            "input[aria-label='Search']",
        ]

        # Try to expand the box if it’s collapsed
        await self._ensure_search_box_open()

        # Return the first visible candidate
        for sel in candidates:
            loc = p.locator(sel).first
            try:
                if await loc.count():
                    # Wait briefly for visibility if it's likely the right thing
                    try:
                        await loc.wait_for(state="visible", timeout=800)
                        return loc
                    except Exception:
                        # If not visible yet, still consider if it *becomes* visible after small delay
                        await p.wait_for_timeout(150)
                        if await loc.is_visible():
                            return loc
            except Exception:
                continue

        # Last resort: press "/" which focuses search on some UIs, then re-scan quickly
        try:
            await p.keyboard.press("/")
            await p.wait_for_timeout(150)
            for sel in candidates:
                loc = p.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return loc
        except Exception:
            pass

        raise TimeoutError("Could not find a visible global search input in either header variant.")


    async def _open_connections_filter(self) -> None:
        p = self.page
        assert p

        self.log("[filters] try open 'Connections' pill by role")
        try:
            btn = p.get_by_role("button", name=lambda n: n and "connections" in n.lower())
            await btn.wait_for(timeout=3000)
            await btn.click()
            await self._shot("connections-pill-open")
            return
        except Exception:
            pass

        try:
            pill = p.locator("[data-test-reusables-filters__filter-pill='CONNECTIONS']")
            await pill.wait_for(timeout=2000)
            await pill.click()
            await self._shot("connections-pill-dataattr")
            return
        except Exception:
            pass

        try:
            btn = p.locator("button[aria-label*='Connections' i]")
            await btn.first.wait_for(timeout=3000)
            await btn.first.click()
            await self._shot("connections-aria-open")
            return
        except Exception:
            pass

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
        p = self.page
        assert p

        self.log("[filters] selecting 2nd-degree")

        try:
            radio = p.get_by_role("radio", name=lambda n: n and n.strip().startswith("2nd"))
            await radio.wait_for(timeout=2500)
            await radio.click()
            await self._shot("2nd-selected-radio")
            return
        except Exception:
            pass

        try:
            btn = p.locator("button[aria-label='2nd']")
            await btn.first.wait_for(timeout=2500)
            await btn.first.click()
            await self._shot("2nd-selected-button")
            return
        except Exception:
            pass

        try:
            lab = p.get_by_label("2nd")
            await lab.wait_for(timeout=2500)
            await lab.click()
            await self._shot("2nd-selected-label")
            return
        except Exception:
            pass

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
            btn = p.get_by_role(
                "button", name=lambda n: n and ("show results" in n.lower() or "apply" in n.lower())
            )
            await btn.wait_for(timeout=2000)
            await btn.click()
            await self._shot("filters-applied")
        except Exception:
            pass

    async def _select_second_degree_toolbar_first(self) -> None:
        p = self.page
        assert p
        self.log("[filters] try toolbar radios (1st/2nd/3rd+)")

        toolbar = p.locator("div[role='toolbar']").first
        await toolbar.wait_for(timeout=5000)
        await self._shot("toolbar-present")

        try:
            lab = toolbar.get_by_text(re.compile(r"^\s*2nd\s*$"))
            await lab.first.scroll_into_view_if_needed()
            await lab.first.wait_for(timeout=2500)
            await lab.first.click()
            await self._shot("2nd-selected-toolbar-label")
            return
        except Exception:
            pass

        try:
            radio = toolbar.get_by_role("radio", name=re.compile(r"^\s*2nd\s*$", re.I))
            await radio.first.scroll_into_view_if_needed()
            await radio.first.wait_for(timeout=2500)
            await radio.first.click()
            await self._shot("2nd-selected-toolbar-radio")
            return
        except Exception:
            pass

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
        p = self.page
        assert p
        self.log("[companies] simple click in toolbar")

        toolbar = p.locator("section.scaffold-layout-toolbar nav[aria-label='Search filters']").first
        await toolbar.wait_for(timeout=10000)

        companies = (
            toolbar.locator("ul.search-reusables__filter-list li button")
            .filter(has_text=re.compile(r"^\s*Companies\s*$", re.I))
            .first
        )

        await companies.wait_for(state="visible", timeout=10000)
        await companies.scroll_into_view_if_needed()
        await companies.click()
        await p.wait_for_timeout(500)

    async def locate_within_scroll(self, text, MAX_SCROLLS=5, DELAY=1):
        for i in range(MAX_SCROLLS):
            next_button = self.page.locator(text)
            if await next_button.is_visible():
                print(f"[✓] Found {text} after {i+1} scrolls.")
                return next_button
            await self.page.mouse.wheel(0, 1000)
            await self.page.wait_for_timeout(DELAY * 1000)

    async def _find_next_button(self):
        p = self.page
        btn = p.locator("button[data-testid='pagination-controls-next-button-visible']").first
        if await btn.count():
            return btn
        btn2 = p.get_by_role("button", name="Next").first
        if await btn2.count():
            return btn2
        return None

    async def _click_next_or_stop(self) -> bool:
        p = self.page

        if await p.locator("button[data-testid='pagination-controls-next-button-hidden']").count():
            self.log("[page] next is hidden → last page")
            return False

        btn = await self._find_next_button()
        if not btn:
            self.log("[page] next not found → stop")
            return False

        prev_page = await self._current_page_label()
        prev_key = await self._first_result_key()

        try:
            await btn.scroll_into_view_if_needed()
            await btn.wait_for(state="visible", timeout=3000)
            try:
                if await btn.get_attribute("disabled") is not None:
                    self.log("[page] next disabled → stop")
                    return False
            except Exception:
                pass

            await btn.click()

            try:
                await p.wait_for_function(
                    """
                    ([prevPage, prevKey]) => {
                      const pageEl = document.querySelector('button[aria-current="true"][aria-label^="Page"] span');
                      const curPage = pageEl ? pageEl.textContent.trim() : null;

                      const card = document.querySelector('[data-view-name="people-search-result"], [data-view-name="search-result"]');
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
                await p.wait_for_timeout(800)

            return True

        except Exception as e:
            self.log(f"[page] next click failed (stop): {e}")
            return False

    async def _current_page_label(self) -> str | None:
        el = self.page.locator('button[aria-current="true"][aria-label^="Page"] span').first
        if await el.count():
            try:
                return (await el.text_content() or "").strip()
            except Exception:
                return None
        return None

    async def _first_result_key(self) -> str | None:
        card = self.page.locator(
            '[data-view-name="people-search-result"], [data-view-name="search-result"]'
        ).first
        if await card.count():
            try:
                link = card.locator('a[href*="linkedin.com/in/"]').first
                if await link.count():
                    href = await link.get_attribute("href")
                    if href:
                        return href
                txt = await card.text_content()
                return (txt or "").strip()[:200]
            except Exception:
                return None
        return None
