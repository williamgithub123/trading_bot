"""
Binance Exchange Service — ccxt async wrapper
"""
import asyncio
import ccxt.async_support as ccxt
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
        await self.exchange.close()

    # ── Market data ───────────────────────────────────────────────────────────

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.exchange.fetch_ticker(symbol)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 200,
    ) -> pd.DataFrame:
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    async def fetch_balance(self) -> dict:
        balance = await self.exchange.fetch_balance()
        return {k: v for k, v in balance["total"].items() if v > 0}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def create_market_order(
        self, symbol: str, side: str, amount: float
    ) -> dict:
        return await self.exchange.create_order(symbol, "market", side, amount)

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> dict:
        return await self.exchange.create_order(symbol, "limit", side, amount, price)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.cancel_order(order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.fetch_order(order_id, symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        return await self.exchange.fetch_open_orders(symbol)
