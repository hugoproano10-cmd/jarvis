"""
Unusual Whales Flow Scorer — Señal de flujo institucional de opciones.
Retorna score -2 a +2 basado en volumen y premium de calls vs puts.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

_API_KEY = os.getenv("UNUSUAL_WHALES_API_KEY", "")
_BASE_URL = "https://api.unusualwhales.com/api/stock"
_TIMEOUT = 5


def get_institutional_flow(simbolo: str) -> int:
    """
    Detecta flujo institucional inusual via Unusual Whales.
    Analiza alertas de las últimas 24h:
    - Volumen de calls vs puts
    - Premium pagado (mayor premium = mayor convicción)
    Retorna:
      +2 si flujo muy bullish (calls dominan en volumen y premium)
      +1 si flujo ligeramente bullish
       0 si neutro o sin datos
      -1 si flujo ligeramente bearish
      -2 si flujo muy bearish (puts dominan)
    """
    if not _API_KEY:
        return 0
    try:
        resp = requests.get(
            f"{_BASE_URL}/{simbolo}/flow-alerts",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return 0
        alerts = resp.json().get("data", [])
        if not alerts:
            return 0

        # Agregar premium y volumen por tipo (calls vs puts)
        call_premium = 0
        put_premium = 0
        call_volume = 0
        put_volume = 0

        for alert in alerts[:50]:  # últimas 50 alertas
            premium = float(alert.get("total_premium", 0) or 0)
            volume = int(alert.get("volume", 0) or 0)
            atype = alert.get("type", "").lower()

            if atype == "call":
                call_premium += premium
                call_volume += volume
            elif atype == "put":
                put_premium += premium
                put_volume += volume

        total_premium = call_premium + put_premium
        total_volume = call_volume + put_volume

        if total_premium == 0 and total_volume == 0:
            return 0

        # Score basado en proporción de premium (más peso) + volumen
        if total_premium > 0:
            call_pct = call_premium / total_premium
        elif total_volume > 0:
            call_pct = call_volume / total_volume
        else:
            return 0

        if call_pct > 0.75:
            return 2
        elif call_pct > 0.6:
            return 1
        elif call_pct < 0.25:
            return -2
        elif call_pct < 0.4:
            return -1
        else:
            return 0

    except Exception:
        return 0
