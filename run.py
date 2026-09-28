"""Boot VibeTrader 95. Usage: python run.py"""
import logging

import uvicorn

from vibetrader.config import env

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if env.host not in ("127.0.0.1", "localhost") and not env.dashboard_password:
        raise SystemExit("Refusing to bind beyond localhost without DASHBOARD_PASSWORD set.")
    print(r"""
 __     ___ _          _____              _             ___  ____
 \ \   / (_) |__   ___|_   _| __ __ _  __| | ___ _ __  / _ \| ___|
  \ \ / /| | '_ \ / _ \ | || '__/ _` |/ _` |/ _ \ '__|| (_) |___ \
   \ V / | | |_) |  __/ | || | | (_| | (_| |  __/ |    \__, |___) |
    \_/  |_|_.__/ \___| |_||_|  \__,_|\__,_|\___|_|      /_/|____/
""")
    print(f"  >> open http://{env.host}:{env.port}  (not financial advice. not even advice.)\n")
    uvicorn.run("vibetrader.api.routes:app", host=env.host, port=env.port, log_level="warning")
