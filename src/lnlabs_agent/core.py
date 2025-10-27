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
import sys
import tempfile
import zipfile
import shutil
import uuid
from collections import deque
from typing import Callable, Optional, Dict, List, Any

import asyncio
import platform
import subprocess
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

DEFAULT_PROD_API_BASE = "https://api.lnlabs.xyz"
_API_BASE = os.environ.get("API_BASE") or DEFAULT_PROD_API_BASE

# Map of environment aliases -> base URL. We keep a few synonyms for production
# but expose only unique entries in the UI/CLI.
_ENV_ALIAS_MAP: dict[str, str] = {
    "prod": DEFAULT_PROD_API_BASE,
    "production": DEFAULT_PROD_API_BASE,
    "main": DEFAULT_PROD_API_BASE,
}

_API_BASE_DEV = os.environ.get("API_BASE_DEV")
if _API_BASE_DEV:
    for key in ("dev", "development", "staging", "test"):
        _ENV_ALIAS_MAP.setdefault(key, _API_BASE_DEV)


def get_api_base() -> str:
    """Return the currently configured API base URL."""
    return _API_BASE.rstrip("/")


def configure_api_base(*, env: Optional[str] = None, override: Optional[str] = None) -> str:
    """
    Configure the global API base.
    env: logical environment name (e.g., dev, prod) mapped via _ENV_ALIAS_MAP.
    override: explicit URL string. Takes precedence over env.
    """
    global _API_BASE
    if override:
        url = override.strip()
        if not url:
            raise ValueError("API base override cannot be empty.")
        _API_BASE = url
    elif env:
        key = env.lower()
        if key not in _ENV_ALIAS_MAP:
            raise ValueError(f"Unknown environment '{env}'. Available: {sorted(_ENV_ALIAS_MAP)}")
        _API_BASE = _ENV_ALIAS_MAP[key]
    return get_api_base()


def known_api_environments(primary_only: bool = True) -> dict[str, str]:
    """Return mapping of environment aliases to base URLs.

    If primary_only is True (default), deduplicates aliases so only the first
    alias for each unique URL is returned. When False, all aliases are included.
    """
    if not primary_only:
        return dict(_ENV_ALIAS_MAP)

    primary: dict[str, str] = {}
    seen: set[str] = set()
    for alias, url in _ENV_ALIAS_MAP.items():
        norm = url.rstrip("/")
        if norm in seen:
            continue
        primary[alias] = url
        seen.add(norm)
    return primary

def _api_url(path: str) -> str:
    base = get_api_base()
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def current_api_environment(default: str = "prod") -> str:
    """Best-effort alias describing which environment is currently active."""
    current = get_api_base().rstrip("/")
    for alias, url in _ENV_ALIAS_MAP.items():
        if url.rstrip("/") == current:
            return alias
    return default

CONF_DIR = user_config_dir(APP_NAME, VENDOR)
TOKEN_FILE = os.path.join(CONF_DIR, "agent_token")
COOKIE_FILE = os.path.join(CONF_DIR, "linkedin_cookies.json")

# Where Playwright should put its browser binaries (per-user cache)
BROWSERS_DIR = os.environ.get("LNLABS_BROWSERS_DIR") or os.path.join(CONF_DIR, "pw-browsers")
os.makedirs(BROWSERS_DIR, exist_ok=True)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS_DIR)

# Artifacts (screenshots, etc.)
ARTIFACTS_DIR = os.path.join(CONF_DIR, "artifacts")
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

HEARTBEAT_SEC = 10
JOB_IDLE_SEC = 2
LOG_HISTORY_LIMIT = 2000

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
    r = requests.post(_api_url("/agent/register"), json={"code": code}, timeout=20)
    r.raise_for_status()
    tok = r.json()["agent_token"]
    save_token(tok)
    return tok

def send_heartbeat(token: str) -> bool:
    try:
        r = requests.post(
            _api_url("/agent/heartbeat"),
            headers={"X-Agent-Token": token},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception:
        return False

def next_job(token: str) -> Optional[Dict]:
    r = requests.get(
        _api_url("/agent/jobs"),
        headers={"X-Agent-Token": token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("job")

def send_result(token: str, job_id: str, result: dict) -> None:
    """
    Robust result sender with a few retries.
    """
    url = _api_url("/agent/result")
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


def send_diagnostic_report(
    token: str,
    *,
    job_id: str,
    mode: str,
    urls: List[str],
    summary: str,
    bundle_path: Path,
) -> None:
    """
    Upload a diagnostic bundle (zip) to the backend so the server can notify developers.
    """
    url = _api_url("/agent/report")
    headers = {"X-Agent-Token": token}
    data = {
        "job_id": job_id,
        "mode": mode,
        "summary": summary,
        "urls": json.dumps(urls),
    }

    try:
        with bundle_path.open("rb") as fh:
            files = {"bundle": (bundle_path.name, fh, "application/zip")}
            r = requests.post(url, data=data, files=files, headers=headers, timeout=60)
            if 200 <= r.status_code < 300:
                return
            print(f"[diagnostic] upload failed: {r.status_code} {r.text[:500]}")
    except Exception as exc:
        print(f"[diagnostic] exception while uploading: {exc}")


def cleanup_artifacts_dir(logger: Optional[Callable[[str], None]] = None) -> None:
    """
    Remove per-job artifact directories and screenshots so future runs start clean.
    """
    path = Path(ARTIFACTS_DIR)
    if not path.exists():
        return

    try:
        for child in path.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception as exc:
                if logger:
                    logger(f"[artifacts] cleanup error for {child}: {exc}")
        path.mkdir(parents=True, exist_ok=True)
        if logger:
            logger(f"[artifacts] cleared {path}")
    except Exception as exc:
        if logger:
            logger(f"[artifacts] cleanup failed: {exc}")

def _safe_slug(text: str, fallback: str = "item") -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in text)
    slug = "-".join(part for part in slug.split("-") if part)
    slug = slug[:40]
    return slug or fallback


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_diagnostic_bundle(job_dir: Path, metadata: Dict[str, Any], log_lines: List[str]) -> Optional[Path]:
    """
    Persist metadata & logs into the job directory and return a zipped archive path.
    """
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        _write_text(job_dir / "metadata.json", json.dumps(metadata, indent=2))
        _write_text(job_dir / "agent.log", "\n".join(log_lines))

        bundle_path = Path(
            tempfile.gettempdir()
        ) / f"lnlabs-report-{metadata.get('job_id','unknown')}-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in job_dir.rglob("*"):
                if item.is_file():
                    zf.write(item, arcname=item.relative_to(job_dir))
        return bundle_path
    except Exception as exc:
        print(f"[diagnostic] could not build bundle: {exc}")
        return None

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
        from playwright._impl._driver import compute_driver_executable

        node_path, cli_path = compute_driver_executable()
        cmd = [node_path, cli_path, "install", "chromium"]
        log(f"[playwright] running: {' '.join(cmd)}")

        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ},
        ) as proc:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if line:
                    log(f"[playwright] {line}")
            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"Playwright install exited with code {code}")
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
        self._log_history: deque[str] = deque(maxlen=LOG_HISTORY_LIMIT)

    def stop(self) -> None:
        self._stop_evt.set()

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"{ts} {msg}"
        self._log_history.append(entry)
        try:
            self.on_log(msg)
        except Exception:
            pass

    def _log_snapshot(self) -> List[str]:
        return list(self._log_history)

    def _create_job_dir(self, job_id: str) -> Path:
        safe_job = _safe_slug(job_id or "job", fallback="job")
        ts = time.strftime("%Y%m%d-%H%M%S")
        job_dir = Path(ARTIFACTS_DIR) / safe_job / ts
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def _submit_diagnostics(
        self,
        *,
        job_id: str,
        mode: str,
        urls: List[str],
        issues: List[Dict[str, Any]],
        job_dir: Path,
    ) -> None:
        if not issues:
            return

        metadata: Dict[str, Any] = {
            "job_id": job_id,
            "mode": mode,
            "urls": urls,
            "issues": issues,
            "log_entries": len(self._log_history),
            "created_at": int(time.time()),
        }
        bundle = _build_diagnostic_bundle(job_dir, metadata, self._log_snapshot())
        if not bundle:
            self.log(f"[diagnostic] skipped for job {job_id} (bundle build failed)")
            return

        summary = "; ".join(f"{issue.get('item')}: {issue.get('error')}" for issue in issues)
        try:
            send_diagnostic_report(
                self.token,
                job_id=job_id,
                mode=mode,
                urls=urls,
                summary=summary,
                bundle_path=bundle,
            )
            self.log(f"[diagnostic] report sent for job {job_id}")
        finally:
            try:
                bundle.unlink(missing_ok=True)  # type: ignore[arg-type]
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

                job_id = str(job.get("id") or uuid.uuid4().hex)
                urls = [str(u) for u in (job.get("urls") or [])]
                mode = (job.get("mode") or "profiles").lower()
                limit_raw = job.get("limit")
                limit = None
                if limit_raw is not None:
                    try:
                        limit = int(limit_raw)
                    except (TypeError, ValueError):
                        limit = None
                limit_note = f", limit={limit}" if limit is not None else ""
                self.log(f"Job {job_id} received mode={mode} ({len(urls)} items{limit_note})")

                job_dir = self._create_job_dir(job_id)
                issues: List[Dict[str, Any]] = []

                try:
                    if mode == "companies":
                        ensure_playwright_chromium_installed(self.log)
                        chromium_exe = _chromium_exe_from_cache(BROWSERS_DIR)
                        if not chromium_exe:
                            raise RuntimeError(
                                "Chromium was not found after install. Check network/proxy and try again. "
                                f"Browsers dir: {BROWSERS_DIR}"
                            )
                        result, flow_issues = asyncio.run(self._companies_flow(job_id, urls, job_dir))
                    else:
                        result, flow_issues = asyncio.run(
                            self._profiles_flow(job_id, urls, job_dir, limit=limit)
                        )
                    if flow_issues:
                        issues.extend(flow_issues)

                    self.log(f"[job {job_id}] sending result …")
                    send_result(self.token, job_id, result)
                    self.log(f"[job {job_id}] result sent ✅")

                except Exception as e:
                    err_msg = str(e)
                    issue: Dict[str, Any] = {"item": "job", "error": err_msg}
                    try:
                        import traceback as _traceback  # local import to avoid top-level dependency if unused

                        issue["traceback"] = _traceback.format_exc()
                    except Exception:
                        pass
                    issues.append(issue)
                    try:
                        self.log(f"[job {job_id}] error: {err_msg} — sending failure result")
                    except Exception:
                        self.log(f"[job] error before job_id known: {err_msg}")
                    fail = {"error": err_msg, "ok": False}
                    try:
                        send_result(self.token, job_id, fail)  # type: ignore[arg-type]
                        self.log(f"[job {job_id}] failure result sent ❌")
                    except Exception as e2:
                        self.log(f"[job {job_id}] could not send failure result: {e2}")
                finally:
                    try:
                        self._submit_diagnostics(
                            job_id=job_id,
                            mode=mode,
                            urls=urls,
                            issues=issues,
                            job_dir=job_dir,
                        )
                    except Exception as diag_exc:
                        self.log(f"[diagnostic] failed to submit for job {job_id}: {diag_exc}")
            except Exception as e:
                self.log(f"[runner] unexpected error while polling jobs: {e}")
                time.sleep(2)

    # ---- async sub-flow for companies mode ----
    async def _companies_flow(
        self,
        job_id: str,
        urls: list[str],
        job_dir: Path,
    ) -> tuple[dict[str, dict], List[Dict[str, Any]]]:
        """
        One job = one browser session. Guarantees teardown via `async with`.
        """
        result: dict[str, dict] = {}
        issues: List[Dict[str, Any]] = []
        crawler = WebCrawler(
            logger=self.log,
            artifacts_dir=str(job_dir),
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
                    err_msg = str(e)
                    self.log(f"[job] company={comp} error: {err_msg}")
                    issues.append({"item": comp, "error": err_msg})
                    try:
                        await crawler.capture_failure_artifacts(f"{job_id}-company-{comp}")
                    except Exception as diag_exc:
                        self.log(f"[diagnostic] capture failure (company) {comp}: {diag_exc}")
                    result[comp] = {"error": err_msg, "employees": []}
        return result, issues

    async def _profiles_flow(
        self,
        job_id: str,
        urls: list[str],
        job_dir: Path,
        limit: Optional[int] = None,
    ) -> tuple[dict[str, dict], List[Dict[str, Any]]]:
        """
        Scrape mutual connections for each profile URL supplied.
        """
        result: dict[str, dict] = {}
        issues: List[Dict[str, Any]] = []
        crawler = WebCrawler(
            logger=self.log,
            artifacts_dir=str(job_dir),
            cookie_file=COOKIE_FILE,
        )
        async with crawler.session(headless=False):
            await crawler.login_if_needed()
            for profile in urls:
                try:
                    connections = await crawler.scrape_mutual_connections(profile, limit=limit)
                    result[profile] = {
                        "connections": connections,
                        "count": len(connections),
                    }
                except Exception as e:
                    err_msg = str(e)
                    self.log(f"[job] profile={profile} error: {err_msg}")
                    issues.append({"item": profile, "error": err_msg})
                    try:
                        await crawler.capture_failure_artifacts(f"{job_id}-profile-{profile}")
                    except Exception as diag_exc:
                        self.log(f"[diagnostic] capture failure (profile) {profile}: {diag_exc}")
                    result[profile] = {"error": err_msg, "connections": []}
        return result, issues
