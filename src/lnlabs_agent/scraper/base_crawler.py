# src/lnlabs_agent/scraper/base_crawler.py
from __future__ import annotations

import os, re, json, random, asyncio, time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Callable, List, Iterable
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from platformdirs import user_log_dir, user_config_dir

from lnlabs_agent.secure_cookies import SecureCookieMixin
from .selectors import SELECTORS, SECOND_RX, _join, _ms


def _slugify_artifact_label(label: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", label or "")
    cleaned = cleaned.strip("-")
    return (cleaned or "artifact")[:48]

# ------------------------ Crawler ------------------------

class BaseWebCrawler(SecureCookieMixin):
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
        self.diagnostic_paths: list[Path] = []

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
            # New multiselect pills
            "ul.search-reusables__multiselect-pill-list",
            # SDUI / lazyLoadedFilterBar wrappers
            "[data-sdui-component*='lazyLoadedFilterBar']",
            "[componentkey='SearchResults_SearchResultsFilterBar']",
            # Fallback to the bar wrapper class batch (harmless if absent)
            "#search-reusables__filters-bar",
        ]

        self.CONNECTIONS_2ND_SELECTORS = [
            # SDUI radio-toolbar: the label holds the text
            "div[role='toolbar'] [role='radio'] label:has-text('2nd')",
            "div[role='toolbar'] [role='radio']:has(label:has-text('2nd'))",
            # Older radio-toolbar variants
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
                self._record_artifact(path)
        except Exception as e:
            self.log(f"[shot] failed: {e}")

    def _record_artifact(self, path: Path) -> None:
        try:
            self.diagnostic_paths.append(path)
        except Exception:
            pass

    async def dump_dom(self, label: str) -> Optional[Path]:
        """
        Persist the current DOM to a file for diagnostics.
        """
        if not self.page:
            return None
        safe = _slugify_artifact_label(label)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.artifacts_dir / f"{ts}-{safe}.html"
        try:
            html = await self.page.content()
            path.write_text(html, encoding="utf-8")
            self._record_artifact(path)
            self.log(f"[dom] saved {path}")
            return path
        except Exception as exc:
            self.log(f"[dom] dump failed: {exc}")
            return None

    async def capture_failure_artifacts(self, label: str) -> None:
        """
        Capture a screenshot and DOM snapshot for diagnostics when a step fails.
        """
        safe = _slugify_artifact_label(label)
        try:
            await self._shot(f"failure-{safe}")
        except Exception as exc:
            self.log(f"[diag] shot failed: {exc}")
        await self.dump_dom(f"failure-{safe}")

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

    async def _goto_core(self, url: str, max_retries: int, allow_feed_redirect: bool) -> bool:
        assert self.page
        for attempt in range(max_retries):
            try:
                await self.page.goto(url, timeout=10_000)
                await self.page.wait_for_load_state("domcontentloaded")
                current_url = self.page.url or ""
                if (not allow_feed_redirect and "linkedin.com/feed" in current_url and "feed" not in url):
                    self.log("[nav] redirected to feed; retrying …]")
                    await asyncio.sleep(0.6 + attempt * 0.4)
                    continue
                return True
            except Exception as e:
                self.log(f"[nav] error loading {url} (attempt {attempt+1}): {e}")
            await asyncio.sleep(0.8 + attempt * 0.6)
        return False

    async def safe_goto(self, url: str, max_retries: int = 3) -> bool:
        return await self._goto_core(url, max_retries, allow_feed_redirect=False)

    async def goto_allow_feed_redirect(self, url: str, max_retries: int = 3) -> bool:
        return await self._goto_core(url, max_retries, allow_feed_redirect=True)

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

    async def _extract_data_names_urls(
        self,
        out_names: list[str],
        out_urls: list[str],
        limit: Optional[int] = None,
    ):
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
                if limit and len(out_urls) >= limit:
                    break
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
                if limit and len(out_urls) >= limit:
                    break

            if limit and len(out_urls) >= limit:
                break

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


        async def _check() -> bool:
            # --- A) SDUI / radio with label "2nd" (new + legacy hashed variants) ---
            try:
                radios = p.locator("div[role='toolbar'] [role='radio']").filter(
                    has=p.locator("label", has_text=re.compile(r"\b2nd\b", re.I))
                )
                count = await radios.count()
                for idx in range(min(count, 3)):
                    r = radios.nth(idx)
                    if not await r.count():
                        continue
                    try:
                        if not await r.is_visible():
                            continue
                    except Exception:
                        pass
                    attrs = {
                        "aria-checked": (await r.get_attribute("aria-checked") or "").lower(),
                        "aria-selected": (await r.get_attribute("aria-selected") or "").lower(),
                        "aria-pressed": (await r.get_attribute("aria-pressed") or "").lower(),
                        "data-state": (await r.get_attribute("data-state") or "").lower(),
                        "data-selected": (await r.get_attribute("data-selected") or "").lower(),
                        "data-checked": (await r.get_attribute("data-checked") or "").lower(),
                    }
                    if any(
                        attrs[key] in ("true", "mixed", "checked", "selected", "on")
                        for key in attrs
                    ):
                        return True
                    cls = (await r.get_attribute("class") or "")
                    if any(tok in cls for tok in ("selected", "is-selected", "active", "--selected")):
                        return True
                    try:
                        has_checked_input = await r.locator("input").evaluate_all(
                            "(nodes) => nodes.some((el) => el.matches(':checked') || "
                            "(el.getAttribute('aria-checked')||'').toLowerCase() === 'true' || "
                            "(el.getAttribute('data-state')||'').toLowerCase() === 'checked')"
                        )
                        if has_checked_input:
                            return True
                    except Exception:
                        pass
            except Exception:
                pass

            # --- B) Classic radio-toolbar where text is on the radio itself (legacy) ---
            try:
                radio_legacy = p.locator("div[role='toolbar'] div[role='radio']").filter(has_text=SECOND_RX)
                count = await radio_legacy.count()
                for idx in range(min(count, 3)):
                    radio = radio_legacy.nth(idx)
                    if not await radio.count():
                        continue
                    attrs = {
                        "aria-checked": (await radio.get_attribute("aria-checked") or "").lower(),
                        "aria-selected": (await radio.get_attribute("aria-selected") or "").lower(),
                        "aria-pressed": (await radio.get_attribute("aria-pressed") or "").lower(),
                    }
                    if any(attrs[key] in ("true", "mixed", "checked", "selected", "on") for key in attrs):
                        return True
                    cls = (await radio.get_attribute("class") or "")
                    if any(tok in cls for tok in ("selected", "is-selected", "active", "--selected")):
                        return True
            except Exception:
                pass

            # --- C) Check the associated INPUT (SDUI w/ hidden checkbox) ---
            try:
                labels = p.locator("div[role='toolbar'] label:has-text('2nd')")
                count = await labels.count()
                for idx in range(min(count, 3)):
                    lab = labels.nth(idx)
                    if not await lab.count():
                        continue
                    checked = await p.evaluate(
                        """
                        (el) => {
                          const id = el.getAttribute('for');
                          const hostRadio = el.closest('[role=\"radio\"], [data-view-name=\"search-filter-top-bar-select\"]');
                          if (hostRadio) {
                            const aria = [
                              hostRadio.getAttribute('aria-checked'),
                              hostRadio.getAttribute('aria-selected'),
                              hostRadio.getAttribute('aria-pressed'),
                              hostRadio.getAttribute('data-state'),
                              hostRadio.getAttribute('data-selected'),
                              hostRadio.getAttribute('data-checked'),
                            ].map(v => (v || '').toLowerCase());
                            if (aria.some(v => ['true','mixed','checked','selected','on'].includes(v))) {
                              return true;
                            }
                          }
                          if (!id) return false;
                          const inp = document.getElementById(id);
                          if (!inp) return false;
                          const aria = (inp.getAttribute('aria-checked')||inp.getAttribute('aria-selected')||inp.getAttribute('aria-pressed')||'').toLowerCase();
                          const data = (inp.getAttribute('data-state')||inp.getAttribute('data-selected')||inp.getAttribute('data-checked')||'').toLowerCase();
                          return !!(inp.checked || inp.matches?.(':checked') || aria === 'true' || aria === 'mixed' || ['true','mixed','checked','selected','on'].includes(data));
                        }
                    """,
                        lab,
                    )
                    if checked:
                        return True
            except Exception:
                pass

            # --- D) Multiselect pill variant ---
            try:
                btns = p.locator(
                    "ul.search-reusables__multiselect-pill-list button[aria-label='2nd'], "
                    "ul.search-reusables__multiselect-pill-list button:has-text('2nd')"
                )
                count = await btns.count()
                for idx in range(min(count, 3)):
                    btn = btns.nth(idx)
                    if not await btn.count():
                        continue
                    aria = (await btn.get_attribute("aria-pressed") or "").lower()
                    if aria in ("true", "mixed"):
                        return True
                    aria_selected = (await btn.get_attribute("aria-selected") or "").lower()
                    if aria_selected in ("true", "mixed"):
                        return True
                    cls = (await btn.get_attribute("class") or "")
                    if "search-reusables__multiselect-pill-button--selected" in cls:
                        return True
            except Exception:
                pass

            # --- E) URL state backstop (most reliable after navigation/ajax refresh) ---
            if self._url_network_has_2nd():
                return True

            return False

        if wait_ms <= 0:
            return await _check()

        deadline = time.monotonic() + (wait_ms / 1000.0)
        while True:
            if await _check():
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                await p.wait_for_timeout(150)
            except Exception:
                pass
