"""Delta Exchange India BTC/XAUT options dashboard with explicit live controls."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))
IST = ZoneInfo("Asia/Kolkata")
API_BASE = "https://api.india.delta.exchange"
REFRESH_MS = 3000


def api_credential(name: str) -> str:
    """Read credentials from local environment variables or Streamlit secrets."""
    try:
        value = str(st.secrets.get(name, "")).strip()
        if value:
            return value
    except Exception:
        pass
    return os.getenv(name, "").strip()


class DeltaAPIError(RuntimeError):
    def __init__(self, message: str, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.context = context or {}


class DeltaClient:
    """Small Delta REST client. API secrets are never included in messages."""

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._thread_sessions = threading.local()
        self._settlement_times: dict[int, datetime] = {}
        self._settlement_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": f"python-{sys.version_info.major}.{sys.version_info.minor}"})
            self._thread_sessions.session = session
        return session

    def _reset_session(self) -> None:
        """Drop a stale keep-alive connection before a safe read retry."""
        session = getattr(self._thread_sessions, "session", None)
        if session is not None:
            session.close()
            del self._thread_sessions.session

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and "put_your_" not in self.api_key and "put_your_" not in self.api_secret)

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None, signed: bool = False) -> dict[str, Any]:
        params = params or {}
        query = urlencode(params, doseq=True)
        path_query = path + (f"?{query}" if query else "")
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        if signed and not self.configured:
            raise DeltaAPIError("API credentials are missing from .env or Streamlit secrets")
        # GET requests are safe to retry after a broken keep-alive/TLS connection.
        # Never blindly retry order POSTs: a timeout can happen after Delta accepted
        # the order, and a second POST could create a duplicate position.
        safe_read = method.upper() == "GET"
        attempts = 3 if safe_read else (2 if signed else 1)
        for attempt in range(attempts):
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if signed:
                timestamp = str(int(time.time()))
                message = method.upper() + timestamp + path_query + body_text
                signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
                headers.update({"api-key": self.api_key, "timestamp": timestamp, "signature": signature})
            try:
                response = self._session().request(
                    method.upper(), API_BASE + path_query,
                    data=body_text if body is not None else None,
                    headers=headers, timeout=(5, 10),
                )
            except requests.RequestException as exc:
                if safe_read and attempt + 1 < attempts:
                    self._reset_session()
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise DeltaAPIError("Could not reach Delta Exchange") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise DeltaAPIError(f"Delta returned HTTP {response.status_code} with an unreadable response") from exc
            if response.status_code >= 400 or not payload.get("success", False):
                error = payload.get("error", {})
                code = error.get("code", f"HTTP {response.status_code}") if isinstance(error, dict) else f"HTTP {response.status_code}"
                context = error.get("context", {}) if isinstance(error, dict) else {}
                if signed and attempt == 0 and str(code).lower() == "expired_signature":
                    continue
                if safe_read and attempt + 1 < attempts and response.status_code in {429, 500, 502, 503, 504}:
                    self._reset_session()
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise DeltaAPIError(f"Delta request failed: {code}", context=context if isinstance(context, dict) else {})
            return payload
        raise DeltaAPIError("Delta request failed: expired_signature")

    def public_chain(self, asset: str) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v2/tickers", {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": asset,
        })
        return payload.get("result", [])

    def connection_check(self) -> None:
        self._request("GET", "/v2/orders", {"state": "open"}, signed=True)

    def product_settlement_time(self, product_id: int) -> datetime:
        with self._settlement_lock:
            cached = self._settlement_times.get(product_id)
        if cached is not None:
            return cached
        payload = self._request("GET", f"/v2/products/{int(product_id)}")
        value = (payload.get("result") or {}).get("settlement_time")
        if not value:
            raise DeltaAPIError(f"Settlement time unavailable for product {product_id}")
        settled_at = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(IST)
        with self._settlement_lock:
            return self._settlement_times.setdefault(product_id, settled_at)

    def open_positions(self, product_ids: list[int]) -> list[dict[str, Any]]:
        if not product_ids:
            return []
        payload = self._request("GET", "/v2/positions/margined", {
            "product_ids": ",".join(str(x) for x in product_ids),
            "contract_types": "call_options,put_options",
        }, signed=True)
        return payload.get("result", [])

    def order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v2/orders", body=payload, signed=True).get("result", {})

    def get_order(self, order_id: int | str) -> dict[str, Any]:
        return self._request("GET", f"/v2/orders/{order_id}", signed=True).get("result", {})


_DELTA_CLIENT_LOCK = threading.Lock()
_CACHED_DELTA_CLIENT: DeltaClient | None = None
_CACHED_DELTA_CREDENTIALS: tuple[str, str] | None = None


def get_client() -> DeltaClient:
    # Reuse per-thread HTTPS connections; rebuild the client if credentials change.
    global _CACHED_DELTA_CLIENT, _CACHED_DELTA_CREDENTIALS
    api_key = api_credential("DELTA_API_KEY")
    api_secret = api_credential("DELTA_API_SECRET")
    credentials = (api_key, api_secret)
    with _DELTA_CLIENT_LOCK:
        if _CACHED_DELTA_CLIENT is None or credentials != _CACHED_DELTA_CREDENTIALS:
            _CACHED_DELTA_CLIENT = DeltaClient(api_key, api_secret)
            _CACHED_DELTA_CREDENTIALS = credentials
        return _CACHED_DELTA_CLIENT


def contract_has_expired(trade: ManagedStraddle) -> bool:
    try:
        client = get_client()
        settlement_times = [client.product_settlement_time(leg.product_id) for leg in trade.legs]
        return bool(settlement_times and datetime.now(IST) >= min(settlement_times))
    except Exception:
        # Keep the display usable during a temporary public API outage; the
        # worker will retry settlement-time lookup on its next monitoring pass.
        return False


@dataclass
class MarketDataPoller:
    lock: threading.Lock = field(default_factory=threading.Lock)
    chains: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    last_updated: datetime | None = None
    worker: threading.Thread | None = None
    client: DeltaClient = field(default_factory=get_client)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.refresh_once()
        self.worker = threading.Thread(target=self._run, daemon=True, name="delta-market-data")
        self.worker.start()

    def refresh_once(self) -> None:
        chains: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        for asset in ("BTC", "XAUT"):
            try:
                chains[asset] = self.client.public_chain(asset)
            except Exception as exc:
                errors[asset] = str(exc)
        with self.lock:
            for asset, rows in chains.items():
                self.chains[asset] = rows
            self.errors = errors
            self.last_updated = datetime.now(IST)

    def _run(self) -> None:
        while True:
            started = time.monotonic()
            self.refresh_once()
            time.sleep(max(0.1, REFRESH_MS / 1000 - (time.monotonic() - started)))

    def snapshot(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], datetime | None]:
        with self.lock:
            return ({asset: list(rows) for asset, rows in self.chains.items()}, dict(self.errors), self.last_updated)


@st.cache_resource
def get_market_data_poller() -> MarketDataPoller:
    poller = MarketDataPoller()
    poller.start()
    return poller


def fetch_chain(asset: str) -> list[dict[str, Any]]:
    chains, errors, _ = get_market_data_poller().snapshot()
    if asset not in chains and asset in errors:
        raise DeltaAPIError(errors[asset])
    return chains.get(asset, [])


def parse_expiry(symbol: str) -> date | None:
    try:
        suffix = symbol.rsplit("-", 1)[-1]
        if len(suffix) == 6 and suffix.isdigit():
            return datetime.strptime(suffix, "%d%m%y").date()
    except (ValueError, AttributeError):
        pass
    return None


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError):
        return None


def nearest_atm_strike(rows: list[dict[str, Any]]) -> float | None:
    spot = next((as_float(r.get("spot_price")) for r in rows if as_float(r.get("spot_price")) is not None), None)
    call_strikes = {as_float(r.get("strike_price")) for r in rows if r.get("contract_type") == "call_options" and as_float(r.get("strike_price")) is not None}
    put_strikes = {as_float(r.get("strike_price")) for r in rows if r.get("contract_type") == "put_options" and as_float(r.get("strike_price")) is not None}
    strikes = sorted(call_strikes & put_strikes)
    if spot is None or not strikes:
        return None
    return min(strikes, key=lambda strike: abs(strike - spot))


def paired_strikes(rows: list[dict[str, Any]], atm: float | None, choice: str) -> tuple[float | None, float | None]:
    """Return (call strike, put strike) for same-ATM or the user's symmetric wings."""
    call_strikes = {as_float(r.get("strike_price")) for r in rows if r.get("contract_type") == "call_options" and as_float(r.get("strike_price")) is not None}
    put_strikes = {as_float(r.get("strike_price")) for r in rows if r.get("contract_type") == "put_options" and as_float(r.get("strike_price")) is not None}
    common = sorted(call_strikes & put_strikes)
    if atm not in common:
        return None, None
    if choice == "ATM (same strike call + put)":
        return atm, atm
    index = common.index(atm)
    offset = {"Put ATM-1 + Call ATM+1": 1, "Put ATM-2 + Call ATM+2": 2, "Put ATM-3 + Call ATM+3": 3}.get(choice)
    if offset is None or index < offset or index + offset >= len(common):
        return None, None
    # Keep the same distance on each side of ATM, using listed paired strikes.
    return common[index + offset], common[index - offset]


def contract_for(rows: list[dict[str, Any]], option_type: str, strike: float) -> dict[str, Any] | None:
    contract_type = "call_options" if option_type == "CALL" else "put_options"
    return next((r for r in rows if r.get("contract_type") == contract_type and as_float(r.get("strike_price")) == strike), None)


def order_fill(client: DeltaClient, contract: dict[str, Any], side: str, size: int,
               reduce_only: bool = False) -> tuple[int, float]:
    order_payload = {
        "product_id": int(contract["product_id"]),
        "product_symbol": contract["symbol"],
        "size": int(size),
        "side": side.lower(),
        "order_type": "market_order",
        "reduce_only": bool(reduce_only),
        "client_order_id": f"dn-{uuid.uuid4().hex[:24]}",
    }
    result = client.order(order_payload)
    order_id = result.get("id")
    if order_id is None:
        raise DeltaAPIError("Delta did not return an order ID")
    latest = result
    for _ in range(8):
        filled = int(latest.get("size", size)) - int(latest.get("unfilled_size", 0))
        state = latest.get("state")
        if state in ("closed", "cancelled") or filled >= size:
            break
        time.sleep(0.35)
        latest = client.get_order(order_id)
    filled = max(0, int(latest.get("size", size)) - int(latest.get("unfilled_size", 0)))
    avg_price = as_float(latest.get("average_fill_price")) or 0.0
    return filled, avg_price


@dataclass
class TrackedLeg:
    asset: str
    expiry: str
    symbol: str
    product_id: int
    size: int
    average_fill: float
    side: str
    closed_size: int = 0


@dataclass
class ManagedStraddle:
    asset: str
    expiry: str
    strike: float
    call_strike: float
    put_strike: float
    call_symbol: str
    put_symbol: str
    entry_credit: float
    take_profit_fraction: float
    stop_multiple: float
    legs: list[TrackedLeg]
    status: str = "OPEN"
    exit_reason: str = ""
    current_ask: float | None = None
    current_pnl: float | None = None


@dataclass
class TradeController:
    lock: threading.RLock = field(default_factory=threading.RLock)
    state: str = "CLOSED"
    message: str = "Strategy stopped"
    config: dict[str, Any] | None = None
    trades: list[ManagedStraddle] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    worker: threading.Thread | None = None
    stop_requested: bool = False
    paused: bool = False
    entry_started: bool = False
    close_locks: dict[int, threading.Lock] = field(default_factory=dict)
    pending_entry_setups: list[dict[str, Any]] = field(default_factory=list)

    def log(self, message: str) -> None:
        with self.lock:
            self.events.insert(0, f"{datetime.now(IST):%H:%M:%S} IST · {message}")
            self.events = self.events[:40]
            self.message = message

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "paused": self.paused,
                "close_at_by_asset": dict(self.config.get("close_at_by_asset", {})) if self.config else {},
                "setups": list(self.config.get("setups", [])) if self.config else [],
                "pending_assets": [s["asset"] for s in getattr(self, "pending_entry_setups", [])],
                "supports_separate_asset_start": bool(getattr(self, "_supports_separate_asset_start", False)),
                "legacy_global_close": bool(getattr(self, "_uses_legacy_close_loop", False)),
                "trades": list(self.trades),
                "events": list(self.events),
            }

    def start(self, config: dict[str, Any]) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("A strategy worker is already running")
            now = datetime.now(IST)
            requested_start = datetime.combine(now.date(), config["start_time"], tzinfo=IST)
            entry_at = now if requested_start <= now else requested_start
            config["entry_at"] = entry_at
            config["close_at_by_asset"] = {}
            config["close_reason_by_asset"] = {}
            for setup in config["setups"]:
                close_date = setup.get("close_date", entry_at.date())
                close_at = datetime.combine(close_date, setup["close_time"], tzinfo=IST)
                if close_at <= entry_at:
                    raise ValueError(f"{setup['asset']} close date/time must be after the scheduled trade start")
                config["close_at_by_asset"][setup["asset"]] = close_at
            self.config = config
            self._uses_legacy_close_loop = False
            self._legacy_close_at = None
            self._dashboard_schema_version = 2
            self._uses_legacy_close_loop = False
            self._legacy_close_at = None
            self.state = "ARMED"
            self.message = "Strategy armed"
            self.trades = []
            self.events = []
            self.stop_requested = False
            self.paused = False
            self.entry_started = False
            self.pending_entry_setups = []
            self._supports_separate_asset_start = True
            self.worker = threading.Thread(target=self._run, daemon=True, name="delta-live-strategy")
            self.worker.start()
        self.log("Live strategy armed; entries begin at the configured start time")

    def add_asset_setup(self, setup: dict[str, Any]) -> None:
        asset = setup["asset"]
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.config or self.state != "RUNNING" or not self.entry_started:
                raise ValueError("A separate asset can be started only while the current strategy is running")
            if not getattr(self, "_supports_separate_asset_start", False):
                raise ValueError("This strategy was started before separate asset starts were enabled. Start a new strategy run after this one is closed.")
            if any(existing["asset"] == asset for existing in self.config["setups"]):
                raise ValueError(f"{asset} is already part of this strategy")
            if any(item["asset"] == asset for item in getattr(self, "pending_entry_setups", [])):
                raise ValueError(f"A {asset} entry is already pending")
            now = datetime.now(IST)
            close_date = setup.get("close_date", now.date())
            close_at = datetime.combine(close_date, setup["close_time"], tzinfo=IST)
            if close_at <= now:
                raise ValueError(f"{asset} close date/time must be in the future")
            legacy_close = getattr(self, "_legacy_close_at", None)
            if getattr(self, "_uses_legacy_close_loop", False) and legacy_close and close_at > legacy_close:
                raise ValueError("This older worker has an earlier global close time. It cannot safely add an asset with a later close time.")
            setup_copy = dict(setup)
            self.config["setups"].append(setup_copy)
            self.config["close_at_by_asset"][asset] = close_at
            self.config["close_reason_by_asset"][asset] = "Scheduled close"
            if not hasattr(self, "pending_entry_setups"):
                self.pending_entry_setups = []
            self.pending_entry_setups.append(setup_copy)
        self.log(f"{asset} live entry queued; worker will submit its call and put orders")

    def pause(self) -> None:
        with self.lock:
            self.paused = True
            self.state = "PAUSED"
        self.log("Paused new entries; open positions remain monitored for exits")

    def resume(self) -> None:
        with self.lock:
            self.paused = False
            self.state = "RUNNING" if self.entry_started else "ARMED"
        self.log("Strategy resumed")

    def stop(self) -> None:
        with self.lock:
            self.stop_requested = True
            if hasattr(self, "pending_entry_setups"):
                self.pending_entry_setups.clear()
            self.state = "CLOSING"
        self.log("Stop requested; closing strategy-managed positions")

    def update_close_time(self, asset: str, close_date: date, close_time: clock_time) -> datetime:
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.config:
                raise ValueError("No active strategy to update")
            if asset not in self.config.get("close_at_by_asset", {}):
                raise ValueError(f"{asset} is not part of the active strategy")
            now = datetime.now(IST)
            close_at = datetime.combine(close_date, close_time, tzinfo=IST)
            if close_at <= now:
                raise ValueError("Choose a future close date and time. Use Stop & close to exit immediately.")
            if any(
                trade.asset == asset and trade.status in {"OPEN", "CLOSING"} and trade.exit_reason
                for trade in self.trades
            ):
                raise ValueError("An exit is already in progress for this asset; its close order cannot be cancelled by changing the schedule.")
            if not self.entry_started:
                entry_at = self.config.get("entry_at")
                if entry_at is None:
                    requested_start = datetime.combine(now.date(), self.config["start_time"], tzinfo=IST)
                    entry_at = now if requested_start <= now else requested_start
                    self.config["entry_at"] = entry_at
                if close_at <= entry_at:
                    raise ValueError("Close date/time must be after the scheduled trade start")
            legacy_loop = getattr(self, "_uses_legacy_close_loop", False)
            if legacy_loop and self._legacy_close_at and close_at > self._legacy_close_at:
                raise ValueError("This already-running worker has an earlier global close time. You can move this asset's close earlier, but cannot extend it past the existing global close without restarting the strategy.")
            self.config["close_at_by_asset"][asset] = close_at
            self.config["close_reason_by_asset"][asset] = "Scheduled close"
            for setup in self.config["setups"]:
                if setup["asset"] == asset:
                    setup["close_date"] = close_date
                    setup["close_time"] = close_time
                    break
            if legacy_loop:
                self._legacy_close_at = (
                    close_at if self._legacy_close_at is None
                    else min(self._legacy_close_at, close_at)
                )
        threading.Thread(target=self._close_asset_at, args=(asset, close_at, "Scheduled close"), daemon=True, name=f"delta-close-{asset}").start()
        self.log(f"{asset} scheduled close time updated to {close_at:%Y-%m-%d %H:%M} IST")
        return close_at

    def close_asset_now(self, asset: str) -> None:
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.config:
                raise ValueError("No active strategy to close")
            if asset not in self.config.get("close_at_by_asset", {}):
                raise ValueError(f"{asset} is not part of the active strategy")
            close_at = datetime.now(IST)
            legacy_loop = getattr(self, "_uses_legacy_close_loop", False)
            self.config["close_at_by_asset"][asset] = close_at
            self.config["close_reason_by_asset"][asset] = "Manual asset close"
            for trade in self.trades:
                if trade.asset == asset and trade.status == "OPEN":
                    trade.exit_reason = "Manual asset close"
                    if legacy_loop:
                        trade.status = "CLOSING"
            if legacy_loop and self._legacy_close_at:
                self._legacy_close_at = min(self._legacy_close_at, close_at)
        threading.Thread(target=self._close_asset_at, args=(asset, close_at, "Manual asset close"), daemon=True, name=f"delta-close-{asset}").start()
        self.log(f"Close requested for {asset}; other segments remain active")

    def _close_asset_at(self, asset: str, close_at: datetime, reason: str) -> None:
        while True:
            with self.lock:
                if not self.config:
                    return
                if self.config.get("close_at_by_asset", {}).get(asset) != close_at:
                    return
            remaining = (close_at - datetime.now(IST)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 1.0))
        with self.lock:
            if not self.config or self.config.get("close_at_by_asset", {}).get(asset) != close_at:
                return
            trades = [t for t in self.trades if t.asset == asset and t.status in {"OPEN", "CLOSING"}]
            for trade in trades:
                trade.status = "CLOSING"
        if trades:
            self.log(f"{asset} close time reached; sending reduce-only close orders")
        for trade in trades:
            while trade.status != "CLOSED":
                try:
                    # Initialize the client inside the retry block as well, so a
                    # transient setup/API error cannot kill the scheduled closer.
                    client = get_client()
                    self._close_trade(client, trade, reason)
                except Exception as exc:
                    self.log(f"{asset} close retry: {exc}")
                    time.sleep(1)
                    continue
                if trade.status != "CLOSED":
                    time.sleep(1)

    def _entry_datetime(self) -> datetime:
        return self.config["entry_at"]

    def _current_rows(self, asset: str, expiry: str) -> list[dict[str, Any]]:
        rows = get_client().public_chain(asset)
        return [row for row in rows if parse_expiry(row.get("symbol", "")) == datetime.strptime(expiry, "%d-%m-%Y").date()]

    def _run(self) -> None:
        client = get_client()
        entry_at = self._entry_datetime()
        self.log(f"Worker running; scheduled entry {entry_at:%Y-%m-%d %H:%M} IST")
        while True:
            with self.lock:
                stop_requested = self.stop_requested
                paused = self.paused
                entry_started = self.entry_started
                config = self.config
                entry_at = config["entry_at"]
            now = datetime.now(IST)
            if stop_requested:
                self._close_all(client, "Manual stop")
                with self.lock:
                    still_open = any(t.status == "OPEN" for t in self.trades)
                    if not still_open:
                        self.state = "CLOSED"
                        self.stop_requested = False
                if still_open:
                    time.sleep(1)
                    continue
                self.log("Strategy stopped; managed positions closed")
                return
            if not entry_started and not paused and now >= entry_at:
                with self.lock:
                    self.entry_started = True
                    self.state = "RUNNING"
                try:
                    active_setups = [
                        setup for setup in config["setups"]
                        if now < config["close_at_by_asset"].get(setup["asset"], now)
                    ]
                    self._open_entries(client, {**config, "setups": active_setups})
                except Exception as exc:
                    self._close_all(client, "Entry failed; rollback")
                    self.log(f"Entry halted: {exc}")
                    with self.lock:
                        still_open = any(t.status == "OPEN" for t in self.trades)
                        self.state = "CLOSING" if still_open else "ERROR"
                        if still_open:
                            self.stop_requested = True
                    if not still_open:
                        return
            with self.lock:
                pending_setups = []
                if not self.paused and not self.stop_requested:
                    pending_setups = list(getattr(self, "pending_entry_setups", []))
                if pending_setups and hasattr(self, "pending_entry_setups"):
                    self.pending_entry_setups.clear()
            for pending_setup in pending_setups:
                asset = pending_setup["asset"]
                try:
                    self._open_entries(client, {**config, "setups": [pending_setup]})
                    with self.lock:
                        pending_close_at = self.config["close_at_by_asset"][asset]
                    threading.Thread(
                        target=self._close_asset_at,
                        args=(asset, pending_close_at, "Scheduled close"),
                        daemon=True,
                        name=f"delta-close-{asset}",
                    ).start()
                except Exception as exc:
                    with self.lock:
                        emergency_open = any(t.asset == asset and t.status in {"OPEN", "CLOSING"} for t in self.trades)
                        emergency_close_at = self.config.get("close_at_by_asset", {}).get(asset) if self.config else None
                        if self.config and not emergency_open:
                            self.config["setups"] = [s for s in self.config["setups"] if s["asset"] != asset]
                            self.config.get("close_at_by_asset", {}).pop(asset, None)
                            self.config.get("close_reason_by_asset", {}).pop(asset, None)
                    if emergency_open and emergency_close_at:
                        threading.Thread(
                            target=self._close_asset_at,
                            args=(asset, emergency_close_at, "Scheduled close"),
                            daemon=True,
                            name=f"delta-close-{asset}",
                        ).start()
                    self.log(f"{asset} additional live entry failed: {exc}")
            with self.lock:
                current_trades = [t for t in self.trades if t.status == "OPEN"]
            for trade in current_trades:
                if self.stop_requested:
                    break
                try:
                    if trade.exit_reason:
                        self._close_trade(client, trade, trade.exit_reason)
                    else:
                        with self.lock:
                            close_at = self.config["close_at_by_asset"].get(trade.asset)
                            close_reason = self.config["close_reason_by_asset"].get(trade.asset, "Scheduled close")
                        if close_at and now >= close_at:
                            with self.lock:
                                latest_close_at = self.config["close_at_by_asset"].get(trade.asset)
                                close_due = latest_close_at is not None and datetime.now(IST) >= latest_close_at
                                if close_due:
                                    close_reason = self.config["close_reason_by_asset"].get(trade.asset, "Scheduled close")
                                    trade.exit_reason = close_reason
                            if close_due:
                                self._close_trade(client, trade, close_reason)
                            else:
                                self._monitor_trade(client, trade)
                        else:
                            self._monitor_trade(client, trade)
                except Exception as exc:
                    self.log(f"Monitoring error for {trade.asset}: {exc}; will retry")
            with self.lock:
                pending = bool(getattr(self, "pending_entry_setups", []))
                all_done = self.entry_started and not pending and all(t.status == "CLOSED" for t in self.trades)
                if all_done:
                    self.state = "CLOSED"
            if all_done:
                self.log("All strategy trades are closed")
                return
            time.sleep(REFRESH_MS / 1000)

    def _open_entries(self, client: DeltaClient, config: dict[str, Any]) -> None:
        selected_contracts: list[dict[str, Any]] = []
        setup_by_asset: list[dict[str, Any]] = []
        for setup in config["setups"]:
            rows = self._current_rows(setup["asset"], setup["expiry"])
            call = next((r for r in rows if r.get("symbol") == setup["call_symbol"]), None)
            put = next((r for r in rows if r.get("symbol") == setup["put_symbol"]), None)
            if not call or not put:
                raise DeltaAPIError(f"Selected {setup['asset']} contracts are no longer listed")
            selected_contracts.extend([call, put])
            setup_by_asset.append({**setup, "call": call, "put": put})
        existing = client.open_positions([int(c["product_id"]) for c in selected_contracts])
        active = [p for p in existing if int(p.get("size", 0)) != 0]
        if active:
            raise DeltaAPIError("A selected contract already has an account position; close or review it before starting")

        for setup in setup_by_asset:
            opened: list[TrackedLeg] = []
            try:
                for option_type in ("CALL", "PUT"):
                    contract = setup[option_type.lower()]
                    filled, price = order_fill(client, contract, "sell", config["quantity"])
                    if filled:
                        opened.append(TrackedLeg(
                            asset=setup["asset"], expiry=setup["expiry"], symbol=contract["symbol"],
                            product_id=int(contract["product_id"]), size=filled, average_fill=price,
                            side="sell",
                        ))
                        if price <= 0:
                            raise DeltaAPIError(f"{option_type} fill price unavailable; rolling back filled leg")
                    if filled != config["quantity"]:
                        raise DeltaAPIError(f"{option_type} entry filled {filled}/{config['quantity']} contracts")
            except Exception:
                rollback_failed = False
                for leg in opened:
                    try:
                        self._close_leg(client, leg)
                    except Exception as close_exc:
                        rollback_failed = True
                        self.log(f"Emergency rollback pending for {leg.symbol}: {close_exc}")
                if rollback_failed:
                    emergency_trade = ManagedStraddle(
                        asset=setup["asset"], expiry=setup["expiry"], strike=setup["strike"],
                        call_strike=setup["call_strike"], put_strike=setup["put_strike"],
                        call_symbol=setup["call_symbol"], put_symbol=setup["put_symbol"],
                        entry_credit=sum(leg.average_fill for leg in opened),
                        take_profit_fraction=config["take_profit_fraction"],
                        stop_multiple=config["stop_multiple"], legs=opened,
                        exit_reason="Entry rollback pending",
                    )
                    with self.lock:
                        self.trades.append(emergency_trade)
                raise
            trade = ManagedStraddle(
                asset=setup["asset"], expiry=setup["expiry"], strike=setup["strike"],
                call_strike=setup["call_strike"], put_strike=setup["put_strike"],
                call_symbol=setup["call_symbol"], put_symbol=setup["put_symbol"],
                entry_credit=sum(leg.average_fill for leg in opened),
                take_profit_fraction=config["take_profit_fraction"],
                stop_multiple=config["stop_multiple"], legs=opened,
            )
            with self.lock:
                self.trades.append(trade)
            self.log(f"Opened {setup['asset']} call {setup['call_strike']} / put {setup['put_strike']}; entry credit {trade.entry_credit:.6g}")

    def _monitor_trade(self, client: DeltaClient, trade: ManagedStraddle) -> None:
        settlement_times = [client.product_settlement_time(leg.product_id) for leg in trade.legs]
        if settlement_times and datetime.now(IST) >= min(settlement_times):
            trade.current_ask = None
            trade.current_pnl = None
            return
        rows = self._current_rows(trade.asset, trade.expiry)
        call = next((r for r in rows if r.get("symbol") == trade.call_symbol), None)
        put = next((r for r in rows if r.get("symbol") == trade.put_symbol), None)
        if not call or not put:
            raise DeltaAPIError("Selected contract missing from live option chain")
        call_ask = as_float((call.get("quotes") or {}).get("best_ask"))
        put_ask = as_float((put.get("quotes") or {}).get("best_ask"))
        if call_ask is None or put_ask is None:
            raise DeltaAPIError("Ask quote unavailable; keeping position open and retrying")
        combined_ask = call_ask + put_ask
        trade.current_ask = combined_ask
        trade.current_pnl = trade.entry_credit - combined_ask
        stop_level = trade.entry_credit * trade.stop_multiple
        target_level = trade.entry_credit * trade.take_profit_fraction
        if combined_ask >= stop_level:
            self._close_trade(client, trade, "Stop: combined ask reached stop multiple")
        elif combined_ask <= target_level:
            self._close_trade(client, trade, "Take profit: combined ask reached target fraction")

    def _close_leg(self, client: DeltaClient, leg: TrackedLeg) -> None:
        remaining = max(0, leg.size - leg.closed_size)
        if remaining <= 0:
            return
        contract = {"product_id": leg.product_id, "symbol": leg.symbol}
        filled, _ = order_fill(client, contract, "buy", remaining, reduce_only=True)
        leg.closed_size += filled
        if filled < remaining:
            raise DeltaAPIError(f"Close order partially filled for {leg.symbol}; will retry")

    def _close_trade(self, client: DeltaClient, trade: ManagedStraddle, reason: str) -> None:
        with self.lock:
            locks = getattr(self, "close_locks", None)
            if locks is None:
                locks = {}
                self.close_locks = locks
            trade_lock = locks.setdefault(id(trade), threading.Lock())
            if trade.status != "CLOSED":
                trade.exit_reason = reason
        with trade_lock:
            if trade.status == "CLOSED":
                return
            for leg in trade.legs:
                self._close_leg(client, leg)
            if all(leg.closed_size >= leg.size for leg in trade.legs):
                trade.status = "CLOSED"
                self.log(f"Closed {trade.asset} straddle · {reason} · ask {trade.current_ask}")

    def _close_all(self, client: DeltaClient, reason: str) -> None:
        with self.lock:
            trades = [t for t in self.trades if t.status in {"OPEN", "CLOSING"}]
        for trade in trades:
            try:
                self._close_trade(client, trade, reason)
            except Exception as exc:
                self.log(f"Close retry needed for {trade.asset}: {exc}")


@st.cache_resource
def get_controller() -> TradeController:
    return TradeController()


@st.cache_data(ttl=5, show_spinner=False)
def check_api_connection() -> tuple[bool, str, str | None]:
    client = get_client()
    if not client.configured:
        return False, "API credentials missing", None
    try:
        client.connection_check()
        return True, "Connected", None
    except DeltaAPIError as exc:
        observed_ip = str(exc.context.get("client_ip", "")) or None
        message = str(exc)
        if "ip_not_whitelisted_for_api_key" in message:
            return False, "API key IP restriction: whitelist the IP Delta reports for this request.", observed_ip
        if "signature" in message.lower():
            return False, "Delta rejected API authentication. Check the API key/secret pair and key permissions in the local .env file.", observed_ip
        return False, message or "Delta API connection failed", observed_ip
    except Exception as exc:
        message = str(exc)
        if "ip_not_whitelisted_for_api_key" in message:
            return False, "API key IP restriction: whitelist the IP Delta reports for this request.", None
        if "signature" in message.lower():
            return False, "Delta rejected API authentication. Check the API key/secret pair and key permissions in the local .env file.", None
        return False, message or "Delta API connection failed", None


@st.cache_data(ttl=60, show_spinner=False)
def get_public_outbound_ips() -> dict[str, str]:
    """Return IPv4/IPv6 egress candidates; Delta's observed IP takes precedence."""
    import ipaddress
    addresses = {"IPv4": "Unavailable", "IPv6": "Unavailable"}
    for label, url in (("IPv4", "https://api.ipify.org"), ("IPv6", "https://api6.ipify.org")):
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            value = str(ipaddress.ip_address(response.text.strip()))
            addresses[label] = value
        except Exception:
            pass
    return addresses


def quote_value(row: dict[str, Any] | None, side: str) -> float | None:
    if not row:
        return None
    return as_float((row.get("quotes") or {}).get("best_" + side))


st.set_page_config(page_title="Delta Options Live Dashboard", page_icon="Δ", layout="wide")
client = get_client()
controller = get_controller()
if getattr(controller, "_dashboard_schema_version", 0) < 2:
    legacy_worker_active = bool(controller.worker and controller.worker.is_alive())
    controller.__class__ = TradeController
    controller._uses_legacy_close_loop = legacy_worker_active
    controller._legacy_close_at = None
if controller.config and "close_at_by_asset" not in controller.config:
    legacy_worker_active = bool(controller.worker and controller.worker.is_alive())
    old_config = controller.config
    now = datetime.now(IST)
    old_close_time = old_config.get("close_time", clock_time(23, 55))
    old_close = old_config.get("close_at")
    if not old_close:
        old_close = datetime.combine(now.date(), old_close_time, tzinfo=IST)
        if old_close <= now:
            old_close += timedelta(days=1)
    old_config["close_at_by_asset"] = {setup["asset"]: old_close for setup in old_config.get("setups", [])}
    old_config["close_reason_by_asset"] = {}
    for setup in old_config.get("setups", []):
        setup.setdefault("close_time", old_close_time)
    controller._uses_legacy_close_loop = legacy_worker_active
    controller._legacy_close_at = old_close if legacy_worker_active else None
    controller._dashboard_schema_version = 2
elif controller.__class__ is not TradeController:
    controller.__class__ = TradeController
if getattr(controller, "_dashboard_schema_version", 0) < 2:
    controller._dashboard_schema_version = 2
snapshot = controller.snapshot()
strategy_active = snapshot["state"] in {"ARMED", "RUNNING", "PAUSED", "CLOSING"}
active_setup_assets = {setup["asset"] for setup in snapshot.get("setups", [])}

st.title("Delta Options Dashboard")
st.caption("Live BTC and XAUT option data · controlled live trading · quotes update in the background")


@st.fragment(run_every="3s")
def render_market_snapshot(assets: list[str]) -> None:
    chains, errors, updated_at = get_market_data_poller().snapshot()
    if updated_at:
        st.caption(f"Latest market data: {updated_at:%H:%M:%S} IST")
    metric_columns = st.columns(max(1, len(assets)))
    for column, asset in zip(metric_columns, assets):
        rows = chains.get(asset, [])
        spot = next((as_float(row.get("spot_price")) for row in rows if as_float(row.get("spot_price")) is not None), None)
        with column:
            if asset in errors and not rows:
                st.warning(f"{asset} market data unavailable")
            else:
                st.metric(f"{asset} index spot", f"{spot:,.4f}" if spot is not None else "—")


@st.fragment(run_every="3s")
def render_selected_legs(active_setups: list[dict[str, Any]], quantity: int) -> None:
    chains, _, _ = get_market_data_poller().snapshot()
    leg_rows: list[dict[str, Any]] = []
    for setup in active_setups:
        rows = chains.get(setup["asset"], [])
        call = next((row for row in rows if row.get("symbol") == setup["call_symbol"]), None)
        put = next((row for row in rows if row.get("symbol") == setup["put_symbol"]), None)
        for option_type, strike_key, contract in (
            ("CALL · SELL", "call_strike", call),
            ("PUT · SELL", "put_strike", put),
        ):
            leg_rows.append({
                "Segment": setup["asset"], "Expiry (Asia/Kolkata)": setup["expiry_label"],
                "Strike choice": setup["strike_choice"], "Type": option_type,
                "Strike": setup[strike_key], "Contract": setup[f"{strike_key.replace('_strike', '_symbol')}"],
                "Bid": quote_value(contract, "bid"), "Ask": quote_value(contract, "ask"),
                "Contracts": quantity,
            })
    if leg_rows:
        st.dataframe(pd.DataFrame(leg_rows), hide_index=True, width="stretch")
    else:
        st.info("Choose a segment and expiry with a listed ATM call/put pair.")


@st.fragment(run_every="3s")
def render_running_summary(active_controller: TradeController) -> None:
    live_snapshot = active_controller.snapshot()
    active = live_snapshot["state"] in {"ARMED", "RUNNING", "PAUSED", "CLOSING"}
    if active:
        for setup in live_snapshot.get("setups", []):
            asset = setup["asset"]
            matching_trades = [
                trade for trade in live_snapshot["trades"]
                if trade.asset == asset and trade.status == "OPEN"
            ]
            trade = matching_trades[-1] if matching_trades else None
            st.markdown(f"**{asset}**")
            metric_cols = st.columns(4)
            expired = bool(trade and contract_has_expired(trade))
            values = ("—", "—", "—", "—") if trade is None else (
                f"{trade.entry_credit:,.2f}",
                f"{trade.entry_credit * trade.take_profit_fraction:,.2f}",
                f"{trade.entry_credit * trade.stop_multiple:,.2f}",
                f"{trade.current_pnl:,.2f}" if not expired and trade.current_pnl is not None else "—",
            )
            for col, label, value in zip(metric_cols, ("Total entry price", "Target", "SL", "P & L running"), values):
                col.metric(label, value)
    else:
        st.metric("Strategy status", live_snapshot["state"])
        st.caption(live_snapshot["message"])


@st.fragment(run_every="15s")
def render_api_connection_status(status_slot: Any, worker_status_slot: Any) -> None:
    outbound_ips = get_public_outbound_ips()
    try:
        connected, connection_message, delta_seen_ip = check_api_connection()
    except Exception as exc:
        message = str(exc)
        connected = False
        delta_seen_ip = None
        if "ip_not_whitelisted_for_api_key" in message:
            connection_message = "API key IP restriction: allowlist this computer/server's public outbound IP in Delta Exchange API-key settings."
        else:
            connection_message = "Delta API connection failed; check API credentials, key permissions, and network access."
    st.session_state["delta_api_connected"] = connected
    with status_slot:
        st.caption("IP ADDRESSES FOR DELTA API ALLOWLIST")
        if delta_seen_ip:
            st.code(f"Delta currently sees: {delta_seen_ip}\nIPv4 candidate: {outbound_ips['IPv4']}\nIPv6 candidate: {outbound_ips['IPv6']}", language=None)
        else:
            st.code(f"IPv4: {outbound_ips['IPv4']}\nIPv6: {outbound_ips['IPv6']}", language=None)
        if connected:
            st.success("CONNECTED")
            st.caption("Private Delta API check passed")
        else:
            st.error("NOT CONNECTED")
            st.caption(connection_message)
            if "IP restriction" in connection_message:
                st.caption("If Delta currently sees an IPv6 address above, add that exact IPv6 address to the API key allowlist. You can allowlist both address families.")
        st.caption("API credentials are loaded from local .env or Streamlit secrets.")
    with worker_status_slot:
        st.success("VPS: Running")
        if connected:
            st.success("Delta: Connected")
        else:
            st.error("Delta: Disconnected")


with st.sidebar:
    st.header("Market data")
    st.markdown(f"**Delta endpoint:**  \n`{API_BASE}`")
    selected_segments = st.multiselect(
        "Segments to display / trade", ["BTC", "XAUT"], default=["BTC", "XAUT"],
        key="segments_to_display_trade",
    )
    segments = list(dict.fromkeys([*selected_segments, *active_setup_assets]))
    st.header("API connection")
    connection_status_slot = st.container()
    st.header("Background worker")
    worker_status_slot = st.container()

render_api_connection_status(connection_status_slot, worker_status_slot)
connected = bool(st.session_state.get("delta_api_connected", False))

if not segments:
    st.info("Select BTC or XAUT in the sidebar to load options.")
    st.stop()

render_market_snapshot(segments)

chains: dict[str, list[dict[str, Any]]] = {}
fetch_errors: dict[str, str] = {}
for asset in segments:
    try:
        chains[asset] = fetch_chain(asset)
    except Exception as exc:
        fetch_errors[asset] = str(exc)

if fetch_errors:
    for asset, error in fetch_errors.items():
        st.error(f"{asset} market data unavailable: {error}")

st.header("Trade setup")
st.caption("The full chain stays in the background. Delta option quotes determine available expiries, ATM strikes and the selected bid/ask premium levels.")

setups: list[dict[str, Any]] = []
setup_columns = st.columns(max(1, len(segments)))
for column, asset in zip(setup_columns, segments):
    rows = chains.get(asset, [])
    expiries = sorted({parse_expiry(r.get("symbol", "")) for r in rows if parse_expiry(r.get("symbol", ""))})
    with column:
        st.subheader(f"{asset} setup")
        if not expiries:
            st.warning("No listed expiries returned")
            continue
        expiry_labels = [d.strftime("%d %b %Y") for d in expiries]
        default_ix = next((i for i, d in enumerate(expiries) if d >= datetime.now(IST).date()), 0)
        asset_in_run = asset in active_setup_assets
        setup_locked = strategy_active and asset_in_run
        expiry_label = st.selectbox(f"{asset} expiry", expiry_labels, index=default_ix, key=f"expiry_{asset}", disabled=setup_locked)
        expiry_date = datetime.strptime(expiry_label, "%d %b %Y").date()
        expiry_text = expiry_date.strftime("%d-%m-%Y")
        expiry_rows = [r for r in rows if parse_expiry(r.get("symbol", "")) == expiry_date]
        atm = nearest_atm_strike(expiry_rows)
        strike_choice = st.selectbox(
            f"{asset} strike selection",
            [
                "ATM (same strike call + put)",
                "Put ATM-1 + Call ATM+1",
                "Put ATM-2 + Call ATM+2",
                "Put ATM-3 + Call ATM+3",
            ],
            key=f"strike_choice_{asset}",
            disabled=setup_locked,
        )
        call_strike, put_strike = paired_strikes(expiry_rows, atm, strike_choice)
        # The close date/time are edited only in the dedicated controls below.
        asset_close_date = st.session_state.get(f"planned_close_date_{asset}", datetime.now(IST).date())
        asset_close_time = st.session_state.get(f"planned_close_{asset}", clock_time(23, 55))
        if call_strike is None or put_strike is None:
            st.warning("Could not determine the selected strike")
            continue
        call = contract_for(expiry_rows, "CALL", call_strike)
        put = contract_for(expiry_rows, "PUT", put_strike)
        if not call or not put:
            st.warning("A matching call/put pair is not listed at this strike")
            continue
        setups.append({
            "asset": asset, "expiry": expiry_text, "expiry_label": expiry_label,
            "close_date": asset_close_date, "close_time": asset_close_time,
            "strike": atm, "call_strike": call_strike, "put_strike": put_strike,
            "strike_choice": strike_choice,
            "call_symbol": call["symbol"], "put_symbol": put["symbol"],
            "call_product_id": int(call["product_id"]), "put_product_id": int(put["product_id"]),
            "call_bid": quote_value(call, "bid"), "call_ask": quote_value(call, "ask"),
            "put_bid": quote_value(put, "bid"), "put_ask": quote_value(put, "ask"),
        })

st.subheader("Selected short option legs")
render_selected_legs(setups, int(st.session_state.get("quantity_per_leg", 1)))

with st.expander("Schedule and strategy settings", expanded=True):
    schedule_cols = st.columns(2)
    now_ist = datetime.now(IST)
    trade_start_time = schedule_cols[0].time_input("Trade start time (Asia/Kolkata)", value=now_ist.time().replace(second=0, microsecond=0), disabled=strategy_active)
    quantity = schedule_cols[1].number_input("Contracts per leg / segment", min_value=1, max_value=100, value=1, step=1, key="quantity_per_leg", disabled=strategy_active)
    tp_label_col, tp_value_col, tp_rule_col = st.columns([1.1, 1, 2.2])
    tp_label_col.markdown("**Take profit**")
    take_profit_fraction = tp_value_col.number_input(
        "Take-profit fraction", min_value=0.05, max_value=0.95, value=0.50, step=0.05,
        disabled=strategy_active, key="take_profit_fraction", label_visibility="collapsed",
    )
    tp_rule_col.markdown(f"Close when combined call ask + put ask <= P x {take_profit_fraction:.2f}")
    st.divider()
    sl_label_col, sl_value_col, sl_rule_col = st.columns([1.1, 1, 2.2])
    sl_label_col.markdown("**Stop loss**")
    stop_multiple = sl_value_col.number_input(
        "Stop-loss multiple", min_value=1.1, max_value=5.0, value=2.0, step=0.1,
        disabled=strategy_active, key="stop_loss_multiple", label_visibility="collapsed",
    )
    sl_rule_col.markdown(f"Close when combined call ask + put ask >= P x {stop_multiple:.2f}")
    st.caption("Entry premium P = call fill price + put fill price. These exit checks use bid/ask execution data, not mark or last-traded price.")

st.warning("The worker sends separate market sell orders for the call and put. They can fill at different times; the worker attempts to close filled legs if its matching leg fails. Equal call/put sizes do not actively hedge delta. Only positions opened by this dashboard worker are managed or closed.")

st.header("Live trade controls")
current_state = snapshot["state"]
control_cols = st.columns(4)
live_confirm = st.checkbox("I understand Start live trade sends real orders to my Delta account", value=False, disabled=strategy_active)
start_disabled = (not connected or not live_confirm or not setups or current_state in {"ARMED", "RUNNING", "PAUSED", "CLOSING"})
if control_cols[0].button("Start live trade", type="primary", disabled=start_disabled, use_container_width=True):
    try:
        controller.start({
            "setups": setups,
            "quantity": int(quantity),
            "start_time": trade_start_time,
            "take_profit_fraction": float(take_profit_fraction),
            "stop_multiple": float(stop_multiple),
        })
        st.rerun()
    except Exception as exc:
        st.error(str(exc))
if control_cols[1].button("Pause", disabled=current_state not in {"ARMED", "RUNNING"}, use_container_width=True):
    controller.pause()
    st.rerun()
if control_cols[2].button("Resume", disabled=current_state != "PAUSED", use_container_width=True):
    controller.resume()
    st.rerun()
if control_cols[3].button("Stop / close strategy trades", disabled=current_state not in {"ARMED", "RUNNING", "PAUSED", "CLOSING"}, use_container_width=True):
    controller.stop()
    st.rerun()

active_snapshot = controller.snapshot()
active_close_states = {"ARMED", "RUNNING", "PAUSED", "CLOSING"}
if setups or current_state in active_close_states:
    st.subheader("BTC and XAUT close schedule controls")
    st.caption("Set a separate close date and time for each asset. Updating the schedule does not close the position; use Stop & close to exit immediately.")
    if current_state in active_close_states:
        active_setups_now = active_snapshot.get("setups", [])
        active_names_now = {setup["asset"] for setup in active_setups_now}
        candidate_setups = [setup for setup in setups if setup["asset"] not in active_names_now]
        close_setups = [*active_setups_now, *candidate_setups]
    else:
        active_names_now = set()
        close_setups = setups
    pending_names = set(active_snapshot.get("pending_assets", []))
    if active_snapshot.get("legacy_global_close"):
        st.info("This run uses the previous global schedule. It supports moving an asset's close earlier; extending beyond its original global close requires a new run.")
    close_control_columns = st.columns(max(1, len(close_setups)))
    for close_col, active_setup in zip(close_control_columns, close_setups):
        asset = active_setup["asset"]
        scheduled_close = active_snapshot.get("close_at_by_asset", {}).get(asset)
        default_close_date = scheduled_close.date() if scheduled_close else active_setup.get("close_date", datetime.now(IST).date())
        default_close_time = scheduled_close.time().replace(tzinfo=None) if scheduled_close else active_setup.get("close_time", clock_time(23, 55))
        with close_col.container(border=True):
            st.markdown(f"**{asset}** · scheduled close: {scheduled_close:%d %b %Y, %H:%M} IST" if scheduled_close else f"**{asset}**")
            if asset in pending_names:
                st.caption(f"{asset} entry is queued; the worker will send its call and put orders.")
            new_close_date = st.date_input(
                f"{asset} close date (Asia/Kolkata)", value=default_close_date,
                key=f"planned_close_date_{asset}", disabled=current_state == "CLOSING" or asset in pending_names,
            )
            new_close_time = st.time_input(
                f"{asset} close time (Asia/Kolkata)", value=default_close_time,
                key=f"planned_close_{asset}", disabled=current_state == "CLOSING" or asset in pending_names,
            )
            if asset in active_names_now and current_state in {"ARMED", "RUNNING", "PAUSED"}:
                close_update_cols = st.columns([1, 1])
                if close_update_cols[0].button(
                    f"Update {asset} close time", key=f"update_close_{asset}",
                    disabled=asset in pending_names, use_container_width=True,
                ):
                    try:
                        updated_close_at = controller.update_close_time(asset, new_close_date, new_close_time)
                        st.success(f"{asset} scheduled close updated to {updated_close_at:%d %b %Y, %H:%M} IST")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if close_update_cols[1].button(
                    f"Stop & close {asset} now", key=f"close_now_{asset}",
                    disabled=asset in pending_names, use_container_width=True,
                ):
                    try:
                        controller.close_asset_now(asset)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            elif (
                strategy_active and asset not in active_names_now and current_state == "RUNNING"
                and active_snapshot.get("supports_separate_asset_start", False)
            ):
                add_confirm = st.checkbox(
                    f"Confirm new {asset} live orders", value=False, key=f"confirm_add_{asset}"
                )
                if st.button(
                    f"Start {asset} live trade now", key=f"start_asset_{asset}",
                    disabled=not connected or not add_confirm, use_container_width=True,
                ):
                    try:
                        controller.add_asset_setup(active_setup)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                st.caption("This uses the running strategy's quantity, take-profit and stop-loss settings.")
            elif strategy_active and asset not in active_names_now:
                if current_state == "RUNNING":
                    st.caption("This strategy run predates separate live starts. Finish it before starting a new run with this asset.")
                else:
                    st.caption("Start the other asset after the strategy enters RUNNING.")
            elif current_state == "CLOSING":
                st.caption("Strategy close is in progress")
            elif st.button(f"Save {asset} close time", key=f"save_close_{asset}", use_container_width=True):
                st.success(f"{asset} close time saved for the next strategy run")

snapshot = controller.snapshot()
strategy_is_active = snapshot["state"] in {"ARMED", "RUNNING", "PAUSED", "CLOSING"}
render_running_summary(controller)
if not strategy_is_active and snapshot["trades"]:
    trade_table = []
    for trade in snapshot["trades"]:
        expired = contract_has_expired(trade)
        pnl = None if expired else trade.current_pnl
        trade_table.append({
            "Segment": trade.asset, "Expiry": trade.expiry, "ATM reference": trade.strike,
            "Call strike": trade.call_strike, "Put strike": trade.put_strike,
            "Call": trade.call_symbol, "Put": trade.put_symbol,
            "Entry credit (fills)": trade.entry_credit,
            "Current buyback (asks)": None if expired else trade.current_ask,
            "Indicative P/L": pnl,
            "Stop ask": trade.entry_credit * trade.stop_multiple,
            "Target ask": trade.entry_credit * trade.take_profit_fraction,
            "State": trade.status, "Exit reason": trade.exit_reason,
        })
    st.dataframe(pd.DataFrame(trade_table), hide_index=True, width="stretch")
if not strategy_is_active:
  with st.expander("Worker activity"):
    if snapshot["events"]:
        for event in snapshot["events"]:
            st.write(event)
    else:
        st.caption("No worker activity yet")

    st.caption("Premium P/L and stop/target checks use the actual average fill credit on entry and the combined best ask for buyback. Mark and last-traded prices are never used for P/L or exit triggers. Fees, rejected orders, gaps and slippage can change realized results.")
