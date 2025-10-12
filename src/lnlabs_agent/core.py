# src/lnlabs_agent/core.py
"""
Shared client logic: pairing, token storage, heartbeats, job polling, and dummy results.
Used by both the CLI and GUI.
"""

from __future__ import annotations

import os
import json
import time
import threading
from typing import Callable, Optional, Dict, List

import requests
from platformdirs import user_config_dir

# -------------------------
# Config / constants
# -------------------------
APP_NAME = "LNLabsAgent"
VENDOR = "LNLabs"

API_BASE = os.environ.get("API_BASE", "https://api.lnlabs.xyz")

CONF_DIR = user_config_dir(APP_NAME, VENDOR)
TOKEN_FILE = os.path.join(CONF_DIR, "agent_token")

HEARTBEAT_SEC = 10
JOB_IDLE_SEC = 2


# --- DUMMY GENERATORS -------------------------------------------------

def _slugify_company(s: str) -> str:
    s = s.strip().lower()
    if s.startswith("http"):
        # extract last non-empty path segment
        try:
            from urllib.parse import urlparse
            p = urlparse(s)
            parts = [x for x in p.path.split("/") if x]
            if parts:
                s = parts[-1]
        except Exception:
            pass
    return s.replace(" ", "-")

def dummy_companies(urls: list[str]) -> dict:
    """
    For each input company (name or URL) return a couple of dummy profile URLs.
    """
    out = {}
    for raw in urls:
        slug = _slugify_company(raw)
        out[raw] = {
            "employees": [
                f"https://www.linkedin.com/in/{slug}-employee-a/",
                f"https://www.linkedin.com/in/{slug}-employee-b/",
            ]
        }
    time.sleep(1.5)
    return out

def dummy_profiles(urls: list[str]) -> dict:
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
# Token storage
# -------------------------
def _ensure_conf_dir() -> None:
    os.makedirs(CONF_DIR, exist_ok=True)

def save_token(tok: str) -> None:
    _ensure_conf_dir()
    with open(TOKEN_FILE, "w") as f:
        f.write(tok.strip())

def load_token() -> Optional[str]:
    try:
        with open(TOKEN_FILE, "r") as f:
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

def send_result(token: str, job_id: str, result: Dict) -> None:
    r = requests.post(
        f"{API_BASE}/agent/result",
        headers={"X-Agent-Token": token, "content-type": "application/json"},
        data=json.dumps({"job_id": job_id, "result": result}),
        timeout=30,
    )
    r.raise_for_status()

# -------------------------
# Dummy "scrape" (placeholder)
# -------------------------
def dummy_mutuals(urls: List[str]) -> Dict[str, Dict]:
    """
    Fabricate mutuals for each profile URL. Replace this with real logic later.
    """
    # simulate a bit of work
    time.sleep(2)
    return {
        u: {"mutuals": ["alice.smith", "bob.jones", "carol.lee"], "count": 3}
        for u in urls
    }

# -------------------------
# Background agent runner
# -------------------------
class AgentRunner(threading.Thread):

    """
    Single-thread background runner that:
      - Sends periodic heartbeats
      - Polls for jobs
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
                mode   = (job.get("mode") or job.get("type") or "profiles").lower()
                self.log(f"Job {job_id} received mode={mode} ({len(urls)} items)")

                if mode == "companies":
                    result = dummy_companies(urls)
                else:  # "profiles" (default)
                    result = dummy_profiles(urls)

                send_result(self.token, job_id, result)
                self.log(f"Job {job_id} done")
            except Exception as e:
                self.log(f"Job loop error: {e}")
                time.sleep(3)
        self.log("AgentRunner stopped.")
