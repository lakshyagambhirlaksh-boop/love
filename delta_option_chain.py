"""Show a live Delta Exchange India option chain, refreshing every 2 seconds.

Read-only public market data; this script does not place orders.
"""

import argparse
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_URL = "https://api.india.delta.exchange/v2/tickers"


def get_chain(asset, expiry=None):
    params = {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": asset,
    }
    if expiry:
        params["expiry_date"] = expiry
    request = Request(f"{API_URL}?{urlencode(params)}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not payload.get("success"):
        raise RuntimeError(f"Delta API returned an unsuccessful response: {payload}")
    return payload.get("result", [])


def nearest_expiry(rows):
    """Infer the nearest listed expiry from symbols ending in DDMMYY."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    dates = set()
    for row in rows:
        symbol = row.get("symbol", "")
        suffix = symbol.rsplit("-", 1)[-1]
        if len(suffix) == 6 and suffix.isdigit():
            try:
                expiry_date = datetime.strptime(suffix, "%d%m%y").date()
                if expiry_date >= today:
                    dates.add(expiry_date)
            except ValueError:
                pass
    return min(dates) if dates else None


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def display(rows, asset, expiry, strikes_each_side):
    if not rows:
        print("No option contracts returned.")
        return

    spot = next((num(row.get("spot_price")) for row in rows if num(row.get("spot_price")) is not None), None)
    rows = [row for row in rows if num(row.get("strike_price")) is not None]
    strikes = sorted({num(row["strike_price"]) for row in rows})
    if spot is not None and strikes:
        atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        low = max(0, atm_index - strikes_each_side)
        high = min(len(strikes), atm_index + strikes_each_side + 1)
        selected = set(strikes[low:high])
        rows = [row for row in rows if num(row["strike_price"]) in selected]

    print("\033[2J\033[H", end="")
    print(f"Delta Exchange India | {asset} | expiry {expiry} | {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}")
    print(f"Spot: {spot:,.2f}" if spot is not None else "Spot: unavailable")
    print("\n   Type       Strike       Bid       Ask      Mark      Delta       IV       OI")
    for row in sorted(rows, key=lambda item: (num(item.get("strike_price")) or 0, item.get("contract_type", ""))):
        quote = row.get("quotes") or {}
        greeks = row.get("greeks") or {}
        kind = "CALL" if row.get("contract_type") == "call_options" else "PUT"
        values = [
            num(row.get("strike_price")), num(quote.get("best_bid")),
            num(quote.get("best_ask")), num(row.get("mark_price")),
            num(greeks.get("delta")), num(quote.get("mark_iv") or row.get("mark_vol")),
            num(row.get("oi")),
        ]
        rendered = [f"{v:,.4f}" if v is not None else "-" for v in values]
        print(f"{kind:>7} {rendered[0]:>11} {rendered[1]:>9} {rendered[2]:>9} {rendered[3]:>9} {rendered[4]:>10} {rendered[5]:>8} {rendered[6]:>9}")
    print("\nRefresh: every 2 seconds | Ctrl+C to stop | public data only")


def main():
    parser = argparse.ArgumentParser(description="Live read-only Delta Exchange India option chain")
    parser.add_argument("--asset", choices=("BTC", "ETH"), default="BTC", help="underlying (default: BTC)")
    parser.add_argument("--expiry", help="expiry as DD-MM-YYYY; default: nearest listed expiry")
    parser.add_argument("--strikes", type=int, default=8, help="strikes to show on each side of ATM (default: 8)")
    args = parser.parse_args()
    if args.strikes < 0:
        parser.error("--strikes must be zero or greater")

    expiry = args.expiry
    while True:
        try:
            rows = get_chain(args.asset, expiry)
            if not expiry:
                nearest = nearest_expiry(rows)
                if nearest:
                    expiry = nearest.strftime("%d-%m-%Y")
                    rows = get_chain(args.asset, expiry)
            display(rows, args.asset, expiry or "all", args.strikes)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"Data fetch failed: {exc}")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
