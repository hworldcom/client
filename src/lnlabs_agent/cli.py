# src/lnlabs_agent/cli.py
"""
Headless client (CLI).
- Pair with:  python -m src.lnlabs_agent.cli --pair ABCD1234
- Run agent:  python -m src.lnlabs_agent.cli
Build it with PyInstaller for a one-file console executable if desired.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Optional

from lnlabs_agent.core import (
    pair_with_code,
    load_token,
    save_token,
    clear_token,
    AgentRunner,
    configure_api_base,
    get_api_base,
    known_api_environments,
)

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LNLabs Agent (CLI)")
    ap.add_argument("--pair", help="One-time pairing code from the website")
    ap.add_argument("--unpair", action="store_true", help="Forget saved agent token and exit")
    env_choices = sorted(known_api_environments(primary_only=True).keys())
    if env_choices:
        ap.add_argument("--env", choices=env_choices, help="Select API environment alias")
    else:
        ap.add_argument("--env", help="Select API environment alias")
    ap.add_argument("--api-base", help="Override API base URL (e.g., https://lnlabs-backend.dev.run.app)")
    ap.add_argument("--show-api-base", action="store_true", help="Print the resolved API base and exit")
    args = ap.parse_args(argv)

    try:
        configure_api_base(env=args.env, override=args.api_base)
    except ValueError as e:
        ap.error(str(e))

    if args.show_api_base:
        print(get_api_base())
        return 0

    if args.unpair:
        clear_token()
        print("Unpaired: token removed.")
        return 0

    if args.pair:
        print(f"Using API base: {get_api_base()}")
        tok = pair_with_code(args.pair)
        print("Paired. Agent token saved.")
        return 0

    tok = load_token()
    if not tok:
        print("No agent token. Get a pairing code in the web UI, then run: --pair CODE")
        return 2

    print(f"Using API base: {get_api_base()}")
    runner = AgentRunner(tok, on_log=lambda s: print(s))
    runner.start()

    # graceful shutdown on Ctrl+C
    def _shutdown(*_):
        print("\nStopping agent...")
        runner.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        runner.join()
    except KeyboardInterrupt:
        _shutdown()
        runner.join()
    return 0

if __name__ == "__main__":
    sys.exit(main())
