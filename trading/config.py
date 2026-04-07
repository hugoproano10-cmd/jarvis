"""
Configuración centralizada de la estrategia de trading JARVIS.
Parámetros optimizados por backtest y screener (2026-04-03).

Universo ampliado: 22 activos rankeados por Sharpe ratio.
Fuentes: screener original (2026-03-28) + screener Tiingo ampliado (2026-04-03).
"""

# ── Activos permitidos, ordenados por Sharpe del screener ───
# Originales:
#   XOM 2.72 | JNJ 2.69 | GLD 1.85 | VZ 1.51 | META 1.34
#   SOXX 1.31 | MCD 0.97 | TSLA 0.70 | KO 0.56 | XLE 0.46
#   SPY 0.18 | AAPL -0.18
# Nuevos (screener Tiingo 2026-04-03):
#   IBM 1.63 | HYG 1.60 | XLU 1.41 | T 1.34 | XLC 1.20
#   AGG 1.16 | D 0.91 | EEM 0.91 | EFA 0.91 | IEF 0.91
ACTIVOS_OPERABLES = [
    # Sharpe > 2.0
    "XOM", "JNJ",
    # Sharpe 1.5 – 2.0
    "GLD", "IBM", "HYG",
    # Sharpe 1.0 – 1.5
    "VZ", "XLU", "META", "T", "SOXX", "XLC", "AGG",
    # Sharpe 0.5 – 1.0
    "MCD", "D", "EEM", "EFA", "IEF", "TSLA", "KO", "XLE",
    # Sharpe < 0.5
    "SPY", "AAPL",
]

# Prioridad para selección de señales (índice = orden de prioridad)
# Cuando hay más señales de compra que slots disponibles, se eligen
# los activos con mayor prioridad (menor índice en esta lista).
PRIORIDAD_SHARPE = {s: i for i, s in enumerate(ACTIVOS_OPERABLES)}

# ── Parámetros de bracket orders ────────────────────────────
STOP_LOSS_PCT = 0.10             # -10% (antes -5%, causaba salidas prematuras)
TAKE_PROFIT_PCT = 0.20           # +20% (antes +10%, no capturaba el movimiento)
ALERTA_STOP_PCT = 0.06           # Alertar si la pérdida llega al -6% (cerca del stop)

# ── Límites de riesgo ───────────────────────────────────────
MAX_POR_OPERACION = 10000.0      # USD máximo por trade (ampliado de 2000)
MAX_PCT_PORTAFOLIO = 0.15        # 15% del portafolio por posición (ampliado de 10%)
MAX_POSICIONES = 8               # Máximo posiciones abiertas simultáneas (ampliado de 5)

# ── Señales de entrada ──────────────────────────────────────
UMBRAL_COMPRA = 3                # Puntuación mínima de indicadores para comprar

# ── Modelos de IA ───────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_DEEP = "deepseek-r1:32b"   # Análisis profundo y decisiones de trading
MODEL_FAST = "deepseek-r1:7b"    # Alertas rápidas y conversación

# ── Indicadores técnicos ────────────────────────────────────
PERIODO_HISTORICO_MESES = 6      # Meses de datos para indicadores
