"""
Quiver Quantitative Scorer — Señales de congressional trading e insider trading.
Retorna score -2 a +2 combinando ambas fuentes.
"""

import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

_API_KEY = os.getenv("QUIVER_API_KEY", "")
_BASE_URL = "https://api.quiverquant.com/beta"
_HEADERS = {"Authorization": f"Bearer {_API_KEY}"}
_TIMEOUT = 5
_DAYS_LOOKBACK = 30


def get_congressional_signal(simbolo: str) -> int:
    """
    Señal de trading de congresistas (últimos 30 días).
    +2 compras dominan, -2 ventas dominan.
    """
    if not _API_KEY:
        return 0
    try:
        resp = requests.get(
            f"{_BASE_URL}/historical/congresstrading/{simbolo}",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return 0
        trades = resp.json()
        if not trades:
            return 0

        cutoff = (datetime.now() - timedelta(days=_DAYS_LOOKBACK)).strftime("%Y-%m-%d")
        buys = 0
        sells = 0
        for t in trades:
            tx_date = (t.get("TransactionDate") or "")[:10]
            if tx_date < cutoff:
                continue
            tx_type = t.get("Transaction", "")
            amount = float(t.get("Amount") or 0)
            if "Purchase" in tx_type:
                buys += amount
            elif "Sale" in tx_type:
                sells += amount

        if buys == 0 and sells == 0:
            return 0

        total = buys + sells
        buy_pct = buys / total if total > 0 else 0.5

        if buy_pct > 0.75:
            return 2
        elif buy_pct > 0.6:
            return 1
        elif buy_pct < 0.25:
            return -2
        elif buy_pct < 0.4:
            return -1
        return 0

    except Exception:
        return 0


def get_insider_signal(simbolo: str) -> int:
    """
    Señal de insider trading (últimos 30 días).
    +2 insiders comprando agresivamente, -2 ventas masivas.
    TransactionCode: P=Purchase, S=Sale, F=Tax (ignore F).
    AcquiredDisposedCode: A=Acquired, D=Disposed.
    """
    if not _API_KEY:
        return 0
    try:
        resp = requests.get(
            f"{_BASE_URL}/live/insiders",
            params={"ticker": simbolo},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return 0
        filings = resp.json()
        if not filings:
            return 0

        cutoff = (datetime.now() - timedelta(days=_DAYS_LOOKBACK)).strftime("%Y-%m-%d")
        buy_value = 0.0
        sell_value = 0.0
        for f in filings:
            tx_date = (f.get("Date") or "")[:10]
            if tx_date < cutoff:
                continue
            code = f.get("TransactionCode", "")
            if code == "F":  # Tax withholding, not a real trade
                continue
            shares = abs(float(f.get("Shares") or 0))
            price = float(f.get("PricePerShare") or 0)
            value = shares * price
            ad_code = f.get("AcquiredDisposedCode", "")
            if ad_code == "A" or code == "P":
                buy_value += value
            elif ad_code == "D" or code == "S":
                sell_value += value

        if buy_value == 0 and sell_value == 0:
            return 0

        if sell_value == 0:
            ratio = 10.0  # All buys
        else:
            ratio = buy_value / sell_value

        if ratio > 3:
            return 2
        elif ratio > 1.5:
            return 1
        elif ratio < 0.2:
            return -2
        elif ratio < 0.5:
            return -1
        return 0

    except Exception:
        return 0


def get_quiver_score(simbolo: str) -> int:
    """Combina señales de congresistas e insiders. Retorna -2 a +2."""
    raw = get_congressional_signal(simbolo) + get_insider_signal(simbolo)
    return max(-2, min(2, raw))
