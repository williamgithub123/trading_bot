"""
Binance Exchange Service — ccxt async wrapper
"""
import asyncio
import ccxt
from datetime import datetime
from typing import List, Optional
import pandas as pd

from app.core.security import decrypt_key


class BinanceService:
    def __init__(
        self,
        api_key_enc: str,
        secret_enc: str,
        testnet: bool = True,
        use_credentials: bool = True,
    ):
        config = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }

        if use_credentials:
            config["apiKey"] = decrypt_key(api_key_enc)
            config["secret"] = decrypt_key(secret_enc)

        self.exchange = ccxt.binance(config)

        if testnet:
            self.exchange.set_sandbox_mode(True)

    async def close(self):
        close = getattr(self.exchange, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    # ── Market data ───────────────────────────────────────────────────────────

    async def fetch_ticker(self, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_ticker, symbol)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 200,
    ) -> pd.DataFrame:
        raw = await asyncio.to_thread(
            self.exchange.fetch_ohlcv,
            symbol,
            timeframe,
            None,
            limit,
        )
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    async def fetch_ohlcv_range(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: datetime | None = None,
        until: datetime | None = None,
        page_limit: int = 1000,
        max_candles: int = 20000,
    ) -> pd.DataFrame:
        since_ms = _datetime_to_milliseconds(since) if since else None
        until_ms = _datetime_to_milliseconds(until) if until else None
        all_rows: list[list] = []
        cursor = since_ms

        while len(all_rows) < max_candles:
            raw = await asyncio.to_thread(
                self.exchange.fetch_ohlcv,
                symbol,
                timeframe,
                cursor,
                min(page_limit, max_candles - len(all_rows)),
            )

            if not raw:
                break

            filtered = [
                row
                for row in raw
                if until_ms is None or int(row[0]) <= until_ms
            ]
            all_rows.extend(filtered)

            last_timestamp = int(raw[-1][0])
            if until_ms is not None and last_timestamp >= until_ms:
                break
            if cursor is not None and last_timestamp <= cursor:
                break
            if len(raw) < page_limit:
                break

            cursor = last_timestamp + 1
            await asyncio.sleep(self.exchange.rateLimit / 1000)

        df = pd.DataFrame(
            all_rows,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        if df.empty:
            return df

        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df.reset_index(drop=True)

    async def fetch_balance(self) -> dict:
        balance = await asyncio.to_thread(self.exchange.fetch_balance)
        return {k: v for k, v in balance["total"].items() if v > 0}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def create_market_order(
        self, symbol: str, side: str, amount: float
    ) -> dict:
        return await asyncio.to_thread(
            self.exchange.create_order,
            symbol,
            "market",
            side,
            amount,
        )

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> dict:
        return await asyncio.to_thread(
            self.exchange.create_order,
            symbol,
            "limit",
            side,
            amount,
            price,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.cancel_order, order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_order, order_id, symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        return await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)

    async def fetch_my_trades(
        self, symbol: str, since: Optional[datetime] = None, limit: int = 50
    ) -> List[dict]:
        """Real fills for `symbol` — used to find what a position actually closed
        at when it was closed outside the bot (see app/services/reconciler.py)."""
        since_ms = _datetime_to_milliseconds(since) if since else None
        return await asyncio.to_thread(self.exchange.fetch_my_trades, symbol, since_ms, limit)


def _datetime_to_milliseconds(value: datetime) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(timestamp.timestamp() * 1000)
