# src/lnlabs_agent/scraper/web_crawler.py
from __future__ import annotations

import os, json, re, random, asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Callable, List
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from platformdirs import user_log_dir, user_config_dir

from lnlabs_agent.secure_cookies import SecureCookieMixin

SECOND_RX = re.compile(r"^\s*2nd(?:\s*degree)?\b", re.I)

def _ms(min_s: float = 0.15, max_s: float = 0.35) -> int:
    """Small human-like delay in ms."""
    return int(random.uniform(min_s, max_s) * 1000)


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
        # Optional context tuning
        user_agent: Optional[str] = None,
        locale: str = "en-US",
        viewport: tuple[int, int] | None = (1440, 900),
    ):
        self.URL = base_url.rstrip("/") + "/"
        self.WINDOW_OFFSET = window_offset
        self.browser_exe = browser_exe
        self.log = logger or (lambda s: None)
        self.verbose_network = verbose_network
        self.user_agent = user_agent
        self.locale = locale
        self.viewport = viewport

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

        # --- results selectors (support multiple UI variants) ---
        self.RESULT_CARD_SELECTORS = [
            '[data-view-name="people-search-result"]',
            '[data-view-name="search-result"]',
            '[data-view-name="search-entity-result-universal-template"]',
            '[data-view-name^="search-entity-result-"]',
            '[data-chameleon-result-urn]',
            # extra catches
            "li.reusable-search__result-container",
            "div.search-result__wrapper",
        ]

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
        if not line:
            return
        try:
            path = self._abs(self.audit_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > self._audit_max_bytes:
                b = path.with_suffix(".log.1")
                try:
                    b.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    path.replace(b)
                except Exception:
                    pass
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{ts} {line}\n")
        except Exception:
            # never raise from audit
            pass

    def audit(self, line: str) -> None:
        if self.verbose_network:
            self._audit_write(line)

    # ---------- attach page listeners ----------
    def _attach_page_logging(self) -> None:
        assert self.page

        def _on_request_failed(req):
            try:
                err = ""
                try:
                    failure = getattr(req, "failure", None)
                    if failure:
                        err = getattr(failure, "error_text", "") or getattr(failure, "errorText", "") or str(failure)
                except Exception:
                    pass
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

            self.audit(f"[console] {msg.type} {url}:{line}:{col} :: {txt}")

            if url and not self._console_allow_origin.search(url):
                return
            if self._suppress_console_re.search(txt):
                return
            self.log(f"[page.console] {msg.type} {txt}")

        self.page.on("requestfailed", _on_request_failed)
        self.page.on("console", _on_console)
        self.log("[session] browser ready (handlers attached)")

    # ---------- playwright context ----------
    async def _prepare_context(self, headless: bool):
        self.log(f"[paths] cookies file: {self._abs(self.COOKIE_FILE)}")
        self.log(f"[paths] artifacts dir: {self._abs(self.artifacts_dir)}")
        self.log(f"[paths] network log: {self._abs(self.audit_log_path)}")

        self.playwright = await async_playwright().start()
        launch_kw = {"headless": headless}
        if self.browser_exe:
            launch_kw["executable_path"] = self.browser_exe

        self.browser = await self.playwright.chromium.launch(**launch_kw)

        context_kw = {
            "locale": self.locale,
            "timezone_id": "UTC",
        }
        if self.user_agent:
            context_kw["user_agent"] = self.user_agent
        if self.viewport:
            w, h = self.viewport
            context_kw["viewport"] = {"width": w, "height": h}

        self.context = await self.browser.new_context(**context_kw)

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
                try:
                    await self._save_cookies()
                except Exception as e:
                    self.log(f"[cookies] save failed: {e}")
                try:
                    await self.context.close()
                except Exception:
                    pass
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    await self.playwright.stop()
            except Exception:
                pass
            self.page = self.context = self.browser = self.playwright = None
            self.log("[session] closed")

    # -------- small helpers --------
    async def wait_visible(self, locator, timeout: int = 10_000):
        await locator.wait_for(state="visible", timeout=timeout)
        return locator

    async def click_with_retry(self, locator, attempts: int = 3, delay_ms: int = 250):
        last_err = None
        for i in range(attempts):
            try:
                await locator.scroll_into_view_if_needed()
                await asyncio.sleep(_ms(0.05, 0.12) / 1000)
                await locator.click()
                return True
            except Exception as e:
                last_err = e
                await self.page.wait_for_timeout(delay_ms + i * 150)
        if last_err:
            self.audit(f"[click:fail] {last_err}")
        return False

    async def fill_with_retry(self, locator, text: str, attempts: int = 3):
        for i in range(attempts):
            try:
                await locator.fill("")
                await asyncio.sleep(_ms(0.05, 0.12) / 1000)
                await locator.type(text, delay=50)
                return True
            except Exception:
                await self.page.wait_for_timeout(200 + i * 120)
        # Fallback: select-all + backspace
        try:
            await self.page.keyboard.press("Meta+A")
            await self.page.keyboard.press("Backspace")
            await locator.type(text, delay=50)
            return True
        except Exception:
            return False

    async def wait_network_quiet(self, timeout_ms: int = 5_000):
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            await self.page.wait_for_timeout(600)

    # -------- auth helpers --------
    async def login_if_needed(self, wait_ms: int = 30_000) -> bool:
        assert self.page
        self.log("[auth] checking login")
        await self.safe_login()
        await self.page.wait_for_timeout(800)
        await self._shot("after-safe-login")

        url = (self.page.url or "").lower()
        if await self.is_authwall() or "login" in url or "checkpoint" in url:
            self.log("[auth] not logged in → manual flow")
            await self.page.goto(self.URL + "login/")
            self.log("[auth] waiting for manual login…")
            await self.page.wait_for_timeout(wait_ms)
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
                await self.page.goto(self.URL + "login", timeout=8_000)
                await self.page.wait_for_load_state("domcontentloaded")
                u = self.page.url.lower()
                if any(k in u for k in ("linkedin.com/feed", "linkedin.com/in", "linkedin.com/login")):
                    return True
                self.log(f"[auth] unexpected URL after login nav: {u} (attempt {attempt+1})")
            except Exception as e:
                self.log(f"[auth] error loading login (attempt {attempt+1}): {e}")
            await asyncio.sleep(1.0 + attempt * 1.5)
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
                await self.page.goto(url, timeout=10_000)
                await self.page.wait_for_load_state("domcontentloaded")
                if "linkedin.com/feed" in (self.page.url or "") and "feed" not in url:
                    self.log("[nav] redirected to feed; retrying …")
                    await asyncio.sleep(0.6 + attempt * 0.4)
                    continue
                return True
            except Exception as e:
                self.log(f"[nav] error loading {url} (attempt {attempt+1}): {e}")
            await asyncio.sleep(0.8 + attempt * 0.6)
        return False

    async def wait_for_any(self, selectors: List[str], timeout: int = 15_000) -> str:
        assert self.page
        end = self.page._impl_obj._loop.time() + (timeout / 1000)
        for sel in selectors:
            try:
                remaining = max(0, int((end - self.page._impl_obj._loop.time()) * 1000))
                if remaining == 0:
                    break
                await self.page.wait_for_selector(sel, timeout=remaining)
                return sel
            except Exception:
                continue
        raise TimeoutError(f"None of selectors appeared: {selectors}")

    # -------- basic helpers --------
    async def locate(self, sel: str, timeout: int = 10_000):
        assert self.page
        loc = self.page.locator(sel).first
        await loc.wait_for(timeout=timeout)
        return loc

    async def locate_no_wait(self, sel: str):
        assert self.page
        return self.page.locator(sel)

    async def locate_all(self, sel: str, text: str | None = None):
        assert self.page
        delay = int(random.uniform(2.5, 4.0) * 1000)
        self.log(f"[sleep] {delay}ms before locate_all('{sel}')")
        await self.page.wait_for_timeout(delay)
        loc = self.page.locator(sel)
        return (await loc.filter(has_text=text).all()) if text else (await loc.all())

    async def locate_all_within(self, root, sel: str):
        return await root.locator(sel).all()

    async def click(self, loc) -> None:
        await self.click_with_retry(loc)

    async def wait_to_appear(self, sel: str) -> None:
        assert self.page
        await self.page.wait_for_selector(sel, timeout=10_000)

    async def type(self, text: str, delay_ms: int = 150) -> None:
        assert self.page
        await self.page.keyboard.type(text, delay=delay_ms)

    async def press_enter(self) -> None:
        assert self.page
        await self.page.keyboard.press("Enter")

    # ---------- results helpers ----------
    async def _wait_for_results(self, timeout_ms: int = 20_000) -> None:
        p = self.page; assert p

        # Fast path: any visible selector quickly
        for sel in self.RESULT_CARD_SELECTORS:
            try:
                await p.wait_for_selector(f"{sel} >> visible=true", timeout=3_000)
                return
            except Exception:
                pass

        # Slow path: any matching element in the DOM
        await p.wait_for_function(
            """(sels) => sels.some(s => document.querySelector(s))""",
            arg=self.RESULT_CARD_SELECTORS,
            timeout=timeout_ms,
        )
        await p.wait_for_timeout(400)

    def _result_cards(self):
        p = self.page; assert p
        combined = ", ".join(self.RESULT_CARD_SELECTORS)
        container = p.locator("div.search-results-container").first
        return (container.locator(combined) if container else p.locator(combined))

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

        try:
            await search_box.click()
        except Exception:
            await self._ensure_search_box_open()
            await search_box.click()

        await self.fill_with_retry(search_box, company)

        self.log(f"[step] typing query: {company}")
        await self.press_enter()
        await self.page.wait_for_timeout(1200)
        await self._shot("after-enter")

        await self._click_companies_tab()
        await self._shot("companies-tab")

        self.log("[step] wait results container")
        await self.wait_to_appear("div.search-results-container")
        await self._wait_for_results()
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
        await self.click(a_tag)
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
        await self._wait_filters_toolbar_hydrated(timeout_ms=15_000)
        try:
            await self._select_second_degree_toolbar_first()
        except Exception as e:
            self.log(f"[filters] toolbar path failed: {e} → trying filter panel")
            await self._open_connections_filter()
            await self._select_second_degree()
            await self._apply_filters_if_present()

        await self.wait_network_quiet()

        self.log("[step] extract names/urls (paged)")
        await self._extract_data_names_urls(out_names, out_urls)
        self.log(f"[step] extracted {len(out_urls)} urls")

    async def _click_companies_tab(self) -> None:
        p = self.page
        assert p
        self.log("[companies] click 'Companies' pill (resilient)")

        try:
            await self.wait_for_any(
                [
                    "nav[aria-label='Search filters']",
                    "#search-reusables__filters-bar",
                    "ul.search-reusables__filter-list",
                ],
                timeout=12_000,
            )
        except Exception:
            await p.wait_for_timeout(500)
            await self._shot("companies-toolbar-not-found")
            raise

        nav = p.locator("nav[aria-label='Search filters']").first
        in_nav = None
        try:
            if await nav.count():
                in_nav = nav.locator("ul.search-reusables__filter-list li button") \
                    .filter(has_text=re.compile(r"^\s*Companies\s*$", re.I)).first
        except Exception:
            pass

        candidates = [
            in_nav,
            p.locator("#search-reusables__filters-bar ul.search-reusables__filter-list li button")
             .filter(has_text=re.compile(r"^\s*Companies\s*$", re.I)).first,
            p.get_by_role("button", name=re.compile(r"^\s*Companies\s*$", re.I)).first,
            p.locator("button.artdeco-pill", has_text=re.compile(r"^\s*Companies\s*$", re.I)).first,
        ]

        btn = None
        for cand in candidates:
            try:
                if cand and await cand.count():
                    btn = cand
                    break
            except Exception:
                continue

        if not btn:
            await self._shot("companies-button-not-found")
            raise TimeoutError("Could not find the 'Companies' pill")

        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await btn.wait_for(timeout=5_000)
        except Exception:
            await p.wait_for_timeout(200)
            await self._shot("companies-before-click-forced")

        await self.click(btn)
        await p.wait_for_timeout(250)

        # Validate by URL or companies link presence
        try:
            if "/search/results/companies" not in p.url:
                await p.wait_for_function(
                    "() => location.pathname.includes('/search/results/companies')",
                    timeout=3_000,
                )
        except Exception:
            pass

        try:
            await p.wait_for_selector("a[href*='/company/']", timeout=6_000)
        except Exception:
            await self._shot("companies-after-click-no-company-links-yet")

        await self._shot("companies-clicked")
        self.log("[companies] pill clicked and validated (best-effort)")

    async def _go_home(self) -> None:
        p = self.page; assert p
        target = self.URL + "feed/?doFeedRefresh=true&nis=true"

        ok = await self.safe_goto(target, max_retries=3)
        if not ok:
            await self.safe_goto(self.URL + "feed/", max_retries=2)

        await p.wait_for_selector(
            "header#global-nav, div[role='search'] input[data-testid='typeahead-input']",
            timeout=10_000
        )

        try:
            await self._ensure_search_box_open()
        finally:
            await self._shot("home-loaded")

    async def _extract_data_names_urls(self, out_names: list[str], out_urls: list[str]):
        p = self.page; assert p

        try:
            await p.wait_for_selector("div.artdeco-pagination, ul.artdeco-pagination__pages", timeout=4_000)
        except Exception:
            pass

        page_i = 1
        while True:
            self.log(f"[page{page_i}] wait results")
            await self._wait_for_results()
            await self._shot(f"page-{page_i}-results")

            cards = await self._result_cards().all()
            self.log(f"[page{page_i}] cards={len(cards)}")

            for i, card in enumerate(cards):
                try:
                    link = card.locator('a[href*="linkedin.com/in/"]').first
                    if not await link.count():
                        link = card.locator('a[data-view-name="search-result-lockup-title"]').first
                    if not await link.count():
                        link = card.locator(".linked-area a[href]").first

                    href = await link.get_attribute("href") if await link.count() else None

                    # Extract a name
                    name_span = card.locator("a span[aria-hidden='true']").first
                    name = (await name_span.inner_text()).strip() if await name_span.count() else None
                    if not name and await link.count():
                        txt = await link.inner_text()
                        name = (txt or "").strip()

                    # Skip non-result widgets (e.g., feedback card)
                    dvn = (await card.get_attribute("data-view-name")) or ""
                    if dvn in ("search-feedback-card", "SERP_TASK_MODULE"):
                        continue

                    if href and "linkedin.com/in/" in href:
                        out_names.append(name or "(unknown)")
                        out_urls.append(href)
                        self.log(f"[✓] {(name or '(unknown)')} → {href}")
                except Exception as e:
                    self.log(f"[!] Error on card {i}: {e}")

            clicked = await self._click_next_or_stop()
            if not clicked:
                break
            self.log(f"[page{page_i}] next → {page_i + 1}")

            prev_key = await self._first_result_key()
            try:
                await p.wait_for_function(
                    """
                    (prevKey) => {
                      const card = document.querySelector(
                        '[data-view-name^="search-entity-result"], [data-view-name="people-search-result"], [data-chameleon-result-urn], li.reusable-search__result-container'
                      );
                      if (!card) return false;
                      const link = card.querySelector('a[href*="linkedin.com/in/"]');
                      const curKey = link?.getAttribute('href') || (card.textContent || '').trim().slice(0, 200);
                      return curKey && curKey !== prevKey;
                    }
                    """,
                    arg=prev_key,
                    timeout=10_000,
                )
            except Exception:
                await p.wait_for_timeout(800)

            page_i += 1

    async def _ensure_search_box_open(self) -> None:
        p = self.page; assert p
        try:
            btn = p.locator("button.search-global-typeahead__collapsed-search-button").first
            if await btn.count() and not await p.locator("#global-nav-search input").first.is_visible():
                await btn.click()
                await p.wait_for_timeout(200)
                return
        except Exception:
            pass

    async def _find_global_search_input(self):
        p = self.page; assert p

        candidates = [
            "#global-nav-search input.search-global-typeahead__input",
            "header#global-nav input.search-global-typeahead__input",
            "header#global-nav input[role='combobox'][aria-autocomplete='list']",
            "input[data-view-name='search-global-typeahead-input']",
            "div[role='search'] input[data-testid='typeahead-input']",
            "input[data-testid='typeahead-input']",
            "div[role='search'] input[aria-autocomplete='list']",
            "input[placeholder='Search']",
            "input[aria-label='Search']",
        ]

        await self._ensure_search_box_open()

        # Prefer an interactable input
        for sel in candidates:
            loc = p.locator(sel).first
            try:
                if await loc.count():
                    try:
                        await loc.wait_for(state="visible", timeout=1000)
                    except Exception:
                        pass
                    if await loc.is_enabled():
                        return loc
            except Exception:
                continue

        # Shortcut "/" often focuses the search
        try:
            await p.keyboard.press("/")
            await p.wait_for_timeout(150)
            for sel in candidates:
                loc = p.locator(sel).first
                if await loc.count() and await loc.is_enabled():
                    return loc
        except Exception:
            pass

        raise TimeoutError("Could not find a visible global search input in either header variant.")

    # ---------- filters helpers (new) ----------
    def _filters_nav(self):
        return self.page.locator("nav[aria-label='Search filters']").first

    async def _open_connections_filter(self) -> None:
        p = self.page; assert p
        self.log("[filters] open 'Connections' filter")

        nav = self._filters_nav()
        if await nav.count():
            try:
                btn = nav.get_by_role("button", name=re.compile(r"\bConnections\b", re.I)).first
                await btn.wait_for(timeout=2000)
                await self.click(btn)
                await self._shot("connections-pill-open")
                return
            except Exception:
                pass

            try:
                pill = nav.locator("[data-test-reusables-filters__filter-pill='CONNECTIONS']").first
                await pill.wait_for(timeout=1500)
                await self.click(pill)
                await self._shot("connections-pill-dataattr")
                return
            except Exception:
                pass

            try:
                allf = nav.get_by_role("button", name=re.compile(r"^\s*All\s+filters\s*$", re.I)).first
                await allf.wait_for(timeout=2000)
                await self.click(allf)
                await self._shot("all-filters-open")
                return
            except Exception:
                pass

        try:
            tb = p.locator("div[role='toolbar']").first
            if await tb.count():
                allf_tb = tb.get_by_role("button", name=re.compile(r"^\s*All\s+filters\s*$", re.I)).first
                if await allf_tb.count():
                    await self.click(allf_tb)
                    await self._shot("all-filters-open-toolbar")
                    return
                allf_any = tb.locator("button", has_text=re.compile(r"^\s*All\s+filters\s*$", re.I)).first
                if await allf_any.count():
                    await self.click(allf_any)
                    await self._shot("all-filters-open-toolbar-fallback")
                    return
        except Exception:
            pass

        try:
            allf_global = p.get_by_role("button", name=re.compile(r"^\s*All\s+filters\s*$", re.I)).first
            if await allf_global.count():
                await self.click(allf_global)
                await self._shot("all-filters-open-global")
                return
        except Exception:
            pass

        await self._shot("connections-open-failed")
        raise TimeoutError("Could not open Connections / All filters UI")

    async def _open_all_filters(self):
        p = self.page; assert p

        nav = self._filters_nav()
        try:
            await nav.scroll_into_view_if_needed()
        except Exception:
            pass

        candidates = [
            nav.locator("button.search-reusables__all-filters-pill-button").first if await nav.count() else None,
            p.locator("button.search-reusables__all-filters-pill-button").first,
            nav.get_by_role("button", name=re.compile(r"^\s*All\s+filters\s*$", re.I)).first if await nav.count() else None,
            p.get_by_role("button", name=re.compile(r"^\s*All\s+filters\s*$", re.I)).first,
        ]

        btn = None
        for cand in candidates:
            try:
                if cand and await cand.count():
                    btn = cand
                    break
            except Exception:
                continue

        if not btn:
            await self._shot("all-filters-button-not-found")
            raise TimeoutError("Could not find 'All filters' button")

        await self.click(btn)

        dlg_selectors = [
            "div[role='dialog'][data-test-reusables-filters-modal]",
            "div[role='dialog'][aria-label*='filter' i]",
            "div[role='dialog']",
        ]
        dlg = None
        for sel in dlg_selectors:
            candidate = p.locator(sel).last
            try:
                await candidate.wait_for(state="visible", timeout=5000)
                dlg = candidate
                break
            except Exception:
                continue

        if not dlg:
            await self._shot("all-filters-dialog-not-found")
            raise TimeoutError("All filters dialog did not open")

        return dlg

    async def _select_second_degree(self) -> None:
        p = self.page; assert p
        self.log("[filters] selecting 2nd-degree")

        # Prefer toolbar if present and hydrated (quick win even from modal path)
        try:
            await self._wait_filters_toolbar_hydrated(timeout_ms=10_000)
            tb = p.locator("div[role='toolbar']").first
            if await tb.count():
                radio = tb.locator("div[role='radio']:has(label:has-text('2nd'))").first
                if await radio.count():
                    await self.click(radio)
                    await self._shot("2nd-selected-radio")
                    await self.wait_network_quiet()
                    return
                sdui_label = tb.locator("label", has_text=SECOND_RX).first
                if await sdui_label.count():
                    await self.click(sdui_label)
                    await self._shot("2nd-selected-sdui-label")
                    await self.wait_network_quiet()
                    return
        except Exception:
            # If toolbar not there, proceed with your other fallbacks
            pass

        # Original fallbacks with broadened matching:

        # ARIA radio by name
        try:
            radio = p.get_by_role("radio", name=SECOND_RX).first
            await radio.wait_for(timeout=2000)
            await self.click(radio)
            await self._shot("2nd-selected-aria-radio")
            await self.wait_network_quiet()
            return
        except Exception:
            pass

        # Specific ARIA-label button
        try:
            btn = p.locator("button[aria-label='2nd']").first
            await btn.wait_for(timeout=2000)
            await self.click(btn)
            await self._shot("2nd-selected-button")
            await self.wait_network_quiet()
            return
        except Exception:
            pass

        # Generic label with broader regex
        try:
            lab = p.locator("label", has_text=SECOND_RX).first
            await lab.wait_for(timeout=2000)
            await lab.click(force=True)
            await self._shot("2nd-selected-generic-label")
            await self.wait_network_quiet()
            return
        except Exception:
            pass

        await self._shot("2nd-not-found")
        raise TimeoutError("Could not find '2nd' option in Connections filter")

    async def _apply_filters_if_present(self) -> None:
        p = self.page; assert p
        try:
            btn = p.get_by_role("button", name=re.compile(r"(show\s+results|apply)", re.I)).first
            await btn.wait_for(timeout=2000)
            await self.click(btn)
            await self._shot("filters-applied")
        except Exception:
            pass

async def _select_second_degree_toolbar_first(self) -> None:
    p = self.page; assert p
    self.log("[filters] try multiselect pills / toolbar radios for 1st/2nd/3rd+]")

    # Keep your original wait targets but extend the timeout a bit
    try:
        await self.wait_for_any(
            [
                "nav[aria-label='Search filters']",
                "div[role='toolbar']",
            ],
            timeout=15_000,
        )
    except Exception:
        self.log("[filters] no toolbar/nav attached yet")
        await self._open_connections_filter()
        return

    # Make sure the lazy filter bar is actually hydrated
    await self._wait_filters_toolbar_hydrated(timeout_ms=15_000)

    # Try working primarily inside the toolbar scope
    tb = p.locator("div[role='toolbar']").first
    try:
        if await tb.count():
            await tb.scroll_into_view_if_needed()
            await p.wait_for_timeout(120)
    except Exception:
        pass

    # 1) Preferred: click the radio container that has a label with "2nd"
    radio = tb.locator("div[role='radio']:has(label:has-text('2nd'))").first
    if await radio.count():
        await self.click(radio)
        await self._shot("2nd-selected-radio")
        await self.wait_network_quiet()
        return

    # 2) Label inside toolbar (your original approach but regex loosened)
    sdui_label = tb.locator("label", has_text=SECOND_RX).first
    if await sdui_label.count():
        await self.click(sdui_label)
        await self._shot("2nd-selected-sdui-label")
        await self.wait_network_quiet()
        return

    # 3) ARIA radio by accessible name (still scoped first, then global)
    radio_scoped = tb.get_by_role("radio", name=SECOND_RX).first
    if await radio_scoped.count():
        await self.click(radio_scoped)
        await self._shot("2nd-selected-aria-radio-scoped")
        await self.wait_network_quiet()
        return

    radio_global = p.get_by_role("radio", name=SECOND_RX).first
    if await radio_global.count():
        await self.click(radio_global)
        await self._shot("2nd-selected-aria-radio-global")
        await self.wait_network_quiet()
        return

    # 4) Try the multiselect chips inside the nav (kept from your code)
    try:
        nav = self._filters_nav()
        if await nav.count():
            btn = nav.locator(
                "ul.search-reusables__multiselect-pill-list button[aria-label='2nd']"
            ).first
            if not await btn.count():
                btn = nav.locator(
                    "ul.search-reusables__multiselect-pill-list button:has-text('2nd')"
                ).first
            if await btn.count():
                await self.click(btn)
                await self._shot("2nd-selected-multiselect")
                await self.wait_network_quiet()
                return
    except Exception:
        pass

    # 5) Generic label/button fallbacks (kept, regex broadened)
    try:
        btn = p.locator("button[aria-label='2nd']").first
        if await btn.count():
            await self.click(btn)
            await self._shot("2nd-selected-button")
            await self.wait_network_quiet()
            return
    except Exception:
        pass

    try:
        lab = p.locator("label", has_text=SECOND_RX).first
        if await lab.count():
            await lab.click(force=True)
            await self._shot("2nd-selected-generic-label")
            await self.wait_network_quiet()
            return
    except Exception:
        pass

    self.log("[filters] toolbar chips not clickable → using All filters modal")
    await self._open_connections_filter()

    async def locate_within_scroll(self, text, MAX_SCROLLS=5, DELAY=1):
        for i in range(MAX_SCROLLS):
            next_button = self.page.locator(text)
            if await next_button.is_visible():
                self.log(f"[✓] Found {text} after {i+1} scrolls.")
                return next_button
            await self.page.mouse.wheel(0, 1000)
            await self.page.wait_for_timeout(DELAY * 1000)

    async def _find_next_button(self):
        p = self.page

        pagination = p.locator("div.artdeco-pagination").last
        try:
            if await pagination.count():
                await pagination.scroll_into_view_if_needed()
                await p.wait_for_timeout(150)
        except Exception:
            pass

        candidates = [
            p.locator("button[data-testid='pagination-controls-next-button-visible']").first,
            p.locator("button.artdeco-pagination__button--next").first,
            p.locator("button[aria-label='Next']").first,
            p.get_by_role("button", name=re.compile(r"^\s*Next\s*$", re.I)).first,
            p.locator("nav[aria-label='Pagination'] button[aria-label='Next']").first,
        ]

        for cand in candidates:
            try:
                if await cand.count():
                    return cand
            except Exception:
                continue

        return None

    async def _click_next_or_stop(self) -> bool:
        p = self.page

        try:
            if await p.locator("button[data-testid='pagination-controls-next-button-hidden']").count():
                self.log("[page] next is hidden → last page")
                return False
        except Exception:
            pass

        prev_page = await self._current_page_label()
        prev_key = await self._first_result_key()

        btn = await self._find_next_button()

        page_number_fallback = None
        if not btn:
            try:
                active_li = p.locator(
                    "div.artdeco-pagination ul.artdeco-pagination__pages li.active, "
                    "ul.artdeco-pagination__pages li.active, "
                    "li.artdeco-pagination__indicator.active, "
                    "li.artdeco-pagination__indicator.selected"
                ).first

                if await active_li.count():
                    page_number_fallback = active_li.locator("~ li button").first
                    if not await page_number_fallback.count():
                        page_number_fallback = active_li.locator("xpath=following-sibling::li[1]//button").first
            except Exception:
                pass

        if not btn and not page_number_fallback:
            self.log("[page] next not found → stop")
            return False

        target = btn or page_number_fallback

        try:
            await target.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            if (await target.get_attribute("disabled")) is not None or \
               ((await target.get_attribute("aria-disabled")) in ("true", "True")):
                self.log("[page] next disabled → stop")
                return False
        except Exception:
            pass

        if not await self.click_with_retry(target):
            self.log("[page] next click failed → stop")
            return False

        try:
            await p.wait_for_function(
                """
                ([prevPage, prevKey]) => {
                  const pageEl =
                    document.querySelector('button[aria-current="true"][aria-label^="Page"] span') ||
                    document.querySelector('li.active button[aria-label^="Page"] span');
                  const curPage = pageEl ? pageEl.textContent.trim() : null;

                  const card = document.querySelector(
                    '[data-view-name^="search-entity-result"], [data-view-name="people-search-result"], [data-chameleon-result-urn], li.reusable-search__result-container'
                  );
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
            await p.wait_for_timeout(900)

        return True

    async def _current_page_label(self) -> str | None:
        el = self.page.locator(
            'button[aria-current="true"][aria-label^="Page"] span, li.active button[aria-label^="Page"] span'
        ).first
        if await el.count():
            try:
                return (await el.text_content() or "").strip()
            except Exception:
                return None
        return None

    async def _first_result_key(self) -> str | None:
        card = self.page.locator(
            '[data-view-name^="search-entity-result"], [data-view-name="people-search-result"], [data-chameleon-result-urn], li.reusable-search__result-container'
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

    async def _wait_filters_toolbar_hydrated(self, timeout_ms: int = 15_000) -> None:
        """Wait until the lazyLoadedFilterBar renders a visible toolbar with radios."""
        p = self.page; assert p
        # Wait for the SDUI container, then for the toolbar and at least one radio
        await p.wait_for_selector(
            "[data-sdui-component*='lazyLoadedFilterBar']",
            timeout=timeout_ms
        )
        # The toolbar itself
        await p.wait_for_selector(
            "div[role='toolbar']",
            timeout=timeout_ms
        )
        # Radios inside the toolbar; we don't assume which labels are present
        try:
            await p.wait_for_selector(
                "div[role='toolbar'] div[role='radio']",
                timeout=timeout_ms
            )
        except Exception:
            # Some variants render as buttons/labels first; just let the caller proceed.
            pass
