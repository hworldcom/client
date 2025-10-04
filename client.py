# client.py — desktop agent (dummy implementation)
import argparse, os, time, json, threading
import requests

API_BASE = os.environ.get("API_BASE", "https://api.lnlabs.xyz")
TOKEN_FILE = os.environ.get("AGENT_TOKEN_FILE", os.path.expanduser("~/.lnlabs_agent_token"))

def save_token(tok: str):
    with open(TOKEN_FILE, "w") as f:
        f.write(tok.strip())

def load_token() -> str | None:
    try:
        return open(TOKEN_FILE).read().strip()
    except FileNotFoundError:
        return None

def pair(code: str):
    r = requests.post(f"{API_BASE}/agent/register", json={"code": code}, timeout=10)
    r.raise_for_status()
    tok = r.json()["agent_token"]
    save_token(tok)
    print("Paired. Agent token saved.")

def heartbeat_loop(tok: str, stop_evt: threading.Event):
    while not stop_evt.is_set():
        try:
            requests.post(f"{API_BASE}/agent/heartbeat", headers={"X-Agent-Token": tok}, timeout=10)
        except Exception as e:
            pass
        stop_evt.wait(10)

def poll_jobs(tok: str):
    while True:
        try:
            r = requests.get(f"{API_BASE}/agent/jobs", headers={"X-Agent-Token": tok}, timeout=30)
            r.raise_for_status()
            job = r.json().get("job")
            if not job:
                time.sleep(2)
                continue

            job_id = job["id"]
            urls = job["urls"]

            # ---- Dummy "scrape": fabricate mutuals ----
            fake = {}
            for u in urls:
                fake[u] = {
                    "mutuals": ["alice.smith", "bob.jones", "carol.lee"],
                    "count": 3,
                }
            time.sleep(2)

            r2 = requests.post(
                f"{API_BASE}/agent/result",
                headers={"X-Agent-Token": tok, "content-type": "application/json"},
                data=json.dumps({"job_id": job_id, "result": fake}),
                timeout=30,
            )
            r2.raise_for_status()
        except Exception as e:
            time.sleep(3)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="One-time pairing code from the web UI")
    args = ap.parse_args()

    if args.pair:
        pair(args.pair)

    tok = load_token()
    if not tok:
        print("No agent token. Get a pairing code in the web UI, then run: client --pair CODE")
        return

    stop_evt = threading.Event()
    t = threading.Thread(target=heartbeat_loop, args=(tok, stop_evt), daemon=True)
    t.start()
    print("Agent running. Polling for jobs…")
    try:
        poll_jobs(tok)
    finally:
        stop_evt.set()

if __name__ == "__main__":
    main()
