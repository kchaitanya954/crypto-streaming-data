"""
Logging that survives a restart.

  logs/bot.log          — everything at INFO+, rotated (10 MB × 10)
  logs/decisions.jsonl  — one JSON object per trading decision: every signal taken
                          or skipped (with the reason), every order, fill and close.
                          This is the audit trail for "why did the bot do that?".

Usage:
    from log_setup import setup_logging, journal
    setup_logging()
    journal("trade_skipped", trigger_id=3, symbol="BTCUSDT", reason="MTF filter")

Read the journal:  tail -f logs/decisions.jsonl | python -m json.tool --json-lines
"""

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

LOG_DIR = os.environ.get("LOG_DIR", "logs")
_FMT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
_journal = logging.getLogger("decisions")


def setup_logging(level: int = logging.INFO, log_dir: str = LOG_DIR) -> None:
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(sh)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=10_000_000,
                                 backupCount=10, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)
    if not _journal.handlers:
        jh = RotatingFileHandler(os.path.join(log_dir, "decisions.jsonl"), maxBytes=20_000_000,
                                 backupCount=10, encoding="utf-8")
        jh.setFormatter(logging.Formatter("%(message)s"))
        _journal.addHandler(jh)
        _journal.setLevel(logging.INFO)
        _journal.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)


def journal(event: str, **fields) -> None:
    """Append one decision record. Never raises — logging must not break trading."""
    try:
        rec = {"ts": round(time.time(), 3),
               "utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
               "event": event, **fields}
        _journal.info(json.dumps(rec, default=str, separators=(",", ":")))
        logging.getLogger("decision").info("%s %s", event,
                                           " ".join(f"{k}={v}" for k, v in fields.items()))
    except Exception:
        pass
