"""
Marketdata.app Options Scorer — Señal de opciones por put/call ratio.
Retorna score -2 a +2 basado en open interest de opciones.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

_API_KEY = os.getenv("MARKETDATA_API_KEY", "")
_BASE_URL = "https://api.marketdata.app/v1/options/chain"
_TIMEOUT = 5


def get_opciones_signal(simbolo: str) -> int:
    """
    Obtiene señal de opciones via Marketdata.app.
    Calcula put/call ratio del open interest total.
    Retorna:
      +2 si ratio < 0.5 (muy bullish, predominan CALLS)
      +1 si ratio < 0.8 (bullish)
       0 si ratio 0.8–1.2 (neutro)
      -1 si ratio < 1.5 (bearish)
      -2 si ratio >= 1.5 (muy bearish, predominan PUTS)
    """
    if not _API_KEY:
        return 0
    try:
        resp = requests.get(
            f"{_BASE_URL}/{simbolo}/",
            headers={"Authorization": f"Token {_API_KEY}"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return 0
        data = resp.json()
        if data.get("s") != "ok":
            return 0

        # Sumar open interest de calls y puts
        total_call_oi = 0
        total_put_oi = 0
        option_types = data.get("optionType", [])
        open_interests = data.get("openInterest", [])

        for otype, oi in zip(option_types, open_interests):
            if oi is None:
                continue
            if otype == "call":
                total_call_oi += oi
            elif otype == "put":
                total_put_oi += oi

        if total_call_oi == 0:
            return 0

        ratio = total_put_oi / total_call_oi

        if ratio < 0.5:
            return 2
        elif ratio < 0.8:
            return 1
        elif ratio < 1.2:
            return 0
        elif ratio < 1.5:
            return -1
        else:
            return -2

    except Exception:
        return 0
