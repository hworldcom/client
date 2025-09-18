import os, sys, json, logging, requests
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
API_BASE = os.getenv("API_BASE", "https://api.lnlabs.xyz")

def main():
    r = requests.get(f"{API_BASE}/hello", timeout=15)
    r.raise_for_status()
    print(json.dumps(r.json()))
    sys.exit(0)

if __name__ == "__main__":
    main()
