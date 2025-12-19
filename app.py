# app.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import time
import sys
import re
import json
from typing import Any, Dict, Tuple

# ✅ FIX UTF-8 para Windows (emojis sin crashear)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from flask import Flask, request, jsonify
from dotenv import dotenv_values

from config_db import load_config
from symbol_queue import SymbolQueueManager, EnqueuedSignal
from bitunix_client import BitunixClient
from executor import TradeExecutor


# -------------------- ENV / PATHS --------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE_DIR, "bitunix.env")
cfg_env = dotenv_values(ENV_PATH)

DB_PATH = (cfg_env.get("BOT_DB_PATH") or os.path.join(BASE_DIR, "bot_config.db")).strip()

API_KEY = (cfg_env.get("BITUNIX_API_KEY") or "").strip()
API_SECRET = (cfg_env.get("BITUNIX_SECRET_KEY") or "").strip()

FLASK_HOST = (cfg_env.get("FLASK_HOST") or "0.0.0.0").strip()
FLASK_PORT = int((cfg_env.get("FLASK_PORT") or "5001").strip())


# -------------------- INIT CORE --------------------

CONFIG_BY_SYMBOL = load_config(DB_PATH)

CLIENT = BitunixClient(api_key=API_KEY, api_secret=API_SECRET)

EXECUTOR = TradeExecutor(
    client=CLIENT,
    config_by_symbol=CONFIG_BY_SYMBOL,
    margin_coin="USDT",
    tp_sl_stop_type="LAST_PRICE",
    min_ticks_away=2,
)

QUEUE = SymbolQueueManager(
    processor=EXECUTOR.process_enqueued_signal,
    max_queue_per_symbol=500,
    daemon_workers=True,
)

# -------------------- FLASK --------------------

app = Flask(__name__)


def _bad(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _get_raw_body_text() -> str:
    try:
        raw = request.get_data(cache=False, as_text=True) or ""
    except Exception:
        raw = ""
    return raw.strip()


def _parse_body_best_effort() -> Dict[str, Any]:
    """
    TradingView puede mandar:
    - JSON válido aunque el Content-Type sea text/plain
    - Texto plano
    """
    raw = _get_raw_body_text()
    if not raw:
        return {}

    # Intento 1: JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            obj["_raw_body"] = raw
            return obj
    except Exception:
        pass

    # Intento 2: texto plano -> lo guardamos como content
    return {"content": raw, "_raw_body": raw}


def _extract_symbol_from_text(text_upper: str) -> str:
    """
    Extrae símbolos tipo:
      SOLUSDT.P
      BTCUSDT.P
      BINANCE:SOLUSDT.P -> SOLUSDT.P
    """
    if not text_upper:
        return ""

    t = text_upper.strip()

    # 1) EXCHANGE:SYMBOL  -> SYMBOL
    m = re.search(r"\b[A-Z0-9_\-]+:([A-Z0-9.\-]{3,})\b", t)
    if m:
        return m.group(1).strip().upper()

    # 2) AlgoUSDT(.P opcional)
    m = re.search(r"\b([A-Z0-9]{2,}USDT(?:\.P)?)\b", t)
    if m:
        return m.group(1).strip().upper()

    # 3) "PARA <SYMBOL> A" o "EN <SYMBOL> A"
    m = re.search(r"\b(?:PARA|EN)\s+([A-Z0-9.\-]{3,})\s+A\b", t)
    if m:
        return m.group(1).strip().upper()

    # 4) último intento: token con punto (ej: SOLUSDT.P)
    m = re.search(r"\b([A-Z0-9]{3,}\.[A-Z0-9]{1,6})\b", t)
    if m:
        return m.group(1).strip().upper()

    return ""


def _infer_signal_from_text(text_upper: str) -> str:
    """
    Devuelve: LONG / SHORT / BUY_TP / SELL_TP / ""
    """
    if not text_upper:
        return ""

    t = text_upper

    # TP manual
    if "BUY TP" in t or "TP ALCISTA" in t:
        return "BUY_TP"
    if "SELL TP" in t or "TP BAJISTA" in t:
        return "SELL_TP"

    # Entradas
    if re.search(r"\bLONG\b", t):
        return "LONG"
    if re.search(r"\bSHORT\b", t):
        return "SHORT"

    return ""


def _map_symbol_to_db(symbol: str) -> str:
    """
    ✅ CLAVE DEL ARREGLO:
    - TradingView puede mandar SOLUSDT.P
    - En Bitunix/API y en tu DB quieres SOLUSDT (sin .P)
    Entonces:
      si llega .P y existe sin .P en DB -> usamos sin .P
      si llega sin .P y existe con .P en DB -> usamos con .P (por compat)
    """
    s = (symbol or "").upper().strip()
    if not s:
        return ""

    if s in CONFIG_BY_SYMBOL:
        return s

    # Si llega con .P -> probar sin .P
    if s.endswith(".P"):
        base = s[:-2]
        if base in CONFIG_BY_SYMBOL:
            return base

    # Si llega sin .P -> probar con .P
    alt = s + ".P"
    if alt in CONFIG_BY_SYMBOL:
        return alt

    return s  # devuelve lo que hay; luego validamos y soltamos error claro


def _resolve_symbol_and_signal(data: Dict[str, Any]) -> Tuple[str, str]:
    """
    Compatibilidad:
    - symbol/ticker + signal/side/action (modo JSON normal)
    - solo content/message (modo texto)
    """
    content = str(data.get("content") or data.get("message") or data.get("alert_message") or "")
    content_upper = content.upper()

    symbol = str(data.get("symbol") or data.get("ticker") or "").upper().strip()
    if not symbol:
        symbol = _extract_symbol_from_text(content_upper)

    signal = str(data.get("signal") or data.get("action") or data.get("side") or "").upper().strip()

    # Normalizar si viniera BUY/SELL
    if signal == "BUY":
        signal = "LONG"
    elif signal == "SELL":
        signal = "SHORT"

    if signal not in ("LONG", "SHORT", "BUY_TP", "SELL_TP"):
        inferred = _infer_signal_from_text(content_upper)
        if inferred:
            signal = inferred

    # ✅ mapear al símbolo que realmente existe en tu DB (sin .P normalmente)
    symbol = _map_symbol_to_db(symbol)

    return symbol, signal


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/webhook", methods=["POST"])
def webhook():
    data = _parse_body_best_effort()
    if not isinstance(data, dict) or not data:
        return _bad("Body vacío o inválido")

    symbol, signal = _resolve_symbol_and_signal(data)

    if not symbol:
        return _bad("Falta symbol/ticker (no pude extraerlo del content)")
    if signal not in ("LONG", "SHORT", "BUY_TP", "SELL_TP"):
        return _bad("Señal inválida o no detectada (LONG/SHORT/BUY_TP/SELL_TP)")

    # Validar que exista en config DB
    if symbol not in CONFIG_BY_SYMBOL:
        return _bad(f"symbol sin config: {symbol} (revisa cómo está guardado en bot_config.db)")

    payload = {"signal": signal, **data}
    sig = EnqueuedSignal(symbol=symbol, payload=payload, received_ts=time.time())
    ok = QUEUE.enqueue(sig)

    if not ok:
        return _bad(f"cola llena para {symbol}", code=429)

    return jsonify({"ok": True, "enqueued": True, "symbol": symbol, "signal": signal})


if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
