# src/lnlabs_agent/core.py
"""
Shared client logic: pairing, token storage, heartbeats, job polling, and scraping.
Used by both the CLI and GUI.

Key change: WebCrawler is used via an async context-managed session:
    async with WebCrawler(...).session(headless=False) as c:
        await c.login_if_needed()
        ...

This guarantees browser teardown even on exceptions (no explicit stop()).
"""

from __future__ import annotations

import os
import json
import time
import threading
from typing import Callable, Optional, Dict, List

import asyncio
import platform
from pathlib import Path

import requests
from platformdirs import user_config_dir

# Playwright scraper
from lnlabs_agent.scraper.web_crawler import WebCrawler

# -------------------------
# Config / constants
# -------------------------
APP_NAME = "LNLabsAgent"
VENDOR = "LNLabs"

API_BASE = os.environ.get("API_BASE", "https://api.lnlabs.xyz")

CONF_DIR = user_config_dir(APP_NAME, VENDOR)
TOKEN_FILE = os.path.join(CONF_DIR, "agent_token")
COOKIE_FILE = os.path.join(CONF_DIR, "linkedin_cookies.json")

# Where Playwright should put its browser binaries (per-user cache)
BROWSERS_DIR = os.path.join(CONF_DIR, "pw-browsers")
os.makedirs(BROWSERS_DIR, exist_ok=True)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS_DIR)

# Artifacts (screenshots, etc.)
ARTIFACTS_DIR = os.path.join(CONF_DIR, "artifacts")
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

HEARTBEAT_SEC = 10
JOB_IDLE_SEC = 2

# -------------------------
# Token storage
# -------------------------
def _ensure_conf_dir() -> None:
    os.makedirs(CONF_DIR, exist_ok=True)

def save_token(tok: str) -> None:
    _ensure_conf_dir()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok.strip())

def load_token() -> Optional[str]:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def clear_token() -> None:
    try:
        os.remove(TOKEN_FILE)
    except FileNotFoundError:
        pass

# -------------------------
# HTTP helpers to backend
# -------------------------
def pair_with_code(code: str) -> str:
    """
    Exchange a one-time pairing code (shown in the web UI) for an agent token.
    """
    r = requests.post(f"{API_BASE}/agent/register", json={"code": code}, timeout=20)
    r.raise_for_status()
    tok = r.json()["agent_token"]
    save_token(tok)
    return tok

def send_heartbeat(token: str) -> bool:
    try:
        r = requests.post(
            f"{API_BASE}/agent/heartbeat",
            headers={"X-Agent-Token": token},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception:
        return False

def next_job(token: str) -> Optional[Dict]:
    r = requests.get(
        f"{API_BASE}/agent/jobs",
        headers={"X-Agent-Token": token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("job")

def send_result(token: str, job_id: str, result: dict) -> None:
    """
    Robust result sender with a few retries.
    """
    url = f"{API_BASE}/agent/result"
    payload = {"job_id": job_id, "result": result}
    headers = {"X-Agent-Token": token}

    last_exc = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if 200 <= r.status_code < 300:
                return
            print(f"[result] POST {url} -> {r.status_code}: {r.text[:500]}")
        except Exception as e:
            last_exc = e
            print(f"[result] exception on attempt {attempt+1}: {e}")
        time.sleep(1.5)

    if last_exc:
        raise last_exc
    else:
        raise RuntimeError(f"send_result failed: {r.status_code} {r.text[:500]}")

# -------------------------
# Optional: dummy generator for 'profiles' mode (placeholder)
# -------------------------
def dummy_profiles(urls: List[str]) -> Dict[str, Dict]:
    """
    For each profile URL return some dummy mutuals; if URL contains 'direct', say 1st.
    """
    out = {}
    for u in urls:
        degree = "1st" if "direct" in u else "2nd"
        conns = [
            {"url": f"{u.rstrip('/')}-mutual-1/", "degree": degree},
            {"url": f"{u.rstrip('/')}-mutual-2/", "degree": "3rd" if degree != "1st" else "2nd"},
        ]
        out[u] = {"connections": conns}
    time.sleep(1.5)
    return out

# -------------------------
# Playwright bootstrap
# -------------------------
def ensure_playwright_chromium_installed(log: Callable[[str], None]) -> None:
    """
    Ensure Playwright Chromium is present in our per-user cache.
    """
    os.makedirs(BROWSERS_DIR, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR

    exe = _chromium_exe_from_cache(BROWSERS_DIR)
    if exe:
        log(f"Chromium present at: {exe}")
        return

    try:
        log("Ensuring Playwright Chromium is installed…")
        # Use the Playwright CLI programmatically
        from playwright.__main__ import main as pw_main
        import sys as _sys
        old = list(_sys.argv)
        try:
            _sys.argv = ["playwright", "install", "chromium"]
            pw_main()
        finally:
            _sys.argv = old
    except Exception as e:
        log(f"Playwright install failed: {e}")

    exe = _chromium_exe_from_cache(BROWSERS_DIR)
    log(f"Post-install chromium: {exe or 'NOT FOUND'}")

def _chromium_exe_from_cache(browsers_dir: str) -> Optional[str]:
    """
    Return full path to the Chromium binary inside our per-user browsers cache,
    or None if not found.
    """
    root = Path(browsers_dir)
    if not root.exists():
        return None

    chromium_folders = sorted([p for p in root.iterdir() if p.is_dir() and p.name.startswith("chromium-")])
    if not chromium_folders:
        return None
    croot = chromium_folders[-1]  # pick the newest/last

    sysname = platform.system().lower()
    if sysname == "darwin":   # macOS
        cand = croot / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    elif sysname == "windows":
        cand = croot / "chrome-win" / "chrome.exe"
    else:                     # linux
        cand = croot / "chrome-linux" / "chrome"

    return str(cand) if cand.exists() else None

# -------------------------
# Background agent runner
# -------------------------
class AgentRunner(threading.Thread):
    """
    Single-thread background runner that:
      - Sends periodic heartbeats
      - Polls for jobs
      - Executes scraping logic
      - Returns results
    """

    def __init__(self, token: str, on_log: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True)
        self.token = token
        self.on_log = on_log or (lambda s: None)
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def log(self, msg: str) -> None:
        try:
            self.on_log(msg)
        except Exception:
            pass

    def run(self) -> None:
        last_hb = 0.0
        self.log("AgentRunner started.")
        while not self._stop_evt.is_set():
            now = time.time()
            # heartbeat
            if now - last_hb >= HEARTBEAT_SEC:
                ok = send_heartbeat(self.token)
                self.log(f"Heartbeat: {'ok' if ok else 'failed'}")
                last_hb = now

            # poll for a job
            try:
                job = next_job(self.token)
                if not job:
                    time.sleep(JOB_IDLE_SEC)
                    continue

                job_id = job.get("id")
                urls   = job.get("urls") or []
                mode   = (job.get("mode") or "profiles").lower()
                self.log(f"Job {job_id} received mode={mode} ({len(urls)} items)")

                if mode == "companies":
                    # Ensure chromium is installed in our managed cache
                    ensure_playwright_chromium_installed(self.log)

                    # If still not present, fail the job gracefully
                    chromium_exe = _chromium_exe_from_cache(BROWSERS_DIR)
                    if not chromium_exe:
                        raise RuntimeError(
                            "Chromium was not found after install. Check network/proxy and try again. "
                            f"Browsers dir: {BROWSERS_DIR}"
                        )

                    # Run the scraping flow in an async context-managed session
                    result = asyncio.run(self._companies_flow(urls))
                else:
                    # Placeholder for 'profiles' mode
                    result = dummy_profiles(urls)

                self.log(f"[job {job_id}] sending result …")
                send_result(self.token, job_id, result)
                self.log(f"[job {job_id}] result sent ✅")

            except Exception as e:
                # best-effort error result so the job completes visibly
                try:
                    self.log(f"[job {job_id}] error: {e} — sending failure result")
                except Exception:
                    self.log(f"[job] error before job_id known: {e}")
                fail = {"error": str(e), "ok": False}
                try:
                    send_result(self.token, job_id, fail)  # type: ignore[arg-type]
                    self.log(f"[job {job_id}] failure result sent ❌")
                except Exception as e2:
                    self.log(f"[job {job_id}] could not send failure result: {e2}")

    # ---- async sub-flow for companies mode ----
    async def _companies_flow(self, urls: list[str]) -> dict:
        """
        One job = one browser session. Guarantees teardown via `async with`.
        """
        result: dict[str, dict] = {}
        crawler = WebCrawler(
            logger=self.log,
            artifacts_dir=ARTIFACTS_DIR,
            cookie_file=COOKIE_FILE,
            # You can pass browser_exe if you want to force a specific binary:
            # browser_exe=_chromium_exe_from_cache(BROWSERS_DIR),
        )
        async with crawler.session(headless=False):
            await crawler.login_if_needed()
            for comp in urls:
                try:
                    names, links = await crawler.start_company_flow(comp)
                    result[comp] = {"employees": links, "count": len(links)}
                except Exception as e:
                    self.log(f"[job] company={comp} error: {e}")
                    result[comp] = {"error": str(e), "employees": []}
        return result
