#!/home/hproano/asistente_env/bin/python
"""
Migración de logs históricos a datos/jarvis_history.db.

Fuentes parseadas:
  1. logs/trading_decisiones.log          → decisiones + trades (BUY/SELL)
  2. logs/trading_decisiones_cripto.log   → decisiones + trades (BUY/SELL)
  3. logs/jarvis_trading_YYYY-MM-DD.txt   → BALANCE → portfolio_snapshots (acciones)
  4. logs/cripto_performance_*.json       → portfolio_snapshots (cripto)

Diseño:
- Idempotente: usa (timestamp, mercado, simbolo, accion) como clave natural
  para dedup. Si una fila ya existe, la salta.
- Tolerante: cada línea/archivo en try/except. Errores se cuentan y reportan.
"""

import os
import re
import sys
import glob
import json
from datetime import datetime

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROYECTO)

from datos import jarvis_db as db  # noqa: E402


LOG_DECISIONES_ACC = os.path.join(PROYECTO, "logs", "trading_decisiones.log")
LOG_DECISIONES_CRI = os.path.join(PROYECTO, "logs", "trading_decisiones_cripto.log")
TRADING_TXT_GLOB = os.path.join(PROYECTO, "logs", "jarvis_trading_*.txt")
CRIPTO_PERF_GLOB = os.path.join(PROYECTO, "logs", "cripto_performance_*.json")


# ── Helpers de parsing ─────────────────────────────────────

_RE_PRECIO = re.compile(r"\$\s*([\d,]+\.?\d*)")
_RE_SCORE_ACC = re.compile(r"score:([+-]?\d+)")
_RE_SCORE_CRI_TOTAL = re.compile(r"total:([+-]?\d+)")
_RE_ENTRADA = re.compile(r"entrada:\$\s*([\d,]+\.?\d*)")
_RE_PNL_PCT = re.compile(r"P&L:([+-]?[\d.]+)%", re.IGNORECASE)
_RE_MOTIVO = re.compile(r"motivo:(.+)$")


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── Dedup: cache de claves ya vistas ────────────────────────

_existentes_dec = None
_existentes_trd = None


def _precargar_claves_existentes():
    """Carga (ts, mercado, sym, accion) de filas ya en DB para evitar duplicados."""
    global _existentes_dec, _existentes_trd
    _existentes_dec = set()
    _existentes_trd = set()
    try:
        with db._conn() as c:
            for r in c.execute("SELECT timestamp, mercado, simbolo, accion FROM decisiones"):
                _existentes_dec.add((r[0], r[1], r[2], r[3]))
            for r in c.execute("SELECT timestamp, mercado, simbolo, accion FROM trades"):
                _existentes_trd.add((r[0], r[1], r[2], r[3]))
    except Exception as e:
        print(f"  [warn] no pude precargar claves: {e}")


def _registrar_decision_dedup(**kwargs):
    clave = (kwargs["timestamp"], kwargs["mercado"], kwargs["simbolo"], kwargs["accion"])
    if clave in _existentes_dec:
        return False
    db.registrar_decision(**kwargs)
    _existentes_dec.add(clave)
    return True


def _registrar_trade_dedup(**kwargs):
    clave = (kwargs["timestamp"], kwargs["mercado"], kwargs["simbolo"], kwargs["accion"])
    if clave in _existentes_trd:
        return False
    db.registrar_trade(**kwargs)
    _existentes_trd.add(clave)
    return True


# ══════════════════════════════════════════════════════════════
#  1. trading_decisiones.log (acciones)
# ══════════════════════════════════════════════════════════════

def migrar_log_acciones():
    ruta = LOG_DECISIONES_ACC
    if not os.path.exists(ruta):
        print(f"  (no existe {ruta})")
        return {"decisiones": 0, "trades": 0, "errores": 0}

    dec_ok = trd_ok = err = 0
    with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or "|" not in linea:
                continue
            try:
                partes = [p.strip() for p in linea.split("|")]
                if len(partes) < 3:
                    continue
                ts = partes[0]
                accion_raw = partes[1].upper().strip()
                simbolo = partes[2].strip().upper()

                # Validar timestamp
                datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

                resto = " | ".join(partes[3:])

                # Normalizar acción
                if accion_raw.startswith("BUY"):
                    accion = "BUY"
                elif accion_raw.startswith("SELL"):
                    accion = "SELL"
                elif accion_raw.startswith("HOLD"):
                    accion = "HOLD"
                elif accion_raw.startswith("SKIP"):
                    accion = "SKIP"
                elif accion_raw.startswith("WAIT"):
                    accion = "WAIT"
                else:
                    continue

                precio_m = _RE_PRECIO.search(resto)
                precio = _num(precio_m.group(1)) if precio_m else None
                score_m = _RE_SCORE_ACC.search(resto)
                score = int(score_m.group(1)) if score_m else None
                pnl_m = _RE_PNL_PCT.search(resto)
                pnl_pct = _num(pnl_m.group(1)) if pnl_m else None
                motivo_m = _RE_MOTIVO.search(resto)
                motivo = motivo_m.group(1).strip() if motivo_m else resto[:200]

                # Extraer regla si aparece al inicio del motivo (R-XXX: ...)
                regla_m = re.match(r"(R-[A-Z-]+)", motivo or "")
                regla = regla_m.group(1) if regla_m else None

                # Registrar decisión
                _registrar_decision_dedup(
                    timestamp=ts, mercado="acciones", simbolo=simbolo,
                    accion=accion, score=score, regla=regla, motivo=motivo,
                    ejecutada=(accion in ("BUY", "SELL")),
                )
                dec_ok += 1

                # Si es BUY/SELL, además registrar en trades
                if accion in ("BUY", "SELL"):
                    _registrar_trade_dedup(
                        timestamp=ts, mercado="acciones", simbolo=simbolo,
                        accion=accion, precio=precio, score=score,
                        regla=regla, pnl_pct=pnl_pct, motivo=motivo,
                    )
                    trd_ok += 1
            except Exception as e:
                err += 1
                if err <= 3:
                    print(f"    [skip] {linea[:80]}... → {e}")

    return {"decisiones": dec_ok, "trades": trd_ok, "errores": err}


# ══════════════════════════════════════════════════════════════
#  2. trading_decisiones_cripto.log
# ══════════════════════════════════════════════════════════════

def migrar_log_cripto():
    ruta = LOG_DECISIONES_CRI
    if not os.path.exists(ruta):
        print(f"  (no existe {ruta})")
        return {"decisiones": 0, "trades": 0, "errores": 0}

    dec_ok = trd_ok = err = 0
    with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or "|" not in linea:
                continue
            try:
                partes = [p.strip() for p in linea.split("|")]
                if len(partes) < 4:
                    continue
                ts = partes[0]
                accion_raw = partes[1].upper().strip()
                par = partes[2].strip().upper()

                datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

                if accion_raw.startswith("BUY"):
                    accion = "BUY"
                elif accion_raw.startswith("SELL"):
                    accion = "SELL"
                elif accion_raw.startswith("WAIT"):
                    accion = "WAIT"
                elif accion_raw.startswith("SKIP"):
                    accion = "SKIP"
                elif accion_raw.startswith("HOLD"):
                    accion = "HOLD"
                else:
                    continue

                resto = " | ".join(partes[3:])
                precio_m = _RE_PRECIO.search(resto)
                precio = _num(precio_m.group(1)) if precio_m else None

                # Score "total" o score:+N
                score = None
                stot = _RE_SCORE_CRI_TOTAL.search(resto)
                if stot:
                    score = int(stot.group(1))
                else:
                    sacc = _RE_SCORE_ACC.search(resto)
                    if sacc:
                        score = int(sacc.group(1))

                # Score detalle: capturar tec/finbert/trends/whales/reddit si hay
                detalle_dict = {}
                for k in ("tec", "finbert", "trends", "whales", "reddit"):
                    m = re.search(rf"{k}:([+-]?\d+)", resto)
                    if m:
                        detalle_dict[k] = int(m.group(1))
                score_detalle = detalle_dict or None

                # Regla: típicamente MULTI-SCORE, UMBRAL, POS, COOLDOWN, REGIMEN
                regla = None
                for cand in ("MULTI-SCORE", "UMBRAL", "POS", "COOLDOWN",
                             "REGIMEN", "take-profit", "stop-loss", "trailing-stop"):
                    if cand in resto:
                        regla = cand
                        break

                motivo = partes[-1] if len(partes) > 3 else resto
                motivo = motivo[:200]

                _registrar_decision_dedup(
                    timestamp=ts, mercado="cripto", simbolo=par,
                    accion=accion, score=score, regla=regla, motivo=motivo,
                    score_detalle=score_detalle,
                    ejecutada=(accion in ("BUY", "SELL")),
                )
                dec_ok += 1

                if accion in ("BUY", "SELL"):
                    _registrar_trade_dedup(
                        timestamp=ts, mercado="cripto", simbolo=par,
                        accion=accion, precio=precio, score=score,
                        regla=regla, motivo=motivo, score_detalle=score_detalle,
                    )
                    trd_ok += 1
            except Exception as e:
                err += 1
                if err <= 3:
                    print(f"    [skip] {linea[:80]}... → {e}")

    return {"decisiones": dec_ok, "trades": trd_ok, "errores": err}


# ══════════════════════════════════════════════════════════════
#  3. jarvis_trading_YYYY-MM-DD.txt (BALANCE + RESULTADOS EJECUCIÓN)
# ══════════════════════════════════════════════════════════════

def migrar_trading_txt():
    archivos = sorted(glob.glob(TRADING_TXT_GLOB))
    snap_ok = err = 0

    for ruta in archivos:
        m = re.search(r"jarvis_trading_(\d{4}-\d{2}-\d{2})\.txt", ruta)
        if not m:
            continue
        fecha = m.group(1)
        try:
            with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
                texto = f.read()

            # Capturar el ÚLTIMO bloque BALANCE: { ... } del día.
            # Cada bloque es un JSON entre llaves.
            balances = re.findall(r"BALANCE:\s*(\{.*?\})", texto, re.DOTALL)
            if not balances:
                continue
            balance_json = balances[-1]
            try:
                bal = json.loads(balance_json)
            except Exception:
                continue

            equity = _num(bal.get("equity"))
            cash = _num(bal.get("cash") or bal.get("settled_cash"))

            # Upsert snapshot (lado acciones)
            # Si ya existe un snapshot con cripto, se actualiza sólo el lado acciones
            # leyendo primero.
            existing = db.get_snapshots(desde=fecha, hasta=fecha)
            prev = existing[0] if existing else {}
            datos_acc = {
                "equity": equity,
                "cash": cash,
                "posiciones": [],  # no disponible confiable en .txt
                "pnl_dia": None,
            }
            datos_cri = {
                "equity": prev.get("equity_cripto"),
                "cash": prev.get("cash_cripto"),
                "posiciones": (prev.get("detalle_posiciones") or {}).get("cripto", [])
                               if isinstance(prev.get("detalle_posiciones"), dict) else [],
                "pnl_dia": prev.get("pnl_dia_cripto"),
                "pnl_acumulado": prev.get("pnl_acumulado"),
            }
            db.guardar_snapshot_diario(datos_acc, datos_cri, fecha=fecha)
            snap_ok += 1
        except Exception as e:
            err += 1
            if err <= 3:
                print(f"    [skip] {ruta} → {e}")

    return {"snapshots_acciones": snap_ok, "errores": err}


# ══════════════════════════════════════════════════════════════
#  4. cripto_performance_*.json
# ══════════════════════════════════════════════════════════════

def migrar_cripto_performance():
    archivos = sorted(glob.glob(CRIPTO_PERF_GLOB))
    snap_ok = err = 0

    for ruta in archivos:
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
            fecha = data.get("fecha")
            if not fecha:
                m = re.search(r"cripto_performance_(\d{4}-\d{2}-\d{2})\.json", ruta)
                fecha = m.group(1) if m else None
            if not fecha:
                continue

            # Valor cripto total: balance_total_usdt del reporte
            equity_cri = _num(data.get("balance_total_usdt"))
            # USDT libre: desglose
            cash_cri = None
            for b in data.get("balance_desglose") or []:
                if b.get("activo") == "USDT":
                    cash_cri = _num(b.get("valor_usdt"))
                    break

            # Preservar lado acciones si ya existe
            existing = db.get_snapshots(desde=fecha, hasta=fecha)
            prev = existing[0] if existing else {}
            detalle_prev = prev.get("detalle_posiciones") or {}
            if not isinstance(detalle_prev, dict):
                detalle_prev = {}

            datos_acc = {
                "equity": prev.get("equity_acciones"),
                "cash": prev.get("cash_acciones"),
                "posiciones": detalle_prev.get("acciones", []),
                "pnl_dia": prev.get("pnl_dia_acciones"),
            }
            datos_cri = {
                "equity": equity_cri,
                "cash": cash_cri,
                "posiciones": data.get("posiciones_abiertas") or [],
                "pnl_dia": _num(data.get("pnl_hoy_total")),
                "pnl_acumulado": _num(data.get("pnl_acumulado_total")),
            }
            db.guardar_snapshot_diario(datos_acc, datos_cri, fecha=fecha)
            snap_ok += 1
        except Exception as e:
            err += 1
            if err <= 3:
                print(f"    [skip] {ruta} → {e}")

    return {"snapshots_cripto": snap_ok, "errores": err}


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  MIGRACIÓN LOGS → jarvis_history.db")
    print("=" * 60)

    db.inicializar()
    _precargar_claves_existentes()
    print(f"  DB: {db.DB_PATH}")
    print(f"  Decisiones pre-existentes: {len(_existentes_dec)}")
    print(f"  Trades pre-existentes:     {len(_existentes_trd)}\n")

    print("1) trading_decisiones.log (acciones)...")
    r1 = migrar_log_acciones()
    print(f"   decisiones: {r1['decisiones']}  trades: {r1['trades']}  errores: {r1['errores']}\n")

    print("2) trading_decisiones_cripto.log ...")
    r2 = migrar_log_cripto()
    print(f"   decisiones: {r2['decisiones']}  trades: {r2['trades']}  errores: {r2['errores']}\n")

    print("3) jarvis_trading_*.txt (BALANCE) ...")
    r3 = migrar_trading_txt()
    print(f"   snapshots: {r3['snapshots_acciones']}  errores: {r3['errores']}\n")

    print("4) cripto_performance_*.json ...")
    r4 = migrar_cripto_performance()
    print(f"   snapshots: {r4['snapshots_cripto']}  errores: {r4['errores']}\n")

    # Resumen final
    print("=" * 60)
    print("  TOTALES EN DB")
    print("=" * 60)
    resumen = db.get_resumen_general()
    for k, v in resumen.items():
        print(f"  {k:25}: {v}")
    print()


if __name__ == "__main__":
    main()
