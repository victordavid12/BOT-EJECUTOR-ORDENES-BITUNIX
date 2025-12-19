# config_db.py
# -*- coding: utf-8 -*-
"""
Carga configuración desde SQLite (SOLO config; NO colas, NO estado runtime).

Tablas esperadas:
- pairs_config (1 fila por símbolo)
- tp_levels    (N filas por símbolo)

Los porcentajes van en decimal:
  0.01 = 1%
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional


# ---------------------------- Models ----------------------------

@dataclass(frozen=True)
class TPLevel:
    symbol: str
    level: int                 # 1,2,3... (o como lo tengas)
    target_pct: float          # decimal (0.01 = 1%)
    close_frac: float          # decimal (0.30 = 30%)
    is_enabled: bool


@dataclass(frozen=True)
class PairConfig:
    symbol: str
    is_enabled: bool

    margin_mode: str           # ISOLATION | CROSS
    leverage: int

    order_size_type: str       # MARGIN_USDT | NOTIONAL_USDT | PCT_BALANCE
    order_size_value: float

    sl_enabled: bool
    sl_pct: float              # decimal

    tp_enabled: bool

    breakeven_enabled: bool
    breakeven_trigger_pct: float
    breakeven_offset_pct: float

    trailing_enabled: bool
    # Activa trailing por movimiento desde la entrada (tipo BE)
    # 0.02 = 2%
    trailing_trigger_pct: float
    trailing_step_pct: float
    trailing_distance_pct: float
    trailing_move_immediately: bool

    same_side_policy: str      # IGNORE | RESET_ORDERS

    tp_levels: List[TPLevel]   # solo los enabled, ordenados


# ---------------------------- Helpers ----------------------------

def _to_bool(v) -> bool:
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _required_str(row: sqlite3.Row, key: str) -> str:
    v = row.get(key) if hasattr(row, "get") else row[key]
    if v is None or str(v).strip() == "":
        raise ValueError(f"Campo requerido vacío: {key}")
    return str(v).strip()


def _required_int(row: sqlite3.Row, key: str) -> int:
    v = row.get(key) if hasattr(row, "get") else row[key]
    if v is None:
        raise ValueError(f"Campo requerido NULL: {key}")
    return int(v)


def _required_float(row: sqlite3.Row, key: str) -> float:
    v = row.get(key) if hasattr(row, "get") else row[key]
    if v is None:
        raise ValueError(f"Campo requerido NULL: {key}")
    return float(v)

def _optional_float(row: sqlite3.Row, key: str, default: float) -> float:
    """Lee float si existe la columna; si no existe o es NULL, devuelve default."""
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return float(default)
        v = row.get(key) if hasattr(row, "get") else row[key]
    except Exception:
        return float(default)
    if v is None:
        return float(default)
    return float(v)



# ---------------------------- Public API ----------------------------

def load_config(db_path: str) -> Dict[str, PairConfig]:
    """
    Devuelve dict: { "BTCUSDT": PairConfig(...), ... }
    - Incluye TP levels enabled y ordenados por level asc.
    - No hace caching: se llama al inicio y listo.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        pairs = _load_pairs(conn)
        tps = _load_tp_levels(conn)

        out: Dict[str, PairConfig] = {}
        for symbol, p in pairs.items():
            levels = tps.get(symbol, [])
            out[symbol] = PairConfig(
                symbol=p["symbol"],
                is_enabled=_to_bool(p["is_enabled"]),
                margin_mode=p["margin_mode"],
                leverage=int(p["leverage"]),
                order_size_type=p["order_size_type"],
                order_size_value=float(p["order_size_value"]),
                sl_enabled=_to_bool(p["sl_enabled"]),
                sl_pct=float(p["sl_pct"]),
                tp_enabled=_to_bool(p["tp_enabled"]),
                breakeven_enabled=_to_bool(p["breakeven_enabled"]),
                breakeven_trigger_pct=float(p["breakeven_trigger_pct"]),
                breakeven_offset_pct=float(p["breakeven_offset_pct"]),
                trailing_enabled=_to_bool(p["trailing_enabled"]),
                trailing_trigger_pct=float(p["trailing_trigger_pct"]),
                trailing_step_pct=float(p["trailing_step_pct"]),
                trailing_distance_pct=float(p["trailing_distance_pct"]),
                trailing_move_immediately=_to_bool(p["trailing_move_immediately"]),
                same_side_policy=p["same_side_policy"],
                tp_levels=levels,
            )

        return out
    finally:
        conn.close()


def get_pair(config: Dict[str, PairConfig], symbol: str) -> Optional[PairConfig]:
    return config.get(symbol.upper())


# ---------------------------- Internal loaders ----------------------------

def _load_pairs(conn: sqlite3.Connection) -> Dict[str, dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM pairs_config")
    rows = cur.fetchall()

    pairs: Dict[str, dict] = {}
    for r in rows:
        symbol = _required_str(r, "symbol").upper()

        margin_mode = _required_str(r, "margin_mode").upper()
        if margin_mode not in ("ISOLATION", "CROSS"):
            raise ValueError(f"{symbol}: margin_mode inválido: {margin_mode}")

        same_side_policy = _required_str(r, "same_side_policy").upper()
        if same_side_policy not in ("IGNORE", "RESET_ORDERS"):
            raise ValueError(f"{symbol}: same_side_policy inválido: {same_side_policy}")

        order_size_type = _required_str(r, "order_size_type").upper()
        if order_size_type not in ("MARGIN_USDT", "NOTIONAL_USDT", "PCT_BALANCE"):
            raise ValueError(f"{symbol}: order_size_type inválido: {order_size_type}")

        leverage = _required_int(r, "leverage")
        if leverage < 1:
            raise ValueError(f"{symbol}: leverage inválido: {leverage}")

        # floats/decimales vienen como REAL -> float. Validamos rangos básicos.
        sl_pct = _required_float(r, "sl_pct")
        if sl_pct < 0 or sl_pct > 1:
            raise ValueError(f"{symbol}: sl_pct fuera de rango [0..1]: {sl_pct}")

        be_trigger = _required_float(r, "breakeven_trigger_pct")
        be_offset = _required_float(r, "breakeven_offset_pct")
        for name, v in (("breakeven_trigger_pct", be_trigger), ("breakeven_offset_pct", be_offset)):
            if v < 0 or v > 1:
                raise ValueError(f"{symbol}: {name} fuera de rango [0..1]: {v}")

        tr_trigger = _optional_float(r, "trailing_trigger_pct", 0.02)
        if tr_trigger < 0 or tr_trigger > 1:
            raise ValueError(f"{symbol}: trailing_trigger_pct fuera de rango [0..1]: {tr_trigger}")

        tr_step = _required_float(r, "trailing_step_pct")
        tr_dist = _required_float(r, "trailing_distance_pct")
        for name, v in (
            ("trailing_step_pct", tr_step),
            ("trailing_distance_pct", tr_dist),
        ):
            if v < 0 or v > 1:
                raise ValueError(f"{symbol}: {name} fuera de rango [0..1]: {v}")

        pairs[symbol] = {
            "symbol": symbol,
            "is_enabled": r["is_enabled"],
            "margin_mode": margin_mode,
            "leverage": leverage,
            "order_size_type": order_size_type,
            "order_size_value": _required_float(r, "order_size_value"),
            "sl_enabled": r["sl_enabled"],
            "sl_pct": sl_pct,
            "tp_enabled": r["tp_enabled"],
            "breakeven_enabled": r["breakeven_enabled"],
            "breakeven_trigger_pct": be_trigger,
            "breakeven_offset_pct": be_offset,
            "trailing_enabled": r["trailing_enabled"],
            "trailing_trigger_pct": tr_trigger,
            "trailing_step_pct": tr_step,
            "trailing_distance_pct": tr_dist,
            "trailing_move_immediately": r["trailing_move_immediately"],
            "same_side_policy": same_side_policy,
        }

    return pairs


def _load_tp_levels(conn: sqlite3.Connection) -> Dict[str, List[TPLevel]]:
    cur = conn.cursor()
    cur.execute("SELECT symbol, level, target_pct, close_frac, is_enabled FROM tp_levels ORDER BY symbol, level")
    rows = cur.fetchall()

    out: Dict[str, List[TPLevel]] = {}
    for r in rows:
        symbol = _required_str(r, "symbol").upper()
        level = int(r["level"])
        target_pct = float(r["target_pct"])
        close_frac = float(r["close_frac"])
        is_enabled = _to_bool(r["is_enabled"])

        # validaciones mínimas
        if target_pct <= 0 or target_pct > 1:
            raise ValueError(f"{symbol} TP level={level}: target_pct inválido: {target_pct}")
        if close_frac <= 0 or close_frac > 1:
            raise ValueError(f"{symbol} TP level={level}: close_frac inválido: {close_frac}")

        if not is_enabled:
            continue

        out.setdefault(symbol, []).append(
            TPLevel(
                symbol=symbol,
                level=level,
                target_pct=target_pct,
                close_frac=close_frac,
                is_enabled=True,
            )
        )

    # ya vienen ordenados por ORDER BY symbol, level, pero por seguridad:
    for s in out:
        out[s].sort(key=lambda x: x.level)

    return out
