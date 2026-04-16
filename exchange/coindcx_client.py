"""
CoinDCX authenticated REST client.

Used ONLY for order execution and balance fetching.
Market data (streaming, OHLCV) is sourced from Binance.

Authentication: HMAC-SHA256 signature.
  - JSON-encode the request body
  - Compute HMAC-SHA256(json_body, api_secret)
  - Send as X-AUTH-SIGNATURE header alongside X-AUTH-APIKEY
"""

import hashlib
import hmac
import json
import time
from typing import Optional

import aiohttp

BASE_URL      = "https://api.coindcx.com"      # balances, markets, non-spot
SPOT_BASE_URL = "https://apigw.coindcx.com"   # spot order execution (mandatory from CoinDCX update)


class CoinDCXClient:
    """Async CoinDCX REST client for authenticated endpoints."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._api_key    = api_key
        self._api_secret = api_secret
        self._session    = session

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, body: dict) -> tuple[str, dict]:
        """Return (json_body_str, auth_headers)."""
        body_str  = json.dumps(body, separators=(",", ":"))
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            body_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type":    "application/json",
            "X-AUTH-APIKEY":   self._api_key,
            "X-AUTH-SIGNATURE": signature,
        }
        return body_str, headers

    async def _post(self, path: str, body: dict, spot: bool = False) -> dict:
        """POST to an authenticated endpoint, return parsed JSON."""
        body_str, headers = self._sign(body)
        base = SPOT_BASE_URL if spot else BASE_URL
        async with self._session.post(
            base + path,
            data=body_str,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET a public (unauthenticated) endpoint."""
        async with self._session.get(BASE_URL + path, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Public endpoints ──────────────────────────────────────────────────────

    async def get_markets(self) -> list[dict]:
        """GET /exchange/v1/markets — list all available markets."""
        return await self._get("/exchange/v1/markets")

    # ── Authenticated endpoints ───────────────────────────────────────────────

    async def get_balances(self) -> list[dict]:
        """
        POST /exchange/v1/users/balances

        Returns list of dicts with keys:
            currency, balance, locked_balance
        Filters out zero-balance entries.
        """
        body = {"timestamp": int(time.time() * 1000)}
        result = await self._post("/exchange/v1/users/balances", body)
        # Return only non-zero balances for cleaner display
        return [
            b for b in result
            if float(b.get("balance", 0)) > 0 or float(b.get("locked_balance", 0)) > 0
        ]

    async def create_order(
        self,
        side: str,
        market: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        POST /exchange/v1/orders/create

        Args:
            side:       "buy" or "sell"
            market:     CoinDCX market symbol e.g. "BTCUSDT"
            order_type: "market_order" or "limit_order"
            quantity:   Amount of the base asset
            price:      Required for limit orders
            client_order_id: Optional idempotency key

        Returns:
            {"id": "<order_id>", "market": "...", "side": "...", "status": "...", ...}
        """
        body: dict = {
            "side":       side,
            "order_type": order_type,
            "market":     market.upper(),
            "total_quantity": quantity,
            "timestamp":  int(time.time() * 1000),
        }
        if price is not None:
            body["price_per_unit"] = price
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        return await self._post("/exchange/v1/orders/create", body, spot=True)

    async def cancel_order(self, order_id: str) -> dict:
        """
        POST /exchange/v1/orders/cancel

        Args:
            order_id: The CoinDCX order id string

        Returns:
            {"code": 200, "message": "cancelled"}
        """
        body = {
            "id":        order_id,
            "timestamp": int(time.time() * 1000),
        }
        return await self._post("/exchange/v1/orders/cancel", body, spot=True)

    async def get_order_status(self, order_id: str) -> dict:
        """
        POST /exchange/v1/orders/status

        Returns full order object including status, filled_quantity, etc.
        """
        body = {
            "id":        order_id,
            "timestamp": int(time.time() * 1000),
        }
        return await self._post("/exchange/v1/orders/status", body, spot=True)

    async def get_active_orders(self, market: Optional[str] = None) -> list[dict]:
        """
        POST /exchange/v1/orders/active_orders

        Returns list of currently open/partially-filled orders.
        """
        body: dict = {"timestamp": int(time.time() * 1000)}
        if market:
            body["market"] = market.upper()
        return await self._post("/exchange/v1/orders/active_orders", body, spot=True)

    async def get_trade_history(
        self,
        limit: int = 500,
        from_id: Optional[str] = None,
    ) -> list[dict]:
        """
        POST /exchange/v1/orders/trade_history

        Returns individual trade fills (executions) with fee details.
        Each item: {order_id, market, side, price, quantity,
                    fee_amount, fee_currency, timestamp, trade_id}
        """
        body: dict = {
            "timestamp": int(time.time() * 1000),
            "limit": min(limit, 500),
        }
        if from_id:
            body["from_id"] = from_id
        return await self._post("/exchange/v1/orders/trade_history", body, spot=True)

    async def get_order_history(
        self,
        limit: int = 100,
        market: Optional[str] = None,
    ) -> list[dict]:
        """
        POST /exchange/v1/orders

        Returns historical (closed/cancelled/filled) orders.
        Each item: {id, market, side, order_type, status,
                    total_quantity, avg_price, fee_amount, fee_currency,
                    created_at, updated_at}
        """
        body: dict = {
            "timestamp": int(time.time() * 1000),
            "limit": min(limit, 500),
        }
        if market:
            body["market"] = market.upper()
        return await self._post("/exchange/v1/orders", body, spot=True)
