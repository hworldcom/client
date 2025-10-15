# src/lnlabs_agent/core.py
"""
Shared client logic: pairing, token storage, heartbeats, job polling, and scraping.
Used by both the CLI and GUI.
"""

from __future__ import annotations

import os
import sys
import json
import time
import threading
from typing import Callable, Optional, Dict, List

import subprocess
import requests
from platformdirs import user_config_dir
import platform
from pathlib import Path

# -------------------------
# Async helper for threads
# -------------------------
import asyncio

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

# NEW: where Playwright should put its browser binaries
BROWSERS_DIR = os.path.join(CONF_DIR, "pw-browsers")

# Make sure Playwright honors that location (must be set before Playwright is used)
os.makedirs(BROWSERS_DIR, exist_ok=True)
artifacts_dir = os.path.join(CONF_DIR, "artifacts")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS_DIR)

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

def run_coro(coro):
    """
    Run an async coroutine inside a dedicated event loop (safe inside threads).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # allow pending tasks to finalize gracefully
        try:
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            pass
        loop.close()


# -------------------------
# Optional: dummy generators (kept for 'profiles' mode placeholder)
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
    os.makedirs(BROWSERS_DIR, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR

    # If we already have a valid executable, skip install
    exe = _chromium_exe_from_cache(BROWSERS_DIR)
    if exe:
        log(f"Chromium present at: {exe}")
        return

    try:
        log("Ensuring Playwright Chromium is installed…")
        from playwright.__main__ import main as pw_main
        import sys as _sys
        old = list(_sys.argv)
        try:
            _sys.argv = ["playwright", "install", "chromium"]
            pw_main()  # reads sys.argv
        finally:
            _sys.argv = old
    except Exception as e:
        log(f"Playwright install failed: {e}")

    # Log what we found after install
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

    # Find a chromium-* folder
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

def send_result(token: str, job_id: str, result: dict) -> None:
    url = f"{API_BASE}/agent/result"
    payload = {"job_id": job_id, "result": result}
    headers = {"X-Agent-Token": token}

    last_exc = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code // 100 == 2:
                return
            # log non-2xx
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
                    ensure_playwright_chromium_installed(self.log)

                    os.makedirs(CONF_DIR, exist_ok=True)
                    chromium_exe = _chromium_exe_from_cache(BROWSERS_DIR)  # <— NEW
                    if not chromium_exe:
                        # Give a clear message and abort this job gracefully
                        raise RuntimeError(
                            "Chromium was not found after install. Check network/proxy and try again. "
                            f"Browsers dir: {BROWSERS_DIR}"
                        )
                    result = asyncio.run(self._companies_flow(urls))
                else:
                    # Placeholder for profiles, keep dummy for now
                    result = dummy_profiles(urls)


                self.log(f"[job {job_id}] sending result …")
                send_result(self.token, job_id, result)
                self.log(f"[job {job_id}] result sent ✅")
            except Exception as e:
                self.log(f"[job {job_id}] error: {e} — sending failure result")
                fail = {"error": str(e), "ok": False}
                try:
                    send_result(self.token, job_id, fail)
                    self.log(f"[job {job_id}] failure result sent ❌")
                except Exception as e2:
                    self.log(f"[job {job_id}] could not send failure result: {e2}")

    # helper so we can use asyncio within thread
    async def _companies_flow(self, urls: list[str]) -> dict:
        result: dict[str, dict] = {}
        crawler = WebCrawler(logger=self.log, artifacts_dir=".artifacts")
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
