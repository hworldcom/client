# secure_cookies.py — drop-in cookie helpers

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Optional, Callable

class SecureCookieMixin:
    """
    Mixin providing secure cookie load/save with:
      - OS-appropriate full path logging
      - parent dir creation
      - POSIX 0600 perms best-effort
    Expects:
      - self.COOKIE_FILE: str path to cookie JSON file
      - self.context: Playwright BrowserContext (has .cookies() / .add_cookies())
      - self.log: Callable[[str], None] (optional; defaults to no-op)
    """

    # Optional: override in your class
    log: Callable[[str], None] = lambda *_args, **_kw: None

    async def _load_cookies(self) -> None:
        """Load cookies from JSON into the current Playwright context."""
        assert getattr(self, "context", None) is not None, "context must be set"
        cookie_path = Path(self.COOKIE_FILE).expanduser().resolve()
        self.log(f"[paths] cookies file: {cookie_path}")

        if not cookie_path.exists():
            self.log("[cookies] no cookie file found; continuing without")
            return

        try:
            with cookie_path.open("r", encoding="utf-8") as f:
                cookies = json.load(f)
            await self.context.add_cookies(cookies)
            self.log(f"[cookies] loaded {len(cookies)} cookies")
        except Exception as e:
            self.log(f"[cookies] load failed: {e}")

    async def _save_cookies(self) -> None:
        """Save cookies from the current Playwright context to JSON with strict perms."""
        assert getattr(self, "context", None) is not None, "context must be set"
        cookie_path = Path(self.COOKIE_FILE).expanduser().resolve()
        cookie_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            cookies = await self.context.cookies()
            with cookie_path.open("w", encoding="utf-8") as f:
                json.dump(cookies, f)

            # Best-effort: POSIX owner read/write only (0600).
            # On Windows this is a no-op and safe to ignore.
            try:
                os.chmod(cookie_path, 0o600)
            except Exception:
                pass

            self.log(f"[cookies] saved to: {cookie_path}")
        except Exception as e:
            self.log(f"[cookies] save failed: {e}")
