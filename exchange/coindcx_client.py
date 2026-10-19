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

BASE_URL      = "https://api.coindcx.com"      # balances, markets, futures
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
        import logging as _log_mod
        _cl = _log_mod.getLogger("coindcx_client")
        body_str, headers = self._sign(body)
        base = SPOT_BASE_URL if spot else BASE_URL
        _cl.debug("POST %s%s  body=%s", base, path, body_str)
        async with self._session.post(
            base + path,
            data=body_str,
            headers=headers,
        ) as resp:
            if not resp.ok:
                try:
                    err_body = await resp.text()
                except Exception:
                    err_body = "<unreadable>"
                _cl.error(
                    "POST %s%s  status=%d  request_body=%s  response=%s",
                    base, path, resp.status, body_str, err_body,
                )
                from aiohttp import ClientResponseError
                raise ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status,
                    message=f"{resp.reason} — {err_body}",
                    headers=resp.headers,
                )
            return await resp.json()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET a public (unauthenticated) endpoint."""
        async with self._session.get(BASE_URL + path, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _signed_get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET an authenticated endpoint (CoinDCX signs a JSON body even on GET)."""
        body_str, headers = self._sign({"timestamp": int(time.time() * 1000)})
        async with self._session.get(BASE_URL + path, data=body_str, headers=headers, params=params) as resp:
            if not resp.ok:
                from aiohttp import ClientResponseError
                raise ClientResponseError(resp.request_info, resp.history, status=resp.status,
                                          message=f"{resp.reason} — {await resp.text()}", headers=resp.headers)
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

    # ── Futures endpoints ─────────────────────────────────────────────────────

    @staticmethod
    def _futures_pair(symbol: str) -> str:
        """
        Convert a spot symbol to CoinDCX futures pair format.
        CoinDCX perpetual futures use "B-BASE_QUOTE" naming, e.g.:
          BTCUSDT  →  B-BTC_USDT
          ETHUSDT  →  B-ETH_USDT
        """
        sym = symbol.upper()
        if sym.endswith("USDT"):
            return f"B-{sym[:-4]}_USDT"
        if sym.endswith("INR"):
            return f"B-{sym[:-3]}_INR"
        return sym  # unknown quote: pass through unchanged

    async def create_futures_order(
        self,
        side: str,
        pair: str,
        order_type: str,
        quantity: float,
        leverage: int,
        price: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> dict:
        """
        POST /exchange/v1/derivatives/futures/orders/create

        side:       "buy"  (open long)  | "sell" (open short)
        pair:       spot symbol e.g. "BTCUSDT" — auto-converted to B-BTC_USDT
        order_type: "market_order" | "limit_order"
        quantity:   base asset amount
        leverage:   1 – 20

        Body structure per CoinDCX docs:
          { "timestamp": ..., "order": { "side": ..., "pair": ..., ... } }
        """
        order: dict = {
            "side":           side,
            "pair":           self._futures_pair(pair),
            "order_type":     order_type,
            "total_quantity": quantity,
            "leverage":       leverage,
            "time_in_force":  "good_till_cancel",
            "notification":   "no_notification",
        }
        if price is not None:
            order["price"] = price
        if sl_price is not None:
            order["stop_loss_price"] = sl_price
        if tp_price is not None:
            order["take_profit_price"] = tp_price
        body = {"timestamp": int(time.time() * 1000), "order": order}
        return await self._post("/exchange/v1/derivatives/futures/orders/create", body)

    async def close_futures_position(
        self,
        side: str,
        pair: str,
        quantity: float,
        leverage: int,
    ) -> dict:
        """
        Close a futures position by placing the opposite-side market order.
        Long → side="sell", Short → side="buy"
        """
        order = {
            "side":           side,
            "pair":           self._futures_pair(pair),
            "order_type":     "market_order",
            "total_quantity": quantity,
            "leverage":       leverage,
            "time_in_force":  "good_till_cancel",
            "notification":   "no_notification",
        }
        body = {"timestamp": int(time.time() * 1000), "order": order}
        return await self._post("/exchange/v1/derivatives/futures/orders/create", body)

    async def get_futures_positions(self, pair: Optional[str] = None) -> list[dict]:
        """
        POST /exchange/v1/derivatives/futures/positions
        Returns all open futures positions for the account.
        """
        body: dict = {"timestamp": int(time.time() * 1000)}
        if pair:
            body["pair"] = self._futures_pair(pair)
        result = await self._post("/exchange/v1/derivatives/futures/positions", body)
        return result if isinstance(result, list) else result.get("positions", [])

    async def get_active_futures_position(self, symbol: str) -> Optional[dict]:
        """
        Return the exchange's live position for this pair, or None if flat.
        active_pos is signed: > 0 long, < 0 short.
        """
        body = {"timestamp": int(time.time() * 1000), "page": "1", "size": "100"}
        result = await self._post("/exchange/v1/derivatives/futures/positions", body)
        positions = result if isinstance(result, list) else result.get("positions", [])
        pair = self._futures_pair(symbol)
        for p in positions:
            if p.get("pair") == pair and abs(float(p.get("active_pos") or 0)) > 0:
                return p
        return None

    async def exit_futures_position(self, position_id: str) -> dict:
        """
        POST /exchange/v1/derivatives/futures/positions/exit
        Closes an existing position by id. Unlike an opposite-side market
        order, this can never open a new position in the other direction.
        """
        body = {"timestamp": int(time.time() * 1000), "id": position_id}
        return await self._post("/exchange/v1/derivatives/futures/positions/exit", body)

    async def cancel_futures_order(self, order_id: str) -> dict:
        """POST /exchange/v1/derivatives/futures/orders/cancel"""
        body = {"id": order_id, "timestamp": int(time.time() * 1000)}
        return await self._post("/exchange/v1/derivatives/futures/orders/cancel", body)

    async def create_futures_tpsl(
        self,
        position_id: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> dict:
        """
        POST /exchange/v1/derivatives/futures/positions/create_tpsl
        Attach exchange-side stop-loss / take-profit (market on trigger) to a position.
        """
        body: dict = {"timestamp": int(time.time() * 1000), "id": position_id}
        if sl_price is not None:
            body["stop_loss"] = {"stop_price": str(sl_price), "order_type": "stop_market"}
        if tp_price is not None:
            body["take_profit"] = {"stop_price": str(tp_price), "order_type": "take_profit_market"}
        return await self._post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)

    async def list_futures_positions(self, margin_currency: str = "USDT") -> list[dict]:
        """All futures positions (flat ones included) for one margin currency."""
        body = {"timestamp": int(time.time() * 1000), "page": "1", "size": "100",
                "margin_currency_short_name": [margin_currency]}
        result = await self._post("/exchange/v1/derivatives/futures/positions", body)
        return result if isinstance(result, list) else result.get("positions", [])

    async def list_futures_orders(
        self,
        status: str = "open,filled,partially_filled,partially_cancelled,cancelled,rejected,untriggered",
        side: str = "buy",
        page: int = 1,
        size: int = 50,
        margin_currency: str = "USDT",
    ) -> list[dict]:
        """POST /exchange/v1/derivatives/futures/orders — newest first."""
        body = {"timestamp": int(time.time() * 1000), "status": status, "side": side,
                "page": str(page), "size": str(size), "margin_currency_short_name": [margin_currency]}
        result = await self._post("/exchange/v1/derivatives/futures/orders", body)
        return result if isinstance(result, list) else result.get("orders", [])

    async def get_futures_transactions(
        self,
        page: int = 1,
        size: int = 100,
        stage: str = "all",
        margin_currency: str = "USDT",
    ) -> list[dict]:
        """
        POST /exchange/v1/derivatives/futures/positions/transactions — the exchange's
        own ledger: realised P&L (`amount`), `fee_amount`, funding, per fill. Newest first.
        """
        body = {"timestamp": int(time.time() * 1000), "stage": stage, "page": str(page),
                "size": str(size), "margin_currency_short_name": [margin_currency]}
        result = await self._post("/exchange/v1/derivatives/futures/positions/transactions", body)
        return result if isinstance(result, list) else result.get("transactions", result.get("data", []))

    async def get_futures_wallets(self) -> list[dict]:
        """GET /exchange/v1/derivatives/futures/wallets — USDT and INR futures wallets."""
        result = await self._signed_get("/exchange/v1/derivatives/futures/wallets")
        return result if isinstance(result, list) else result.get("data", [])

    async def get_futures_instrument(self, symbol: str, margin_currency: str = "USDT") -> dict:
        """Public instrument spec: quantity_increment, price_increment, min_notional, fees."""
        result = await self._get(
            "/exchange/v1/derivatives/futures/data/instrument",
            params={"pair": self._futures_pair(symbol), "margin_currency_short_name": margin_currency},
        )
        return result.get("instrument", result) if isinstance(result, dict) else {}

    async def get_spot_fills(
        self,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        limit: int = 500,
        sort: str = "asc",
        from_id: Optional[str] = None,
    ) -> list[dict]:
        """POST /exchange/v1/orders/trade_history with a time window."""
        body: dict = {"timestamp": int(time.time() * 1000), "limit": min(limit, 500), "sort": sort}
        if from_ms is not None:
            body["from_timestamp"] = int(from_ms)
        if to_ms is not None:
            body["to_timestamp"] = int(to_ms)
        if from_id:
            body["from_id"] = from_id
        result = await self._post("/exchange/v1/orders/trade_history", body, spot=True)
        return result if isinstance(result, list) else []

    async def get_futures_funding_rate(self, pair: str) -> Optional[float]:
        """
        GET funding rate for a futures pair.
        Returns the current 8-hour funding rate as a float (e.g. 0.0001 = 0.01%).
        Returns None if unavailable.
        """
        try:
            result = await self._get(
                "/exchange/v1/derivatives/futures/funding_rate",
                params={"pair": self._futures_pair(pair)},
            )
            return float(result.get("funding_rate", 0))
        except Exception:
            return None
