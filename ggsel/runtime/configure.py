from __future__ import annotations

import getpass
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "config.json"


def required(prompt: str, *, secret: bool = False) -> str:
    read = getpass.getpass if secret else input
    while True:
        value = read(prompt).strip()
        if value:
            return value
        print("Value is required.")


def positive_integer(prompt: str) -> int:
    while True:
        value = required(prompt)
        try:
            number = int(value)
        except ValueError:
            number = 0
        if number > 0:
            return number
        print("Enter a positive integer.")


def defaulted(prompt: str, default: str) -> str:
    return input(f"{prompt} [{default}]: ").strip() or default


def main() -> int:
    if OUTPUT.exists():
        answer = input("config.json already exists. Replace it? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Existing configuration kept.")
            return 0

    print("Configure the GGSel Seller runtime.")
    connection_token = required("Buywell connection key: ", secret=True)
    seller_id = positive_integer("GGSel seller ID: ")
    api_key = required("GGSel API key with V1 orders/chats access: ", secret=True)
    buywell_url = defaulted("Buywell API URL", "https://buywell.pro/api")
    ggsel_api_url = defaulted(
        "GGSel API URL", "https://seller.ggsel.com/api_sellers/api"
    )
    config = {
        "buywell_url": buywell_url,
        "connection_token": connection_token,
        "seller_id": seller_id,
        "api_key": api_key,
        "ggsel_api_url": ggsel_api_url,
        "database_path": "state/ggsel-runtime.sqlite3",
        "poll_interval_seconds": 30,
        "message_poll_interval_seconds": 10,
        "sales_window": 100,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 30,
        "emit_existing_on_first_start": False,
        "log_level": "INFO",
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, OUTPUT)
    try:
        OUTPUT.chmod(0o600)
    except OSError:
        pass
    print(f"Configuration saved to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
