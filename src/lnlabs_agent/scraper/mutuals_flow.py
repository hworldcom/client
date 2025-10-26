"""
Mutual connections scraping routines extracted from web_crawler.py.
"""
from __future__ import annotations

import re
from typing import Optional

from .selectors import _ms


class MutualConnectionsMixin:
    async def scrape_mutual_connections(
        self,
        profile_url: str,
        limit: Optional[int] = None,
    ) -> list[dict]:
        self.log(f"[mutuals] profile={profile_url}")
        p = self.page
        assert p
        ok = await self.safe_goto(profile_url, max_retries=3)
        if not ok:
            raise RuntimeError(f"Failed to load profile: {profile_url}")

        await self.wait_network_quiet()
        await p.wait_for_timeout(_ms(0.6, 1.2))
        try:
            await p.wait_for_selector("main", timeout=8_000)
        except Exception:
            pass
        await self._shot("profile-opened")

        btn = await self._first_present("profile_mutuals_link")
        if not btn:
            self.log("[mutuals] mutual connections control not found.")
            await self._shot("mutuals-link-missing")
            return []

        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await btn.wait_for(state="visible", timeout=4_000)
        except Exception:
            pass

        if not await self.click_with_retry(btn, attempts=3, delay_ms=200):
            self.log("[mutuals] click_on_mutuals_failed")
            await self._shot("mutuals-click-failed")
            return []

        await p.wait_for_timeout(_ms(1.5, 2.8))
        try:
            await self.wait_network_quiet()
        except Exception:
            pass
        await self._shot("mutuals-opened")

        names: list[str] = []
        urls: list[str] = []
        try:
            await self._extract_data_names_urls(names, urls, limit=limit)
        except Exception as e:
            self.log(f"[mutuals] extraction error: {e}")

        rows: list[dict] = []
        for name, url in zip(names, urls):
            if not url:
                continue
            rows.append(
                {
                    "name": name,
                    "url": url,
                    "source_profile": profile_url,
                }
            )
            if limit and len(rows) >= limit:
                break

        if not rows:
            fallback = await self._collect_mutuals_from_modal(profile_url, limit=limit)
            if fallback:
                rows = fallback

        self.log(f"[mutuals] collected {len(rows)} connections")
        return rows

    async def _collect_mutuals_from_modal(
        self,
        profile_url: str,
        limit: Optional[int] = None,
    ) -> list[dict]:
        p = self.page
        assert p
        modal = p.locator("div[role='dialog']").filter(
            has_text=re.compile(r"Mutual", re.I)
        ).first
        try:
            if not await modal.count():
                return []
        except Exception:
            return []

        rows: list[dict] = []
        seen: set[str] = set()
        try:
            anchors = await modal.locator("a[href*='/in/']").all()
        except Exception:
            anchors = []

        for anchor in anchors:
            try:
                href = await anchor.get_attribute("href")
                if not href:
                    continue
                href = href.strip()
                if href.startswith("/"):
                    href = self.URL.rstrip("/") + href
                href = href.split("?", 1)[0].split("#", 1)[0]
                if href in seen:
                    continue
                seen.add(href)
                name = (await anchor.text_content() or "").strip() or "(unknown)"
                rows.append(
                    {
                        "name": name,
                        "url": href,
                        "source_profile": profile_url,
                    }
                )
                if limit and len(rows) >= limit:
                    break
            except Exception:
                continue

        if rows:
            self.log(f"[mutuals] modal fallback collected {len(rows)} entries")
            await self._shot("mutuals-modal-collected")
        return rows
