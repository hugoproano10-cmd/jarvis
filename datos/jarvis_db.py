#!/home/hproano/asistente_env/bin/python
"""
JARVIS DB — Base de datos histórica SQLite para trades, decisiones, snapshots
diarios y métricas agregadas. Alimenta el dashboard y cualquier análisis
post-hoc. NUNCA debe bloquear al código de trading: todas las funciones
públicas capturan cualquier excepción y loguean en stderr — los logs de texto
siguen siendo la fuente de verdad autoritativa.
"""

import os
import sys
import json
import sqlite3
import threading
from datetime import datetime
from contextlib import contextmanager

PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_history.db")

# SQLite no es seguro bajo concurrencia desde múltiples procesos escribiendo
# al mismo tiempo. Un lock en proceso alcanza para el modelo actual (un cron
# a la vez). Entre procesos, WAL mode reduce contención.
_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════
#  SCHEMA
# ══════════════════════════════════════════════════════════════

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        mercado TEXT NOT NULL,
        simbolo TEXT NOT NULL,
        accion TEXT NOT NULL,
        qty REAL,
        precio REAL,
        monto REAL,
        score INTEGER,
        score_detalle TEXT,
        regla TEXT,
        pnl_pct REAL,
        pnl_usd REAL,
        motivo TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_trades_mercado_sym ON trades(mercado, simbolo)",
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL UNIQUE,
        equity_acciones REAL,
        cash_acciones REAL,
        equity_cripto REAL,
        cash_cripto REAL,
        equity_total REAL,
        n_posiciones_acciones INTEGER,
        n_posiciones_cripto INTEGER,
        pnl_dia_acciones REAL,
        pnl_dia_cripto REAL,
        pnl_dia_total REAL,
        pnl_acumulado REAL,
        detalle_posiciones TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisiones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        mercado TEXT NOT NULL,
        simbolo TEXT NOT NULL,
        accion TEXT NOT NULL,
        score INTEGER,
        score_detalle TEXT,
        regla TEXT,
        motivo TEXT,
        ejecutada INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisiones_ts ON decisiones(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_decisiones_mercado_sym ON decisiones(mercado, simbolo)",
    """
    CREATE TABLE IF NOT EXISTS metricas_diarias (
        fecha TEXT PRIMARY KEY,
        trades_ejecutados INTEGER DEFAULT 0,
        compras INTEGER DEFAULT 0,
        ventas INTEGER DEFAULT 0,
        win_rate REAL,
        pnl_realizado REAL,
        pnl_no_realizado REAL,
        mejor_trade TEXT,
        peor_trade TEXT,
        regimen_mercado TEXT,
        fng_valor INTEGER,
        vix_valor REAL
    )
    """,
]


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL habilita lecturas concurrentes con una escritura activa
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    try:
        yield conn
    finally:
        conn.close()


def inicializar():
    """Crea tablas si no existen. Idempotente."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _LOCK, _conn() as c:
        for stmt in _SCHEMA:
            c.execute(stmt)
        c.commit()


# Auto-init en import. Silencioso si ya existe.
try:
    inicializar()
except Exception as e:
    sys.stderr.write(f"[jarvis_db] init falló: {e}\n")


def _safe_exec(fn):
    """Decorator: atrapa cualquier excepción y la loguea, nunca propaga.
    Así el trading en vivo nunca se rompe por un fallo de DB.
    """
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            sys.stderr.write(f"[jarvis_db.{fn.__name__}] error: {e}\n")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__wrapped__ = fn
    return wrapper


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_dump(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  ESCRITURA
# ══════════════════════════════════════════════════════════════

@_safe_exec
def registrar_trade(mercado, simbolo, accion, qty=None, precio=None,
                    score=None, regla=None, pnl_pct=None, pnl_usd=None,
                    motivo=None, score_detalle=None, timestamp=None, monto=None):
    """Registra una operación EJECUTADA (no decisiones)."""
    ts = timestamp or _now_iso()
    if monto is None and qty is not None and precio is not None:
        try:
            monto = round(float(qty) * float(precio), 2)
        except Exception:
            monto = None
    if isinstance(score_detalle, (dict, list)):
        score_detalle = _json_dump(score_detalle)

    with _LOCK, _conn() as c:
        c.execute(
            """INSERT INTO trades
               (timestamp, mercado, simbolo, accion, qty, precio, monto,
                score, score_detalle, regla, pnl_pct, pnl_usd, motivo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, mercado, simbolo, accion, qty, precio, monto,
             score, score_detalle, regla, pnl_pct, pnl_usd, motivo),
        )
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


@_safe_exec
def registrar_decision(mercado, simbolo, accion, score=None, regla=None,
                       motivo=None, ejecutada=False, score_detalle=None,
                       timestamp=None):
    """Registra una decisión (ejecutada o no)."""
    ts = timestamp or _now_iso()
    if isinstance(score_detalle, (dict, list)):
        score_detalle = _json_dump(score_detalle)

    with _LOCK, _conn() as c:
        c.execute(
            """INSERT INTO decisiones
               (timestamp, mercado, simbolo, accion, score, score_detalle,
                regla, motivo, ejecutada)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, mercado, simbolo, accion, score, score_detalle,
             regla, motivo, 1 if ejecutada else 0),
        )
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


@_safe_exec
def guardar_snapshot_diario(datos_acciones=None, datos_cripto=None, fecha=None):
    """UPDATE parcial por columna — preserva el lado que NO fue provisto.

    BUG FIX: el upsert anterior sobrescribía la fila completa, haciendo
    que el ciclo de cripto (que pasaba datos_acciones=None) pisara las
    columnas de acciones con NULL, y viceversa.

    Regla de merge: una columna se actualiza sólo si el valor nuevo no
    es None. `equity_total` y `pnl_dia_total` se recalculan desde el
    estado MERGEADO (existente + nuevos). `detalle_posiciones` también
    se mergea por lado (acciones/cripto) preservando el ausente.

    Args:
        datos_acciones: dict con keys: equity, cash, posiciones, pnl_dia
                         (None = no tocar columnas de acciones)
        datos_cripto:   dict con keys: equity, cash, posiciones, pnl_dia,
                         pnl_acumulado (None = no tocar columnas de cripto)
    """
    fecha = fecha or datetime.now().strftime("%Y-%m-%d")
    da = datos_acciones or {}
    dc = datos_cripto or {}

    # Valores nuevos (None = no actualizar esa columna)
    new_eq_acc = _num(da.get("equity"))
    new_cash_acc = _num(da.get("cash"))
    new_pnl_acc = _num(da.get("pnl_dia"))
    new_pos_acc = da.get("posiciones") if isinstance(da.get("posiciones"), list) else None
    new_n_pos_acc = len(new_pos_acc) if new_pos_acc is not None else None

    new_eq_cri = _num(dc.get("equity"))
    new_cash_cri = _num(dc.get("cash"))
    new_pnl_cri = _num(dc.get("pnl_dia"))
    new_pnl_acum = _num(dc.get("pnl_acumulado"))
    new_pos_cri = dc.get("posiciones") if isinstance(dc.get("posiciones"), list) else None
    new_n_pos_cri = len(new_pos_cri) if new_pos_cri is not None else None

    def _merge(new, old):
        return new if new is not None else old

    with _LOCK, _conn() as c:
        existing = c.execute(
            "SELECT * FROM portfolio_snapshots WHERE fecha = ?", (fecha,)
        ).fetchone()
        ex = dict(existing) if existing else {}

        # Merge por columna
        eq_acc = _merge(new_eq_acc, ex.get("equity_acciones"))
        cash_acc = _merge(new_cash_acc, ex.get("cash_acciones"))
        pnl_acc = _merge(new_pnl_acc, ex.get("pnl_dia_acciones"))
        n_pos_acc = _merge(new_n_pos_acc, ex.get("n_posiciones_acciones"))

        eq_cri = _merge(new_eq_cri, ex.get("equity_cripto"))
        cash_cri = _merge(new_cash_cri, ex.get("cash_cripto"))
        pnl_cri = _merge(new_pnl_cri, ex.get("pnl_dia_cripto"))
        n_pos_cri = _merge(new_n_pos_cri, ex.get("n_posiciones_cripto"))
        pnl_acum = _merge(new_pnl_acum, ex.get("pnl_acumulado"))

        # Detalle: parse existente, overlay el lado provisto
        detalle = {}
        if ex.get("detalle_posiciones"):
            try:
                parsed = json.loads(ex["detalle_posiciones"])
                if isinstance(parsed, dict):
                    detalle = parsed
            except Exception:
                pass
        if new_pos_acc is not None:
            detalle["acciones"] = new_pos_acc
        if new_pos_cri is not None:
            detalle["cripto"] = new_pos_cri

        # Totales derivados del estado mergeado
        if eq_acc is not None or eq_cri is not None:
            equity_total = (eq_acc or 0) + (eq_cri or 0)
        else:
            equity_total = None
        if pnl_acc is not None or pnl_cri is not None:
            pnl_dia_total = (pnl_acc or 0) + (pnl_cri or 0)
        else:
            pnl_dia_total = None

        valores = (
            eq_acc, cash_acc, eq_cri, cash_cri, equity_total,
            n_pos_acc, n_pos_cri, pnl_acc, pnl_cri, pnl_dia_total,
            pnl_acum, _json_dump(detalle),
        )

        if existing:
            c.execute(
                """UPDATE portfolio_snapshots SET
                     equity_acciones=?, cash_acciones=?,
                     equity_cripto=?, cash_cripto=?,
                     equity_total=?,
                     n_posiciones_acciones=?, n_posiciones_cripto=?,
                     pnl_dia_acciones=?, pnl_dia_cripto=?,
                     pnl_dia_total=?,
                     pnl_acumulado=?, detalle_posiciones=?
                   WHERE fecha = ?""",
                valores + (fecha,),
            )
        else:
            c.execute(
                """INSERT INTO portfolio_snapshots
                   (fecha, equity_acciones, cash_acciones,
                    equity_cripto, cash_cripto, equity_total,
                    n_posiciones_acciones, n_posiciones_cripto,
                    pnl_dia_acciones, pnl_dia_cripto, pnl_dia_total,
                    pnl_acumulado, detalle_posiciones)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fecha,) + valores,
            )
        c.commit()
        return fecha


@_safe_exec
def backfill_qty_pnl():
    """Estima qty y pnl_usd para trades migrados del log que no tenían
    esos campos. Se ejecuta una sola vez.

    Reglas:
      - BUY sin qty: qty = MONTO_OPERACION / precio
        (MONTO_OPERACION: $750 acciones / $1000 cripto)
      - SELL sin qty: usar qty del BUY más reciente del mismo (mercado, símbolo)
        previo al SELL. Si no hay BUY previo, estimar con MONTO_OPERACION.
      - SELL con pnl_pct pero sin pnl_usd: pnl_usd = precio * qty * (pnl_pct/100)

    Retorna dict con el conteo de filas actualizadas.
    """
    MONTO = {"acciones": 750.0, "cripto": 1000.0}

    buys_updated = 0
    sells_qty_updated = 0
    pnl_updated = 0

    with _LOCK, _conn() as c:
        # 1) BUYs sin qty: estimar desde MONTO_OPERACION / precio
        rows = c.execute(
            """SELECT id, mercado, precio FROM trades
               WHERE accion='BUY' AND (qty IS NULL OR qty = 0)
                 AND precio IS NOT NULL AND precio > 0"""
        ).fetchall()
        for r in rows:
            monto_op = MONTO.get(r["mercado"], 750.0)
            qty = monto_op / float(r["precio"])
            c.execute(
                "UPDATE trades SET qty = ?, monto = ? WHERE id = ?",
                (qty, round(qty * float(r["precio"]), 2), r["id"]),
            )
            buys_updated += 1

        # 2) SELLs sin qty: buscar BUY previo del mismo símbolo
        sells = c.execute(
            """SELECT id, mercado, simbolo, timestamp, precio FROM trades
               WHERE accion='SELL' AND (qty IS NULL OR qty = 0)
               ORDER BY timestamp"""
        ).fetchall()
        for s in sells:
            prev_buy = c.execute(
                """SELECT qty FROM trades
                   WHERE accion='BUY' AND mercado = ? AND simbolo = ?
                     AND timestamp <= ? AND qty IS NOT NULL
                   ORDER BY timestamp DESC LIMIT 1""",
                (s["mercado"], s["simbolo"], s["timestamp"]),
            ).fetchone()
            qty = None
            if prev_buy and prev_buy["qty"]:
                qty = float(prev_buy["qty"])
            elif s["precio"] and float(s["precio"]) > 0:
                monto_op = MONTO.get(s["mercado"], 750.0)
                qty = monto_op / float(s["precio"])
            if qty is None:
                continue
            c.execute("UPDATE trades SET qty = ? WHERE id = ?", (qty, s["id"]))
            sells_qty_updated += 1

        # 3) SELLs con pnl_pct sin pnl_usd: derivar desde precio * qty * pct
        rows = c.execute(
            """SELECT id, precio, qty, pnl_pct FROM trades
               WHERE accion='SELL' AND pnl_usd IS NULL
                 AND pnl_pct IS NOT NULL
                 AND qty IS NOT NULL AND qty > 0
                 AND precio IS NOT NULL AND precio > 0"""
        ).fetchall()
        for r in rows:
            pnl_usd = round(float(r["precio"]) * float(r["qty"])
                            * (float(r["pnl_pct"]) / 100.0), 2)
            c.execute("UPDATE trades SET pnl_usd = ? WHERE id = ?",
                      (pnl_usd, r["id"]))
            pnl_updated += 1

        c.commit()

    return {
        "buys_qty_estimada": buys_updated,
        "sells_qty_estimada": sells_qty_updated,
        "sells_pnl_usd_estimado": pnl_updated,
    }


@_safe_exec
def limpiar_snapshots_invalidos(umbral=50000):
    """Elimina snapshots con equity fuera de rango creíble.

    Los logs antiguos (cuenta de paper trading a $100k) se colaron en la
    migración. El capital real del sistema no ha superado ~$15k, así que
    cualquier equity > umbral es espurio.

    Retorna el número de filas borradas.
    """
    with _LOCK, _conn() as c:
        cur = c.execute(
            """DELETE FROM portfolio_snapshots
               WHERE COALESCE(equity_total, 0) > ?
                  OR COALESCE(equity_acciones, 0) > ?
                  OR COALESCE(equity_cripto, 0) > ?""",
            (umbral, umbral, umbral),
        )
        c.commit()
        return cur.rowcount


@_safe_exec
def guardar_metricas_diarias(fecha, metricas):
    """Upsert de métricas agregadas del día.

    metricas: dict con cualquier subset de las columnas.
    """
    campos = ["trades_ejecutados", "compras", "ventas", "win_rate",
              "pnl_realizado", "pnl_no_realizado", "mejor_trade",
              "peor_trade", "regimen_mercado", "fng_valor", "vix_valor"]
    valores = [metricas.get(k) for k in campos]

    set_clause = ", ".join(f"{k}=excluded.{k}" for k in campos)
    placeholders = ",".join("?" * (len(campos) + 1))

    with _LOCK, _conn() as c:
        c.execute(
            f"""INSERT INTO metricas_diarias (fecha, {",".join(campos)})
                VALUES ({placeholders})
                ON CONFLICT(fecha) DO UPDATE SET {set_clause}""",
            [fecha] + valores,
        )
        c.commit()
        return fecha


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════
#  LECTURA
# ══════════════════════════════════════════════════════════════

def get_trades(mercado=None, simbolo=None, desde=None, hasta=None, limit=None):
    """Retorna lista de trades como dicts, filtrable."""
    q = "SELECT * FROM trades WHERE 1=1"
    params = []
    if mercado:
        q += " AND mercado = ?"
        params.append(mercado)
    if simbolo:
        q += " AND simbolo = ?"
        params.append(simbolo)
    if desde:
        q += " AND timestamp >= ?"
        params.append(desde)
    if hasta:
        q += " AND timestamp <= ?"
        params.append(hasta)
    q += " ORDER BY timestamp DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    try:
        with _conn() as c:
            rows = c.execute(q, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        sys.stderr.write(f"[jarvis_db.get_trades] {e}\n")
        return []


def get_decisiones(mercado=None, simbolo=None, desde=None, hasta=None, limit=None):
    q = "SELECT * FROM decisiones WHERE 1=1"
    params = []
    if mercado:
        q += " AND mercado = ?"
        params.append(mercado)
    if simbolo:
        q += " AND simbolo = ?"
        params.append(simbolo)
    if desde:
        q += " AND timestamp >= ?"
        params.append(desde)
    if hasta:
        q += " AND timestamp <= ?"
        params.append(hasta)
    q += " ORDER BY timestamp DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    try:
        with _conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]
    except Exception as e:
        sys.stderr.write(f"[jarvis_db.get_decisiones] {e}\n")
        return []


def get_snapshots(desde=None, hasta=None):
    q = "SELECT * FROM portfolio_snapshots WHERE 1=1"
    params = []
    if desde:
        q += " AND fecha >= ?"
        params.append(desde)
    if hasta:
        q += " AND fecha <= ?"
        params.append(hasta)
    q += " ORDER BY fecha ASC"
    try:
        with _conn() as c:
            rows = c.execute(q, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if d.get("detalle_posiciones"):
                    try:
                        d["detalle_posiciones"] = json.loads(d["detalle_posiciones"])
                    except Exception:
                        pass
                out.append(d)
            return out
    except Exception as e:
        sys.stderr.write(f"[jarvis_db.get_snapshots] {e}\n")
        return []


def get_metricas(desde=None, hasta=None):
    q = "SELECT * FROM metricas_diarias WHERE 1=1"
    params = []
    if desde:
        q += " AND fecha >= ?"
        params.append(desde)
    if hasta:
        q += " AND fecha <= ?"
        params.append(hasta)
    q += " ORDER BY fecha ASC"
    try:
        with _conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]
    except Exception as e:
        sys.stderr.write(f"[jarvis_db.get_metricas] {e}\n")
        return []


def get_resumen_general():
    """Resumen global: totales desde el inicio de los registros."""
    try:
        with _conn() as c:
            def scalar(q, *p):
                r = c.execute(q, p).fetchone()
                return r[0] if r else None

            resumen = {
                "trades_total": scalar("SELECT COUNT(*) FROM trades") or 0,
                "trades_acciones": scalar(
                    "SELECT COUNT(*) FROM trades WHERE mercado='acciones'") or 0,
                "trades_cripto": scalar(
                    "SELECT COUNT(*) FROM trades WHERE mercado='cripto'") or 0,
                "compras": scalar(
                    "SELECT COUNT(*) FROM trades WHERE accion='BUY'") or 0,
                "ventas": scalar(
                    "SELECT COUNT(*) FROM trades WHERE accion='SELL'") or 0,
                "pnl_realizado_total": scalar(
                    "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE accion='SELL'") or 0,
                "decisiones_total": scalar("SELECT COUNT(*) FROM decisiones") or 0,
                "snapshots_total": scalar("SELECT COUNT(*) FROM portfolio_snapshots") or 0,
                "primer_trade": scalar("SELECT MIN(timestamp) FROM trades"),
                "ultimo_trade": scalar("SELECT MAX(timestamp) FROM trades"),
            }
            # Win rate: usa pnl_usd si está, si no pnl_pct (logs históricos
            # traen solo pnl_pct).
            ganadoras = scalar(
                "SELECT COUNT(*) FROM trades WHERE accion='SELL' "
                "AND COALESCE(pnl_usd, pnl_pct) > 0") or 0
            perdedoras = scalar(
                "SELECT COUNT(*) FROM trades WHERE accion='SELL' "
                "AND COALESCE(pnl_usd, pnl_pct) < 0") or 0
            con_pnl = scalar(
                "SELECT COUNT(*) FROM trades WHERE accion='SELL' "
                "AND COALESCE(pnl_usd, pnl_pct) IS NOT NULL") or 0
            resumen["win_rate"] = round(ganadoras / con_pnl * 100, 2) if con_pnl else None
            resumen["ventas_ganadoras"] = ganadoras
            resumen["ventas_perdedoras"] = perdedoras
            return resumen
    except Exception as e:
        sys.stderr.write(f"[jarvis_db.get_resumen_general] {e}\n")
        return {}


# ══════════════════════════════════════════════════════════════
#  CLI de inspección
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"DB: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("  (no existe aún — inicializando)")
    inicializar()
    r = get_resumen_general()
    print("\nResumen:")
    for k, v in r.items():
        print(f"  {k}: {v}")
