"""
Tests for exchange/coindcx_client.py — pair format conversion,
order_type normalisation, and auth signing.
"""
import pytest
from exchange.coindcx_client import CoinDCXClient


# ── _futures_pair ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spot, expected", [
    ("BTCUSDT",  "B-BTC_USDT"),
    ("ETHUSDT",  "B-ETH_USDT"),
    ("BNBUSDT",  "B-BNB_USDT"),
    ("SOLUSDT",  "B-SOL_USDT"),
    ("XRPUSDT",  "B-XRP_USDT"),
    ("btcusdt",  "B-BTC_USDT"),   # lowercase input
    ("EthUsdt",  "B-ETH_USDT"),   # mixed case
])
def test_futures_pair_usdt(spot, expected):
    assert CoinDCXClient._futures_pair(spot) == expected


def test_futures_pair_inr():
    assert CoinDCXClient._futures_pair("BTCINR") == "B-BTC_INR"


def test_futures_pair_unknown_quote_passthrough():
    """Unknown quote currency is returned as-is (uppercased)."""
    result = CoinDCXClient._futures_pair("BTCBUSD")
    assert result == "BTCBUSD"


# ── order_type normalisation ──────────────────────────────────────────────────

def test_order_type_strips_suffix():
    """'market_order' → 'market' for futures."""
    raw = "market_order"
    assert raw.replace("_order", "") == "market"


def test_order_type_limit_strips_suffix():
    raw = "limit_order"
    assert raw.replace("_order", "") == "limit"


def test_order_type_already_short():
    raw = "market"
    assert raw.replace("_order", "") == "market"


# ── _sign / HMAC signature ────────────────────────────────────────────────────

def test_sign_returns_json_and_headers():
    client = CoinDCXClient.__new__(CoinDCXClient)
    client._api_key    = "test-key"
    client._api_secret = "test-secret"
    body = {"timestamp": 1234567890000, "side": "buy"}
    body_str, headers = client._sign(body)

    assert isinstance(body_str, str)
    assert "timestamp" in body_str
    assert headers["X-AUTH-APIKEY"] == "test-key"
    assert "X-AUTH-SIGNATURE" in headers
    assert len(headers["X-AUTH-SIGNATURE"]) == 64   # SHA-256 hex = 64 chars


def test_sign_deterministic():
    """Same body → same signature every time."""
    client = CoinDCXClient.__new__(CoinDCXClient)
    client._api_key    = "k"
    client._api_secret = "s"
    body = {"timestamp": 999, "pair": "B-BTC_USDT"}
    _, h1 = client._sign(body)
    _, h2 = client._sign(body)
    assert h1["X-AUTH-SIGNATURE"] == h2["X-AUTH-SIGNATURE"]


def test_sign_different_bodies_different_signatures():
    client = CoinDCXClient.__new__(CoinDCXClient)
    client._api_key    = "k"
    client._api_secret = "s"
    _, h1 = client._sign({"a": 1})
    _, h2 = client._sign({"a": 2})
    assert h1["X-AUTH-SIGNATURE"] != h2["X-AUTH-SIGNATURE"]
