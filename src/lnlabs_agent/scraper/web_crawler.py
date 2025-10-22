# src/lnlabs_agent/scraper/web_crawler.py
from __future__ import annotations

import os, re, json, random, asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Callable, List, Iterable
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from platformdirs import user_log_dir, user_config_dir

from lnlabs_agent.secure_cookies import SecureCookieMixin

# ------------------------ Regexes / small utils ------------------------

SECOND_RX = re.compile(r"^\s*2nd(?:\s*degree)?\b", re.I)

def _ms(min_s: float = 0.15, max_s: float = 0.35) -> int:
    """Small human-like delay in ms."""
    return int(random.uniform(min_s, max_s) * 1000)

# ------------------------ Centralized selector registry ------------------------
# For each "object" we target on linkedin, we keep a list of alternative selectors.
# Add to these lists (don't replace) when LinkedIn changes markup.

SELECTORS = {
    # --- Global / layout ---
    "results_container": [
        "div.search-results-container",                         # classic
        "div.search-results__list",                             # alt (some A/B)
    ],
    "result_cards": [
        '[data-view-name="people-search-result"]',
        '[data-view-name="search-result"]',
        '[data-view-name="search-entity-result-universal-template"]',
        '[data-view-name^="search-entity-result-"]',
        '[data-chameleon-result-urn]',
        "li.reusable-search__result-container",
        "div.search-result__wrapper",
    ],
    "profile_link_in_card": [
        'a[href*="/in/"]'
    ],
    "company_link_anywhere": [
        "a[href*='/company/']"
    ],
    "search_filters_nav": [
        "nav[aria-label='Search filters']",
        "#search-reusables__filters-bar nav[aria-label='Search filters']",  # nested
    ],
    "toolbar": [
        "div[role='toolbar']",
    ],
    "all_filters_button": [
        "button.search-reusables__all-filters-pill-button",
        "button[aria-label='All filters']",
        "button[aria-label='All Filters']",
        "button[aria-label*='All' i][aria-label*='filter' i]",
        "button.artdeco-pill:has-text('All filters')",
    ],
    "companies_pill": [
        "ul.search-reusables__filter-list li button:has-text('Companies')",
        "button.artdeco-pill:has-text('Companies')",
        "button:has-text('Companies')",
    ],
    "connections_pill": [
        # Prefer role-based first; fallbacks allow text match
        "button[role='button']:has-text('Connections')",
        "[data-test-reusables-filters__filter-pill='CONNECTIONS']",
        "button:has-text('All filters')",  # as a last resort open All filters
    ],
    # --- Search input / header ---
    "search_input": [
        "#global-nav-search input.search-global-typeahead__input",
        "header#global-nav input.search-global-typeahead__input",
        "header#global-nav input[role='combobox'][aria-autocomplete='list']",
        "input[data-view-name='search-global-typeahead-input']",
        "div[role='search'] input[data-testid='typeahead-input']",
        "input[data-testid='typeahead-input']",
        "div[role='search'] input[aria-autocomplete='list']",
        "input[placeholder='Search']",
        "input[aria-label='Search']",
    ],
    "search_expand_button": [
        "button.search-global-typeahead__collapsed-search-button",
    ],
    # --- Company page ---
    "company_top_card": [
        "div.org-top-card-summary-info-list",     # the container you pasted
        "section.org-top-card",                   # alt containers seen in AB tests
        "div.org-top-card__primary-content",
    ],

    "company_employees_link": [
        # Exact class on the anchor you pasted:
        "div.org-top-card-summary-info-list a.org-top-card-summary-info-list__info-item-link",
        "a.org-top-card-summary-info-list__info-item-link",

        # Very common canned-search pattern from company pages:
        "a[href*='currentCompany=']",
        "a[href*='/search/results/people/?currentCompany=']",

        # Generic fallbacks:
        "a[href$='/people/']",
        "a[href*='/people/']",
        "a:has(span:has-text('employees'))",
        "a:has-text('employees')",
    ],
    # --- Pagination ---
    "pagination_container": [
        "div.artdeco-pagination",
        "nav[aria-label='Pagination']",
        "ul.artdeco-pagination__pages",
    ],
    "pagination_next_button": [
        "button[data-testid='pagination-controls-next-button-visible']",
        "button.artdeco-pagination__button--next",
        "nav[aria-label='Pagination'] button[aria-label='Next']",
        "button[aria-label='Next']",
        "button:has-text('Next')",
    ],
    "pagination_next_hidden": [
        "button[data-testid='pagination-controls-next-button-hidden']",
    ],
    "pagination_page_label": [
        'button[aria-current="true"][aria-label^="Page"] span',
        "li.active button[aria-label^='Page'] span",
        "li.selected button[aria-label^='Page'] span",
    ],
    "pagination_active_li": [
        "div.artdeco-pagination ul.artdeco-pagination__pages li.active",
        "ul.artdeco-pagination__pages li.active",
        "li.artdeco-pagination__indicator.active",
        "li.artdeco-pagination__indicator.selected",
    ],
}

def _join(selectors: Iterable[str]) -> str:
    return ", ".join(selectors)

# ------------------------ Crawler ------------------------

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

        # Expose the result-card list for quick patching (legacy external entry point)
        self.RESULT_CARD_SELECTORS = list(SELECTORS["result_cards"])
        self.CONNECTIONS_CONTAINER_SELECTORS = [
            # Old/radio toolbar
            "div[role='toolbar']",
            # New/pill list
            "ul.search-reusables__multiselect-pill-list",
            # SDUI / lazyLoadedFilterBar (new variant you pasted)
            "[data-sdui-component*='lazyLoadedFilterBar']",
            "[componentkey='SearchResults_SearchResultsFilterBar']",
        ]

        self.CONNECTIONS_2ND_SELECTORS = [
            # New SDUI radio-toolbar variants (label text lives inside the radio item)
            "div[role='toolbar'] [role='radio'] label:has-text('2nd')",
            "div[role='toolbar'] [role='radio']:has(label:has-text('2nd'))",
            # Old toolbar/radio style
            "div[role='toolbar'] label:has-text('2nd')",
            "div[role='toolbar'] div[role='radio']:has-text('2nd')",
            "div[role='toolbar'] [aria-label*='2nd' i]",
            # New multiselect pills
            "ul.search-reusables__multiselect-pill-list button[aria-label='2nd']",
            "ul.search-reusables__multiselect-pill-list button:has-text('2nd')",
            # Generic fallbacks
            "button[aria-label='2nd']",
            "button:has-text('2nd')",
        ]

    # ------------------------ Paths / shots ------------------------

    def _abs(self, p: os.PathLike | str) -> Path:
        return Path(p).expanduser().resolve()

    async def _shot(self, name: str) -> None:
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.artifacts_dir / f"{ts}-{name}.png"
            if self.page:
                await self.page.screenshot(path=str(path), full_page=False)
                self.log(f"[shot] {path}")
        except Exception as e:
            self.log(f"[shot] failed: {e}")

    # ------------------------ Session helpers ------------------------

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

    # ------------------------ Auditing ------------------------

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

    # ------------------------ Page listeners ------------------------

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

    # ------------------------ Playwright context ------------------------

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
            self.log("[session] closed]")

    # ------------------------ Tiny loc utilities (selector-aware) ------------------------

    def _sel(self, key: str) -> List[str]:
        return SELECTORS.get(key, [])

    def _loc(self, key: str):
        """Return a Locator for 'any of' the selectors under key (union)."""
        assert self.page
        sels = self._sel(key)
        if not sels:
            return self.page.locator("__never__")
        return self.page.locator(_join(sels))

    async def _first_present(self, key: str):
        """Return the first locator under key that exists (count>0), else None."""
        assert self.page
        for sel in self._sel(key):
            loc = self.page.locator(sel).first
            try:
                if await loc.count():
                    return loc
            except Exception:
                continue
        return None

    async def _first_visible(self, key: str, timeout_ms: int = 2000):
        """Return the first locator under key that becomes visible (best-effort)."""
        assert self.page
        for sel in self._sel(key):
            loc = self.page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return loc
            except Exception:
                continue
        return None

    # ------------------------ Basic helpers ------------------------

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

    # ------------------------ Auth helpers ------------------------

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

    # ------------------------ Navigation primitives ------------------------

    async def safe_goto(self, url: str, max_retries: int = 3) -> bool:
        assert self.page
        for attempt in range(max_retries):
            try:
                await self.page.goto(url, timeout=10_000)
                await self.page.wait_for_load_state("domcontentloaded")
                if "linkedin.com/feed" in (self.page.url or "") and "feed" not in url:
                    self.log("[nav] redirected to feed; retrying …]")
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

    # ------------------------ Basic locator wrappers ------------------------

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

    # ------------------------ Results helpers ------------------------

    async def _wait_for_results(self, timeout_ms: int = 20_000) -> None:
        p = self.page; assert p

        # Fast path: any visible selector quickly
        for sel in SELECTORS["result_cards"]:
            try:
                await p.wait_for_selector(f"{sel} >> visible=true", timeout=3_000)
                return
            except Exception:
                pass

        # Slow path: any matching element in the DOM
        await p.wait_for_function(
            """(sels) => sels.some(s => document.querySelector(s))""",
            arg=SELECTORS["result_cards"],
            timeout=timeout_ms,
        )
        await p.wait_for_timeout(400)

    def _result_cards(self):
        # kept sync (legacy callsites)
        return self._result_cards_union()

    def _result_cards_union(self):
        """
        Broad union of result-card selectors, filtered to only cards that contain a profile link (/in/).
        This keeps us layout-agnostic and avoids ads / widgets.
        """
        p = self.page; assert p
        union = _join(SELECTORS["result_cards"])
        cards = p.locator(union)
        return cards.filter(has=p.locator(_join(SELECTORS["profile_link_in_card"])))

    async def _maybe_scoped_cards(self):
        """
        Prefer scoping to the container if (and only if) it exists *and* contains results.
        Fallback to the page-wide union otherwise.
        """
        p = self.page; assert p
        base = self._result_cards_union()
        container = await self._first_present("results_container")
        try:
            if container:
                scoped = container.locator(_join(SELECTORS["result_cards"]))
                scoped = scoped.filter(has=p.locator(_join(SELECTORS["profile_link_in_card"])))
                if await scoped.count() > 0:
                    return scoped
        except Exception:
            pass
        return base

    async def _card_name_and_url(self, card):
        """
        Extract just {name, url} from a result card.
        - URL: first profile link
        - Name: prefer lockup title, then the anchor text
        - Normalize URL: absolute + strip ?query#fragment
        """
        # URL
        link = card.locator(_join(SELECTORS["profile_link_in_card"])).first
        href = await link.get_attribute("href") if await link.count() else None
        url = (href or "").strip() or None
        if url:
            if url.startswith("/"):  # absolutize if needed
                base = self.URL.rstrip("/")
                url = f"{base}{url}"
            url = url.split("?", 1)[0].split("#", 1)[0]  # strip query/fragment

        # NAME (prefer lockup title; fallback to anchor text)
        name_loc = card.locator(
            '[data-view-name="search-result-lockup-title"] a, '
            'a[data-view-name="search-result-lockup-title"], '
            + _join(SELECTORS["profile_link_in_card"])
        ).first
        name_txt = ""
        try:
            name_txt = (await name_loc.text_content() or "").strip()
        except Exception:
            pass
        name = name_txt or "(unknown)"

        return {"name": name, "url": url}

    # ------------------------ Main flows ------------------------

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
        container_union = self._loc("results_container")
        await container_union.first.wait_for(timeout=10_000)
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
                cand = item.locator(_join(SELECTORS["company_link_anywhere"])).first
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
        await self.page.wait_for_timeout(1200)
        self.log("[step] open employees")
        await self._open_company_employees()
        await self.page.wait_for_timeout(20000)

        self.log("[step] filter 2nd-degree (simple toolbar click)]")
        ok = await self._click_second_degree_simple(timeout_ms=15_000)
        if not ok:
            await self._shot("2nd-simple-failed")
            raise TimeoutError("Could not select '2nd' from toolbar")
        await self.wait_network_quiet()

        self.log("[step] extract names/urls (paged)")
        await self._extract_data_names_urls(out_names, out_urls)
        self.log(f"[step] extracted {len(out_urls)} urls")

    # ------------------------ Search filters / pills ------------------------

    async def _click_companies_tab(self) -> None:
        p = self.page
        assert p
        self.log("[companies] click 'Companies' pill (via registry)")

        try:
            await self.wait_for_any(
                self._sel("search_filters_nav") + ["#search-reusables__filters-bar", "ul.search-reusables__filter-list"],
                timeout=12_000,
            )
        except Exception:
            await p.wait_for_timeout(500)
            await self._shot("companies-toolbar-not-found")
            raise

        # Try within nav first
        nav = await self._first_present("search_filters_nav")

        btn = None
        if nav:
            # All variants inside nav
            for sel in self._sel("companies_pill"):
                cand = nav.locator(sel).first
                try:
                    if await cand.count():
                        btn = cand; break
                except Exception:
                    continue

        if not btn:
            # Global fallbacks
            for sel in self._sel("companies_pill"):
                cand = p.locator(sel).first
                try:
                    if await cand.count():
                        btn = cand; break
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
            await p.wait_for_selector(_join(SELECTORS["company_link_anywhere"]), timeout=6_000)
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

        # either global nav exists or search input appears
        try:
            await p.wait_for_selector(
                "header#global-nav, " + _join(SELECTORS["search_input"]),
                timeout=10_000
            )
        except Exception:
            pass

        try:
            await self._ensure_search_box_open()
        finally:
            await self._shot("home-loaded")

    async def _extract_data_names_urls(self, out_names: list[str], out_urls: list[str]):
        p = self.page; assert p

        try:
            await p.wait_for_selector(_join(SELECTORS["pagination_container"]), timeout=4_000)
        except Exception:
            pass

        seen = set()  # de-dupe by normalized URL
        page_i = 1
        while True:
            self.log(f"[page{page_i}] wait results")
            await self._wait_for_results()
            await self._shot(f"page-{page_i}-results")

            cards_loc = await self._maybe_scoped_cards()
            cards = await cards_loc.all()
            self.log(f"[page{page_i}] cards={len(cards)}")

            for i, card in enumerate(cards):
                try:
                    item = await self._card_name_and_url(card)
                    url = item["url"]
                    if url and url not in seen:
                        seen.add(url)
                        out_names.append(item["name"])
                        out_urls.append(url)
                        self.log(f"[✓] {item['name']} → {url}")
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
                      const link = card.querySelector('a[href*="/in/"]');
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
        # If there's a collapsed button and the input isn't visible, click it.
        try:
            btn = await self._first_present("search_expand_button")
            if btn and not await p.locator("#global-nav-search input").first.is_visible():
                await btn.click()
                await p.wait_for_timeout(200)
                return
        except Exception:
            pass

    async def _find_global_search_input(self):
        p = self.page; assert p

        await self._ensure_search_box_open()

        # Prefer an interactable input
        for sel in self._sel("search_input"):
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
            for sel in self._sel("search_input"):
                loc = p.locator(sel).first
                if await loc.count() and await loc.is_enabled():
                    return loc
        except Exception:
            pass

        raise TimeoutError("Could not find a visible global search input in either header variant.")

    # ------------------------ Filters helpers ------------------------

    def _filters_nav(self):
        # Favor first present nav
        return self._loc("search_filters_nav")

    async def _open_connections_filter(self) -> None:
        p = self.page; assert p
        self.log("[filters] open 'Connections' filter")

        nav = await self._first_present("search_filters_nav")
        if nav:
            # 1) Try connections pill in the nav
            for sel in self._sel("connections_pill"):
                try:
                    btn = nav.locator(sel).first
                    if await btn.count():
                        await btn.wait_for(timeout=1500)
                        await self.click(btn)
                        await self._shot("connections-pill-open")
                        return
                except Exception:
                    continue

        # 2) Try toolbar-level "All filters" as fallback
        try:
            tb = await self._first_present("toolbar")
            if tb:
                # Try "All filters" in toolbar
                for sel in self._sel("all_filters_button"):
                    allf_tb = tb.locator(sel).first
                    if await allf_tb.count():
                        await self.click(allf_tb)
                        await self._shot("all-filters-open-toolbar")
                        return
                # Generic text fallback
                allf_any = tb.locator("button", has_text=re.compile(r"^\s*All\s+filters\s*$", re.I)).first
                if await allf_any.count():
                    await self.click(allf_any)
                    await self._shot("all-filters-open-toolbar-fallback")
                    return
        except Exception:
            pass

        # 3) Try global "All filters"
        for sel in self._sel("all_filters_button"):
            try:
                allf_global = p.locator(sel).first
                if await allf_global.count():
                    await self.click(allf_global)
                    await self._shot("all-filters-open-global")
                    return
            except Exception:
                continue

        await self._shot("connections-open-failed")
        raise TimeoutError("Could not open Connections / All filters UI")

    async def _open_all_filters(self):
        p = self.page; assert p

        nav = await self._first_present("search_filters_nav")
        try:
            if nav:
                await nav.scroll_into_view_if_needed()
        except Exception:
            pass

        # Try in-nav first, then global
        btn = None
        if nav:
            for sel in self._sel("all_filters_button"):
                cand = nav.locator(sel).first
                if await cand.count():
                    btn = cand; break

        if not btn:
            for sel in self._sel("all_filters_button"):
                cand = p.locator(sel).first
                if await cand.count():
                    btn = cand; break

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

    # --- simplified “2nd” selection path (no All Filters, no scrolling) ---
    async def _wait_connections_ui_ready(self, timeout_ms: int = 10_000):
        p = self.page; assert p
        sels = self.CONNECTIONS_CONTAINER_SELECTORS
        # Wait for ANY container…
        await p.wait_for_function(
            """(sels)=>sels.some(s=>document.querySelector(s))""",
            arg=sels, timeout=timeout_ms
        )
        # …and then ensure at least one radio or pill exists
        try:
            await p.wait_for_selector(
                "div[role='toolbar'] [role='radio'], ul.search-reusables__multiselect-pill-list button",
                timeout=timeout_ms
            )
        except Exception:
            # give SDUI a beat to hydrate
            await p.wait_for_timeout(300)



    async def _verify_2nd_checked(self) -> bool:
        p = self.page; assert p
        radio = p.locator('div[role="toolbar"] div[role="radio"]').filter(has_text=SECOND_RX).first
        try:
            if await radio.count():
                v = await radio.get_attribute("aria-checked")
                return (v or "").lower() == "true"
        except Exception:
            pass
        # Fallback through the label association
        try:
            lab = p.locator('div[role="toolbar"] label').filter(has_text=SECOND_RX).first
            if await lab.count():
                return await lab.evaluate(
                    "el => el.closest('div[role=radio]')?.getAttribute('aria-checked') === 'true'"
                )
        except Exception:
            pass
        return False

    async def _click_second_degree_simple(self, timeout_ms: int = 12_000) -> bool:
        p = self.page; assert p
        await self._wait_connections_ui_ready(timeout_ms)
        await self._dismiss_open_popovers()

        # Bring filters bar into view if present
        try:
            nav = await self._first_present("search_filters_nav")
            if nav:
                await nav.scroll_into_view_if_needed()
                await p.wait_for_timeout(100)
        except Exception:
            pass

        # Try candidates in priority (SDUI radios → old radios → new pills → generic)
        candidates_in_order = list(self.CONNECTIONS_2ND_SELECTORS)

        for sel in candidates_in_order:
            try:
                target = p.locator(sel).first
                if not await target.count():
                    continue

                try: await target.scroll_into_view_if_needed()
                except: pass
                try: await target.wait_for(state="visible", timeout=1500)
                except: pass

                # 1) Click the target normally
                if await self.click_with_retry(target, attempts=3, delay_ms=160):
                    if await self._verify_connections_2nd_selected():
                        return True

                # 2) If this is the SDUI radio flavor, click the closest radio and try Space on it
                try:
                    await p.evaluate(
                        """(sel) => {
                          const t = document.querySelector(sel);
                          if (!t) return false;
                          const radio = t.closest('[role="radio"]') || t;
                          radio.click();
                          radio.focus && radio.focus();
                          return true;
                        }""",
                        sel,
                    )
                    await p.keyboard.press("Space")
                except Exception:
                    pass

                # Tiny delay for aria to flip
                await p.wait_for_timeout(250)
                if await self._verify_connections_2nd_selected():
                    return True

                # 3) Force click via JS as last resort
                try:
                    await p.evaluate("(el)=>el.click()", target)
                except Exception:
                    pass

                await p.wait_for_timeout(200)
                if await self._verify_connections_2nd_selected():
                    return True
            except Exception:
                continue

        # 4) All else fails → All Filters modal path
        return await self._set_2nd_via_all_filters()



    # --- legacy robust helpers kept (unchanged logic) ---
    async def _wait_filters_toolbar_hydrated(self, timeout_ms: int = 15_000) -> None:
        """Wait until the lazyLoadedFilterBar renders a visible toolbar with radios."""
        p = self.page; assert p
        await p.wait_for_selector(
            "[data-sdui-component*='lazyLoadedFilterBar']",
            timeout=timeout_ms
        )
        await p.wait_for_selector(
            "div[role='toolbar']",
            timeout=timeout_ms
        )
        try:
            await p.wait_for_selector(
                "div[role='toolbar'] div[role='radio']",
                timeout=timeout_ms
            )
        except Exception:
            pass

    async def _activate_radio_by_label(self, label_regex: re.Pattern, scope_selector: str = "div[role='toolbar']", timeout_ms: int = 8_000) -> bool:
        p = self.page; assert p

        scope = p.locator(scope_selector).first
        if await scope.count():
            try:
                await scope.scroll_into_view_if_needed()
                await p.wait_for_timeout(120)
            except Exception:
                pass
        else:
            scope = p

        label = scope.locator("label").filter(has_text=label_regex).first
        if not await label.count():
            try:
                await p.wait_for_timeout(200)
            except Exception:
                pass
            label = scope.locator("label").filter(has_text=label_regex).first
            if not await label.count():
                return False

        radio = label.locator("xpath=ancestor::*[@role='radio'][1]").first
        input_id = await label.get_attribute("for")
        input_by_for = p.locator(f'#{input_id}') if input_id else None

        async def _is_selected() -> bool:
            try:
                if await radio.count():
                    val = await radio.get_attribute("aria-checked")
                    if (val or "").lower() == "true":
                        return True
                if input_by_for and await input_by_for.count():
                    return await input_by_for.is_checked()
            except Exception:
                pass
            return False

        if await _is_selected():
            return True

        try:
            await label.click()
            if await _is_selected():
                return True
        except Exception:
            pass

        if await radio.count():
            try:
                await radio.click()
                if await _is_selected():
                    return True
            except Exception:
                pass
            try:
                await radio.focus()
                await p.keyboard.press("Space")
                if await _is_selected():
                    return True
            except Exception:
                pass
            try:
                await p.evaluate("(el) => el.click()", radio)
                if await _is_selected():
                    return True
            except Exception:
                pass

        try:
            ctrl = p.get_by_label(label_regex).first
            if await ctrl.count():
                await ctrl.check(force=True)
                if await _is_selected():
                    return True
        except Exception:
            pass

        try:
            await label.click(force=True)
            if await _is_selected():
                return True
        except Exception:
            pass

        return False

    async def _select_second_degree_toolbar_first(self) -> None:
        self.log("[filters] simple toolbar path → click '2nd'")
        ok = await self._click_second_degree_simple(timeout_ms=15_000)
        if not ok:
            await self._shot("2nd-simple-failed-toolbar-first")
            raise TimeoutError("Could not click '2nd' in toolbar")
        await self._shot("2nd-selected-simple")
        await self.wait_network_quiet()

    async def _select_second_degree(self) -> None:
        self.log("[filters] simple path (no All filters) → click '2nd'")
        ok = await self._click_second_degree_simple(timeout_ms=12_000)
        if not ok:
            await self._shot("2nd-simple-failed-alt")
            raise TimeoutError("Could not click '2nd' in toolbar")
        await self._shot("2nd-selected-simple-alt")
        await self.wait_network_quiet()

    async def _apply_filters_if_present(self) -> None:
        p = self.page; assert p
        try:
            btn = p.get_by_role("button", name=re.compile(r"(show\s+results|apply)", re.I)).first
            await btn.wait_for(timeout=2000)
            await self.click(btn)
            await self._shot("filters-applied")
        except Exception:
            pass

    # ------------------------ Pagination helpers ------------------------

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
        # Bring pagination into view if present
        pagination = await self._first_present("pagination_container")
        try:
            if pagination:
                await pagination.scroll_into_view_if_needed()
                await p.wait_for_timeout(150)
        except Exception:
            pass

        for sel in self._sel("pagination_next_button"):
            try:
                cand = p.locator(sel).first
                if await cand.count():
                    return cand
            except Exception:
                continue
        return None

    async def _click_next_or_stop(self) -> bool:
        p = self.page

        # If LinkedIn exposes a hidden-next sentinel, stop
        try:
            for sel in self._sel("pagination_next_hidden"):
                if await p.locator(sel).count():
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
                # Try to find the active page <li> and click the next sibling button
                for sel in self._sel("pagination_active_li"):
                    active_li = p.locator(sel).first
                    if await active_li.count():
                        page_number_fallback = active_li.locator("~ li button").first
                        if not await page_number_fallback.count():
                            page_number_fallback = active_li.locator("xpath=following-sibling::li[1]//button").first
                        if await page_number_fallback.count():
                            break
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
                    document.querySelector('li.active button[aria-label^="Page"] span') ||
                    document.querySelector('li.selected button[aria-label^="Page"] span');
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
        for sel in self._sel("pagination_page_label"):
            el = self.page.locator(sel).first
            if await el.count():
                try:
                    return (await el.text_content() or "").strip()
                except Exception:
                    return None
        return None

    async def _first_result_key(self) -> str | None:
        card = self.page.locator(
            _join(SELECTORS["result_cards"])
        ).first
        if await card.count():
            try:
                link = card.locator(_join(SELECTORS["profile_link_in_card"])).first
                if await link.count():
                    href = await link.get_attribute("href")
                    if href:
                        return href
                txt = await card.text_content()
                return (txt or "").strip()[:200]
            except Exception:
                return None
        return None


    async def _open_company_employees(self) -> None:
        """
        Wait for the company top card to hydrate, then click the 'employees' link.
        Handles multiple UI variants:
          - <a class="org-top-card-summary-info-list__info-item-link" ...>
          - canned search links with ?currentCompany=...
          - /people/ links
          - anchors whose visible text/span includes 'employees'
        """
        p = self.page; assert p

        # Wait for either the top card to appear or the link itself
        try:
            await p.wait_for_selector(
                _join(SELECTORS["company_top_card"]) + ", " + _join(SELECTORS["company_employees_link"]),
                timeout=10_000
            )
        except Exception:
            # keep going; we'll still try to find the link
            pass

        # Small retry window in case the top card content hydrates a beat later
        last_err = None
        for _ in range(5):
            try:
                # Prefer the most specific/intent-revealing anchors first
                priority_order = [
                    "a[href*='/search/results/people/?currentCompany=']",
                    "a[href*='currentCompany=']",
                    "div.org-top-card-summary-info-list a.org-top-card-summary-info-list__info-item-link",
                    "a.org-top-card-summary-info-list__info-item-link",
                    "a[href$='/people/']",
                    "a[href*='/people/']",
                    "a:has(span:has-text('employees'))",
                    "a:has-text('employees')",
                ]

                btn = None
                for sel in priority_order:
                    cand = p.locator(sel).first
                    try:
                        if await cand.count():
                            btn = cand
                            break
                    except Exception:
                        continue

                if not btn:
                    raise RuntimeError("Employees link not present (yet)")

                # Scroll into view and click
                try:
                    await btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await btn.wait_for(state="visible", timeout=1500)
                except Exception:
                    # Some variants are present but not strictly 'visible' — still try click
                    pass

                if not await self.click_with_retry(btn, attempts=3, delay_ms=180):
                    raise RuntimeError("Employees link click failed")

                await p.wait_for_load_state("domcontentloaded")
                return
            except Exception as e:
                last_err = e
                await p.wait_for_timeout(500)

        await self._shot("employees-link-missing")
        raise TimeoutError(f"Employees link not found on company page: {last_err}")


    async def _verify_connections_2nd_selected(self) -> bool:
        p = self.page; assert p

        # SDUI / modern radio-toolbar: check aria-checked on the radio hosting '2nd'
        try:
            r = p.locator("div[role='toolbar'] [role='radio']:has(label:has-text('2nd'))").first
            if await r.count():
                v = (await r.get_attribute("aria-checked") or "").lower()
                if v == "true":
                    return True
        except Exception:
            pass

        # Old toolbar/radio variant
        try:
            radio = p.locator('div[role="toolbar"] div[role="radio"]').filter(has_text=SECOND_RX).first
            if await radio.count():
                v = (await radio.get_attribute("aria-checked") or "").lower()
                if v == "true":
                    return True
        except Exception:
            pass

        # New pill buttons variant (aria-pressed or selected class)
        try:
            btn = p.locator(
                "ul.search-reusables__multiselect-pill-list button[aria-label='2nd'], "
                "ul.search-reusables__multiselect-pill-list button:has-text('2nd')"
            ).first
            if await btn.count():
                pressed = (await btn.get_attribute("aria-pressed") or "").lower() == "true"
                # class-based selection sometimes used by LI experiments
                cls = (await btn.get_attribute("class") or "")
                selected_cls = "search-reusables__multiselect-pill-button--selected" in cls
                if pressed or selected_cls:
                    return True
        except Exception:
            pass

        # Generic fallback
        try:
            any_btn = p.locator("button[aria-label='2nd'], button:has-text('2nd')").first
            if await any_btn.count():
                v1 = (await any_btn.get_attribute("aria-pressed") or "").lower()
                v2 = (await any_btn.get_attribute("aria-checked") or "").lower()
                cls = (await any_btn.get_attribute("class") or "")
                selected_cls = "search-reusables__multiselect-pill-button--selected" in cls
                return v1 == "true" or v2 == "true" or selected_cls
        except Exception:
            pass

        return False


    async def _set_2nd_via_all_filters(self) -> bool:
        p = self.page; assert p
        try:
            dlg = await self._open_all_filters()

            # Inside the modal, there’s usually a Connections group with 1st/2nd/3rd+ checkboxes or toggles
            # Try a few variants for the "2nd" control
            candidates = [
                "input[type='checkbox'][value='S']",                      # LI sometimes uses S for 2nd network
                "label:has-text('2nd') input[type='checkbox']",
                "button[aria-label='2nd']",
                "label:has-text('2nd')",
            ]

            target = None
            for sel in candidates:
                cand = dlg.locator(sel).first
                if await cand.count():
                    target = cand; break

            if not target:
                # fallback: find any control in dialog with visible text 2nd
                target = dlg.locator(":is(button,label,input):has-text('2nd')").first
                if not await target.count():
                    await self._shot("all-filters-no-2nd")
                    return False

            try:
                await target.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                await target.wait_for(state="visible", timeout=1500)
            except Exception:
                pass

            # Click (force if needed)
            try:
                await target.click()
            except Exception:
                try:
                    await p.evaluate("(el)=>el.click()", target)
                except Exception:
                    pass

            # Apply / Show results
            apply_btn = dlg.locator("button", has_text=re.compile(r"(show\s+results|apply)", re.I)).first
            if await apply_btn.count():
                await self.click_with_retry(apply_btn)
            else:
                # close dialog if no apply
                esc_ok = True
                try:
                    await p.keyboard.press("Escape")
                except Exception:
                    esc_ok = False
                if not esc_ok:
                    # click backdrop
                    try:
                        await p.mouse.click(5, 5)
                    except Exception:
                        pass

            await self.wait_network_quiet()
            # Verify using the shared verifier (which looks at pills/radios)
            return await self._verify_connections_2nd_selected()
        except Exception:
            return False


    async def _dismiss_open_popovers(self):
        p = self.page; assert p
        try:
            # hit Escape a couple of times
            await p.keyboard.press("Escape")
            await p.wait_for_timeout(100)
            await p.keyboard.press("Escape")
        except Exception:
            pass
        # try clicking outside the filters bar
        try:
            await p.mouse.click(5, 5)
        except Exception:
            pass
