"""
Company-specific scraping routines extracted from web_crawler.py.
Provides mixin methods that operate on a crawler instance exposing the base helpers.
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urlparse

from .selectors import SELECTORS, SECOND_RX, _join


class CompanyFlowMixin:
    async def start_company_flow(self, company: str):
        self.log(f"[flow] company={company}")
        names: list[str] = []
        urls: list[str] = []

        if self._looks_like_company_url(company):
            await self._shot("before-company-url")
            await self._extract_from_company_url(company, names, urls)
        else:
            await self._shot("before-search")
            await self._extract_data_urls_names_company(company, names, urls)

        self.log(f"[flow] company done: {len(urls)} urls")
        await self._shot("after-company-flow")
        return names, urls

    def _looks_like_company_url(self, value: str) -> bool:
        if not value:
            return False
        value = value.strip()
        if not value:
            return False
        if "linkedin.com" not in value.lower():
            return False
        # Accept scheme-less URLs
        if value.startswith("/"):
            return False
        if not value.startswith("http://") and not value.startswith("https://"):
            candidate = "https://" + value
        else:
            candidate = value
        try:
            parsed = urlparse(candidate)
        except Exception:
            return False
        path = parsed.path.lower()
        return "/company/" in path

    def _normalize_company_url(self, url: str) -> str:
        url = url.strip()
        if not url:
            raise ValueError("Company URL cannot be empty")
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        elif not url.startswith("https://"):
            url = "https://" + url
        parsed = urlparse(url)
        if not parsed.netloc:
            raise ValueError(f"Invalid company URL: {url}")
        if "/company/" not in parsed.path.lower():
            raise ValueError("URL does not appear to be a LinkedIn company page")
        normalized = parsed._replace(query="", fragment="").geturl()
        return normalized

    async def _extract_from_company_url(
        self,
        company_url: str,
        out_names: list[str],
        out_urls: list[str],
    ) -> None:
        url = self._normalize_company_url(company_url)
        self.log(f"[step] open company url directly -> {url}")
        ok = await self.safe_goto(url, max_retries=3)
        if not ok:
            raise RuntimeError(f"Failed to load company URL: {url}")

        await self.page.wait_for_load_state("domcontentloaded")
        await self._shot("company-opened-direct")
        await self.page.wait_for_timeout(1200)

        try:
            await self._open_company_employees()
        except Exception as e:
            await self._shot("company-open-employees-failed")
            raise

        await self._scrape_company_employees(out_names, out_urls)

    async def _go_home(self) -> None:
        p = self.page
        assert p
        target = self.URL + "feed/?doFeedRefresh=true&nis=true"

        ok = await self.safe_goto(target, max_retries=3)
        if not ok:
            await self.safe_goto(self.URL + "feed/", max_retries=2)

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
        await self._scrape_company_employees(out_names, out_urls)

    async def _scrape_company_employees(self, out_names: list[str], out_urls: list[str]) -> None:
        await self.page.wait_for_timeout(5000)

        self.log("[step] filter 2nd-degree (simple toolbar click)]")
        ok = await self._click_second_degree_simple(timeout_ms=15_000)
        if not ok:
            await self._shot("2nd-simple-failed")
            raise TimeoutError("Could not select '2nd' from toolbar")
        await self.wait_network_quiet()

        self.log("[step] extract names/urls (paged)")
        await self._extract_data_names_urls(out_names, out_urls)
        self.log(f"[step] extracted {len(out_urls)} urls")

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

        nav = await self._first_present("search_filters_nav")

        btn = None
        if nav:
            for sel in self._sel("companies_pill"):
                cand = nav.locator(sel).first
                try:
                    if await cand.count():
                        btn = cand
                        break
                except Exception:
                    continue

        if not btn:
            for sel in self._sel("companies_pill"):
                cand = p.locator(sel).first
                try:
                    if await cand.count():
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

    async def _open_company_employees(self) -> None:
        p = self.page
        assert p

        try:
            await p.wait_for_selector(
                _join(SELECTORS["company_top_card"]) + ", " + _join(SELECTORS["company_employees_link"]),
                timeout=10_000
            )
        except Exception:
            pass

        last_err: Optional[Exception] = None
        for _ in range(5):
            try:
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

                try:
                    await btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await btn.wait_for(state="visible", timeout=1500)
                except Exception:
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

    async def _open_all_filters(self):
        p = self.page
        assert p

        nav = await self._first_present("search_filters_nav")
        try:
            if nav:
                await nav.scroll_into_view_if_needed()
        except Exception:
            pass

        btn = None
        if nav:
            for sel in self._sel("all_filters_button"):
                cand = nav.locator(sel).first
                if await cand.count():
                    btn = cand
                    break

        if not btn:
            for sel in self._sel("all_filters_button"):
                cand = p.locator(sel).first
                if await cand.count():
                    btn = cand
                    break

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

    async def _wait_connections_ui_ready(self, timeout_ms: int = 10_000):
        p = self.page
        assert p
        await p.wait_for_function(
            """(sels)=>sels.some(s=>document.querySelector(s))""",
            arg=self.CONNECTIONS_CONTAINER_SELECTORS,
            timeout=timeout_ms
        )
        try:
            await p.wait_for_selector(
                "div[role='toolbar'] [role='radio'], ul.search-reusables__multiselect-pill-list button",
                timeout=timeout_ms
            )
        except Exception:
            await p.wait_for_timeout(300)

    async def _verify_2nd_checked(self) -> bool:
        p = self.page
        assert p
        radio = p.locator('div[role="toolbar"] div[role="radio"]').filter(has_text=SECOND_RX).first
        try:
            if await radio.count():
                v = await radio.get_attribute("aria-checked")
                return (v or "").lower() == "true"
        except Exception:
            pass
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
        p = self.page
        assert p
        await self._wait_connections_ui_ready(timeout_ms)
        await self._dismiss_open_popovers()

        try:
            nav = await self._first_present("search_filters_nav")
            if nav:
                await nav.scroll_into_view_if_needed()
                await p.wait_for_timeout(80)
        except Exception:
            pass

        radio_group = p.locator("div[role='toolbar'] [role='radio']")
        radio_2nd = radio_group.filter(has=p.locator("label", has_text=SECOND_RX)).first
        label_2nd = radio_2nd.locator("label", has_text=SECOND_RX).first

        pill_2nd = p.locator("ul.search-reusables__multiselect-pill-list button[aria-label='2nd'], ul.search-reusables__multiselect-pill-list button:has-text('2nd')").first
        generic_2nd = p.locator("button[aria-label='2nd'], button:has-text('2nd')").first

        for target in [radio_2nd, label_2nd, pill_2nd, generic_2nd]:
            try:
                if not await target.count():
                    continue
                try:
                    await target.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await target.wait_for(state="visible", timeout=1500)
                except Exception:
                    pass

                if await self.click_with_retry(target, attempts=3, delay_ms=140):
                    if await self._verify_connections_2nd_selected(wait_ms=1_200):
                        return True

                if target == radio_2nd:
                    try:
                        await target.focus()
                    except Exception:
                        pass
                    try:
                        await p.keyboard.press("Space")
                        await p.wait_for_timeout(220)
                        if await self._verify_connections_2nd_selected(wait_ms=1_200):
                            return True
                    except Exception:
                        pass

                try:
                    await p.evaluate("(el)=>{el.click(); el.dispatchEvent(new MouseEvent('click',{bubbles:true}));}", target)
                    await p.wait_for_timeout(180)
                    if await self._verify_connections_2nd_selected(wait_ms=1_200):
                        return True
                except Exception:
                    pass
            except Exception:
                continue

        return await self._set_2nd_via_all_filters()

    async def _verify_connections_2nd_selected(self, wait_ms: int = 0) -> bool:
        p = self.page
        assert p

        async def _check() -> bool:
            try:
                radios = p.locator("div[role='toolbar'] [role='radio']").filter(
                    has=p.locator("label", has_text=SECOND_RX)
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
                    if any(attrs[key] in ("true", "mixed", "checked", "selected", "on") for key in attrs):
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
                          const hostRadio = el.closest('[role="radio"], [data-view-name="search-filter-top-bar-select"]');
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

    async def _set_2nd_via_all_filters(self) -> bool:
        p = self.page
        assert p
        try:
            dlg = await self._open_all_filters()

            candidates = [
                "input[type='checkbox'][value='S']",
                "label:has-text('2nd') input[type='checkbox']",
                "button[aria-label='2nd']",
                "label:has-text('2nd')",
            ]

            target = None
            for sel in candidates:
                cand = dlg.locator(sel).first
                if await cand.count():
                    target = cand
                    break

            if not target:
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

            try:
                await target.click()
            except Exception:
                try:
                    await p.evaluate("(el)=>el.click()", target)
                except Exception:
                    pass

            apply_btn = dlg.locator("button", has_text=re.compile(r"(show\\s+results|apply)", re.I)).first
            if await apply_btn.count():
                await self.click_with_retry(apply_btn)
            else:
                esc_ok = True
                try:
                    await p.keyboard.press("Escape")
                except Exception:
                    esc_ok = False
                if not esc_ok:
                    try:
                        await p.mouse.click(5, 5)
                    except Exception:
                        pass

            await self.wait_network_quiet()
            return await self._verify_connections_2nd_selected(wait_ms=1_500)
        except Exception:
            return False

    async def _dismiss_open_popovers(self):
        p = self.page
        assert p
        try:
            await p.keyboard.press("Escape")
            await p.wait_for_timeout(100)
        except Exception:
            pass
        try:
            await p.evaluate("""
                () => {
                  document.querySelectorAll('.artdeco-hoverable-content[aria-hidden="false"]')
                    .forEach(el => el.setAttribute('aria-hidden', 'true'));
                  document.querySelectorAll('[aria-expanded="true"][data-view-name="search-filter-top-bar-select"]')
                    .forEach(el => el.setAttribute('aria-expanded','false'));
                }
            """)
        except Exception:
            pass

    def _url_network_has_2nd(self) -> bool:
        try:
            u = (self.page.url or "").lower()
        except Exception:
            return False
        patterns = [
            'network=["s"]', 'network=%5b%22s%22%5d',
            'facetnetwork=["s"]', 'facetnetwork=%5b%22s%22%5d',
            'network=s', 'facetnetwork=s'
        ]
        return any(p in u for p in patterns)
