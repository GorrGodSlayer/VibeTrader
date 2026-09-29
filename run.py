"""Start the VibeTrader server.

Usage:
    python run.py                             # settings from .env
    python run.py --db demo.db --port 5001    # one-off overrides (e.g. the seeded demo)
"""
import argparse
import logging

import uvicorn

from vibetrader.config import env

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Start the VibeTrader dashboard and engine.")
    ap.add_argument("--port", type=int, help=f"override PORT (currently {env.port})")
    ap.add_argument("--db", help=f"override DB_PATH (currently {env.db_path})")
    args = ap.parse_args()
    if args.port:
        env.port = args.port
    if args.db:
        env.db_path = args.db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if env.host not in ("127.0.0.1", "localhost") and not env.dashboard_password:
        raise SystemExit("Refusing to bind beyond localhost without DASHBOARD_PASSWORD set.")
    print(f"VibeTrader dashboard: http://{env.host}:{env.port}  (db: {env.db_path})")
    uvicorn.run("vibetrader.api.routes:app", host=env.host, port=env.port, log_level="warning")
