"""
Daily breakout trend-following (Donchian), long or flat — spot only.

Three independent systems share a trigger's budget (one third each):
    20-day high in / 10-day low out
    50-day high in / 25-day low out
   100-day high in / 50-day low out
Decisions are made on the daily close (00:00 UTC) against the previous N/M days.

Why this one: on 2020–2026 BTC futures data it was positive in BOTH halves (2020–22
and 2023–26) at every setting from 20 to 200 days, net of CoinDCX's 0.2% spot fee —
the only strategy tested here that was. It does NOT beat buy-and-hold in strong bull
runs (2023–26: +101% vs +396%); its value is cutting drawdowns roughly in half
(−26% vs −53%) by stepping aside in sustained falls. ETH was much weaker since 2023.
"""

from __future__ import annotations

from typing import Optional

SYSTEMS: tuple[tuple[int, int], ...] = ((20, 10), (50, 25), (100, 50))


def levels(highs: list[float], lows: list[float], n: int, m: int) -> tuple[Optional[float], Optional[float]]:
    """(entry level, exit level) for the latest closed bar: prior n-bar high, prior m-bar low."""
    if len(highs) < max(n, m) + 1:
        return None, None
    return max(highs[-n - 1:-1]), min(lows[-m - 1:-1])


def decide(highs: list[float], lows: list[float], closes: list[float], n: int, m: int,
           holding: bool) -> Optional[str]:
    """'buy' on a close above the prior n-day high, 'sell' on a close below the prior m-day low."""
    hi, lo = levels(highs, lows, n, m)
    if hi is None:
        return None
    c = closes[-1]
    if not holding and c > hi:
        return "buy"
    if holding and c < lo:
        return "sell"
    return None


def replay_state(highs: list[float], lows: list[float], closes: list[float], n: int, m: int) -> bool:
    """Whether the system would be long after the last bar, replaying the whole history."""
    pos = False
    for i in range(max(n, m) + 1, len(closes) + 1):
        a = decide(highs[:i], lows[:i], closes[:i], n, m, pos)
        if a == "buy":
            pos = True
        elif a == "sell":
            pos = False
    return pos
