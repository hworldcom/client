import os, sys, json, logging, requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("client")

API_BASE = os.getenv("API_BASE", "https://api.yourdomain.com")
API_TOKEN = os.getenv("API_TOKEN")  # don't hardcode; provide at runtime

def call_backend():
    url = f"{API_BASE}/hello"
    headers = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
    log.info(f"Calling {url}")
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def main():
    try:
        data = call_backend()
        print(json.dumps(data))
        sys.exit(0)
    except Exception as e:
        log.error(f"Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
