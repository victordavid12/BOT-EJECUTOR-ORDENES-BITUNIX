# executor.py
# -*- coding: utf-8 -*-
"""
Executor REAL: lógica de trading por señal (LONG/SHORT) + SL/TP + breakeven + trailing (emulado).

FIX:
- Al cerrar (tradeSide=CLOSE) ahora pasamos position_id y usamos el side correcto (lo resuelve BitunixClient.close_market).
- ✅ NUEVO: soporta señales de TP manual:
    BUY_TP  -> cierra LONG
    SELL_TP -> cierra SHORT
  (sin abrir posiciones nuevas)
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Dict, List, Optional

from config_db import PairConfig, TPLevel
from symbol_queue import EnqueuedSignal
from bitunix_client import BitunixClient


# ---------------------------- utils num ----------------------------

def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def round_down(value: Decimal, precision: int) -> Decimal:
    if precision <= 0:
        return value.to_integral_value(rounding=ROUND_DOWN)
    q = Decimal("1").scaleb(-precision)
    return value.quantize(q, rounding=ROUND_DOWN)


def fmt_decimal(value: Decimal, precision: int) -> str:
    return format(round_down(value, precision), "f")


def tick_size(quote_precision: int) -> Decimal:
    return Decimal("1").scaleb(-quote_precision) if quote_precision > 0 else Decimal("1")


def clamp_sl_not_instant(side: str, sl: Decimal, current: Decimal, quote_precision: int, min_ticks_away: int = 2) -> Decimal:
    ticks = tick_size(quote_precision) * Decimal(str(max(1, int(min_ticks_away))))
    s = side.upper()
    if s == "LONG":
        max_sl = current - ticks
        if sl >= max_sl:
            sl = round_down(max_sl, quote_precision)
    else:
        min_sl = current + ticks
        if sl <= min_sl:
            sl = round_down(min_sl, quote_precision)
    return sl


def compute_sl_from_entry(entry: Decimal, quote_precision: int, side: str, sl_pct: Decimal) -> Decimal:
    t = tick_size(quote_precision)
    s = side.upper()
    if s == "LONG":
        sl = entry * (Decimal("1") - sl_pct)
        sl = round_down(sl, quote_precision)
        if sl >= entry:
            sl = round_down(entry - t, quote_precision)
    else:
        sl = entry * (Decimal("1") + sl_pct)
        sl = round_down(sl, quote_precision)
        if sl <= entry:
            sl = round_down(entry + t, quote_precision)
    return sl


def compute_tp_from_entry(entry: Decimal, quote_precision: int, side: str, target_pct: Decimal) -> Decimal:
    t = tick_size(quote_precision)
    s = side.upper()
    if s == "LONG":
        tp = entry * (Decimal("1") + target_pct)
        tp = round_down(tp, quote_precision)
        if tp <= entry:
            tp = round_down(entry + t, quote_precision)
    else:
        tp = entry * (Decimal("1") - target_pct)
        tp = round_down(tp, quote_precision)
        if tp >= entry:
            tp = round_down(entry - t, quote_precision)
    return tp


def side_matches(prefer: str, got: str) -> bool:
    p = prefer.upper()
    g = got.upper()
    if p == "LONG":
        return g in ("LONG", "BUY")
    if p == "SHORT":
        return g in ("SHORT", "SELL")
    return g == p


# ---------------------------- models runtime ----------------------------

@dataclass
class OpenPosition:
    symbol: str
    position_id: str
    side: str                 # LONG | SHORT
    entry_price: Decimal
    initial_qty: Decimal
    base_precision: int
    quote_precision: int
    margin_coin: str = "USDT"


# ---------------------------- monitor ----------------------------

class SymbolMonitor:
    def __init__(self, client: BitunixClient, symbol: str) -> None:
        self.client = client
        self.symbol = symbol.upper()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"monitor:{self.symbol}", daemon=True)

        self._pos: Optional[OpenPosition] = None
        self._cfg: Optional[PairConfig] = None

        self._last_sl: Decimal = Decimal("0")
        self._be_done: bool = False
        self._trail_active: bool = False
        self._trail_best: Decimal = Decimal("0")
        self._trail_anchor: Decimal = Decimal("0")

        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_position(self, pos: Optional[OpenPosition], cfg: Optional[PairConfig]) -> None:
        with self._lock:
            self._pos = pos
            self._cfg = cfg
            self._last_sl = Decimal("0")
            self._be_done = False
            self._trail_active = False
            self._trail_best = Decimal("0")
            self._trail_anchor = Decimal("0")

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)

            with self._lock:
                pos = self._pos
                cfg = self._cfg

            if not pos or not cfg:
                continue

            if not (cfg.sl_enabled and (cfg.breakeven_enabled or cfg.trailing_enabled)):
                continue

            try:
                pos_list = self.client.get_pending_positions(pos.symbol)
                p = next((pp for pp in pos_list if str(pp.get("positionId") or "") == pos.position_id), None)

                if not p:
                    any_open = any(abs(_d(pp.get("qty"))) > 0 for pp in pos_list)
                    if not any_open:
                        with self._lock:
                            self._pos = None
                        continue
                    continue

                remaining = abs(_d(p.get("qty")))
                if remaining <= 0:
                    with self._lock:
                        self._pos = None
                    continue

                curr_sl = _d(p.get("slPrice") or p.get("stopLossPrice") or p.get("sl") or 0)
                if curr_sl > 0 and self._last_sl == 0:
                    self._last_sl = curr_sl

                price = self.client.get_last_price(pos.symbol)
                if price <= 0:
                    continue

                if cfg.breakeven_enabled and not self._be_done:
                    self._maybe_breakeven(pos, cfg, price)

                if cfg.trailing_enabled:
                    self._maybe_trailing(pos, cfg, price)

            except Exception as e:
                print(f"⚠️ Monitor {self.symbol}: {e}")

    def _tighten_sl(self, pos: OpenPosition, new_sl: Decimal) -> None:
        s = pos.side.upper()
        qp = pos.quote_precision

        try:
            price = self.client.get_last_price(pos.symbol)
        except Exception:
            price = Decimal("0")

        if price > 0:
            new_sl = clamp_sl_not_instant(s, new_sl, price, qp)

        if self._last_sl > 0:
            if s == "LONG" and new_sl <= self._last_sl:
                return
            if s == "SHORT" and new_sl >= self._last_sl:
                return

        sl_str = fmt_decimal(new_sl, qp)
        self.client.modify_position_sl(pos.symbol, pos.position_id, sl_str)
        self._last_sl = new_sl
        print(f"🔒 {pos.symbol} {s}: SL -> {sl_str}")

    def _maybe_breakeven(self, pos: OpenPosition, cfg: PairConfig, price: Decimal) -> None:
        s = pos.side.upper()
        entry = pos.entry_price
        trigger = Decimal(str(cfg.breakeven_trigger_pct))
        offset = Decimal(str(cfg.breakeven_offset_pct))

        if entry <= 0:
            return

        if s == "LONG":
            if price < entry * (Decimal("1") + trigger):
                return
            be_sl = entry * (Decimal("1") + offset)
        else:
            if price > entry * (Decimal("1") - trigger):
                return
            be_sl = entry * (Decimal("1") - offset)

        be_sl = round_down(be_sl, pos.quote_precision)
        try:
            self._tighten_sl(pos, be_sl)
            self._be_done = True
            print(f"🟢 {pos.symbol} {s}: breakeven aplicado")
        except Exception as e:
            print(f"⚠️ {pos.symbol} {s}: breakeven falló: {e}")

    def _maybe_trailing(self, pos: OpenPosition, cfg: PairConfig, price: Decimal) -> None:
        """Trailing por movimiento de precio (tipo BE).

        Activa trailing cuando el precio se aleja de la entrada al menos
        cfg.trailing_trigger_pct (ej: 0.02 = 2%).
        """
        entry = pos.entry_price
        if entry <= 0:
            return

        trigger = Decimal(str(getattr(cfg, 'trailing_trigger_pct', 0.02)))
        step_pct = Decimal(str(cfg.trailing_step_pct))
        dist_pct = Decimal(str(cfg.trailing_distance_pct))
        s = pos.side.upper()

        # --- Activación por movimiento desde entrada ---
        if not self._trail_active:
            if s == "LONG":
                if price < entry * (Decimal("1") + trigger):
                    return
            else:
                if price > entry * (Decimal("1") - trigger):
                    return

            self._trail_active = True
            self._trail_best = price
            self._trail_anchor = price
            print(f"🚀 {pos.symbol} {s}: trailing ACTIVADO (trigger={trigger})")

            if cfg.trailing_move_immediately:
                if s == "LONG":
                    new_sl = price * (Decimal("1") - dist_pct)
                else:
                    new_sl = price * (Decimal("1") + dist_pct)
                new_sl = round_down(new_sl, pos.quote_precision)
                try:
                    self._tighten_sl(pos, new_sl)
                except Exception as e:
                    print(f"⚠️ {pos.symbol} {s}: trailing move inmediato falló: {e}")
            return

        # --- Seguimiento ---
        if s == "LONG":
            if price > self._trail_best:
                self._trail_best = price

            if self._trail_best >= self._trail_anchor * (Decimal("1") + step_pct):
                new_sl = self._trail_best * (Decimal("1") - dist_pct)
                new_sl = round_down(new_sl, pos.quote_precision)
                try:
                    self._tighten_sl(pos, new_sl)
                    self._trail_anchor = self._trail_best
                except Exception as e:
                    print(f"⚠️ {pos.symbol} LONG trailing falló: {e}")
        else:
            if self._trail_best == 0 or price < self._trail_best:
                self._trail_best = price

            if self._trail_best <= self._trail_anchor * (Decimal("1") - step_pct):
                new_sl = self._trail_best * (Decimal("1") + dist_pct)
                new_sl = round_down(new_sl, pos.quote_precision)
                try:
                    self._tighten_sl(pos, new_sl)
                    self._trail_anchor = self._trail_best
                except Exception as e:
                    print(f"⚠️ {pos.symbol} SHORT trailing falló: {e}")


# ---------------------------- executor ----------------------------

class TradeExecutor:
    def __init__(
        self,
        client: BitunixClient,
        config_by_symbol: Dict[str, PairConfig],
        margin_coin: str = "USDT",
        tp_sl_stop_type: str = "LAST_PRICE",
        min_ticks_away: int = 2,
    ) -> None:
        self.client = client
        self.cfgs = {k.upper(): v for k, v in config_by_symbol.items()}
        self.margin_coin = margin_coin
        self.tp_sl_stop_type = tp_sl_stop_type
        self.min_ticks_away = int(min_ticks_away)

        self._monitors_lock = threading.RLock()
        self._monitors: Dict[str, SymbolMonitor] = {}

    def process_enqueued_signal(self, sig: EnqueuedSignal) -> None:
        symbol = sig.symbol.upper()
        cfg = self.cfgs.get(symbol)
        if not cfg:
            print(f"⚠️ {symbol}: no hay config")
            return
        if not cfg.is_enabled:
            print(f"⏭️ {symbol}: deshabilitado")
            return

        # Señales soportadas:
        # - LONG / SHORT (abre/gestiona)
        # - BUY_TP  (cierra LONG)
        # - SELL_TP (cierra SHORT)
        raw = str(
            sig.payload.get("signal")
            or sig.payload.get("action")
            or sig.payload.get("side")
            or ""
        ).upper().strip()

        # fallback: inferir desde el texto del alert si no viene señal explícita
        if raw not in ("LONG", "SHORT", "BUY_TP", "SELL_TP"):
            content = str(
                sig.payload.get("content")
                or sig.payload.get("message")
                or sig.payload.get("alert_message")
                or ""
            ).upper()
            if "BUY TP" in content or "TP ALCISTA" in content:
                raw = "BUY_TP"
            elif "SELL TP" in content or "TP BAJISTA" in content:
                raw = "SELL_TP"
            elif "LONG" in content:
                raw = "LONG"
            elif "SHORT" in content:
                raw = "SHORT"

        if raw not in ("LONG", "SHORT", "BUY_TP", "SELL_TP"):
            print(f"⚠️ {symbol}: señal inválida: {raw}")
            return

        self._ensure_monitor(symbol)

        try:
            if raw in ("BUY_TP", "SELL_TP"):
                target_side = "LONG" if raw == "BUY_TP" else "SHORT"
                self._handle_tp_close(symbol, target_side)
                return

            self._handle_signal(symbol, raw, cfg)

        except Exception as e:
            print(f"❌ {symbol}: error procesando señal {raw}: {e}")

    def _handle_tp_close(self, symbol: str, target_side: str) -> None:
        """
        Cierre por señal de TP manual:
          BUY_TP  => cierra LONG
          SELL_TP => cierra SHORT

        No abre posiciones nuevas. Si la posición actual no coincide, ignora.
        """
        cur_pos = self._get_open_position(symbol)
        if not cur_pos:
            print(f"⏭️ {symbol}: TP {target_side} recibido pero no hay posición abierta")
            return

        if cur_pos.side.upper() != target_side.upper():
            print(f"⏭️ {symbol}: TP {target_side} ignorado (posición actual: {cur_pos.side})")
            return

        # Cancelar TPs pendientes antes de cerrar (evita órdenes colgadas)
        try:
            pending = self.client.get_pending_tpsl_orders(symbol=symbol, limit=200)
        except Exception:
            pending = []

        for o in pending:
            # Solo cancelamos TP (no SL) mirando tpPrice
            tp_price = str(o.get("tpPrice") or "").strip()
            if not tp_price:
                continue
            oid = self.client._extract_id_field(o)
            if not oid:
                continue
            try:
                self.client.cancel_tpsl_order(symbol, oid)
            except Exception as e:
                print(f"⚠️ {symbol}: no pude cancelar TP {oid}: {e}")

        print(f"✅ {symbol}: TP manual -> cerrando {cur_pos.side}")
        self._close_position_market(symbol, cur_pos)

    def _handle_signal(self, symbol: str, side: str, cfg: PairConfig) -> None:
        try:
            self.client.set_margin_mode(symbol, self.margin_coin, cfg.margin_mode)
        except Exception as e:
            print(f"⚠️ {symbol}: no pude set_margin_mode: {e}")

        try:
            self.client.set_leverage(symbol, self.margin_coin, int(cfg.leverage))
        except Exception as e:
            print(f"⚠️ {symbol}: no pude set_leverage: {e}")

        cur_pos = self._get_open_position(symbol)

        if not cur_pos:
            self._open_new_position(symbol, side, cfg)
            return

        if cur_pos.side.upper() == side.upper():
            if cfg.same_side_policy.upper() == "IGNORE":
                print(f"⏭️ {symbol}: ya en {side}. IGNORE")
                return
            print(f"🔁 {symbol}: ya en {side}. RESET_ORDERS")
            self._reset_orders(symbol, cur_pos, cfg)
            return

        print(f"🔄 {symbol}: flip {cur_pos.side} -> {side}")
        self._close_position_market(symbol, cur_pos)
        self._open_new_position(symbol, side, cfg)

    def _get_open_position(self, symbol: str) -> Optional[OpenPosition]:
        pos_list = self.client.get_pending_positions(symbol)
        nonzero = [p for p in pos_list if abs(_d(p.get("qty"))) > 0]
        if not nonzero:
            return None

        nonzero.sort(key=lambda p: abs(_d(p.get("qty"))), reverse=True)
        p = nonzero[0]

        side = str(p.get("side") or "").upper()
        if side == "BUY":
            side = "LONG"
        if side == "SELL":
            side = "SHORT"

        position_id = str(p.get("positionId") or "")
        qty = abs(_d(p.get("qty")))
        entry = _d(p.get("avgOpenPrice") or p.get("entryPrice") or 0)

        info = self.client.get_symbol_info(symbol)
        bp = int(info.get("basePrecision", 0))
        qp = int(info.get("quotePrecision", 0))

        return OpenPosition(
            symbol=symbol,
            position_id=position_id,
            side=side,
            entry_price=entry,
            initial_qty=qty,
            base_precision=bp,
            quote_precision=qp,
            margin_coin=self.margin_coin,
        )

    def _calc_qty(self, symbol: str, cfg: PairConfig, last_price: Decimal, base_precision: int, min_trade_volume: Decimal) -> Decimal:
        t = cfg.order_size_type.upper()
        v = Decimal(str(cfg.order_size_value))

        if t == "MARGIN_USDT":
            margin = v
            notional = margin * Decimal(str(cfg.leverage))
        elif t == "NOTIONAL_USDT":
            notional = v
        elif t == "PCT_BALANCE":
            available = self.client.get_account_available(self.margin_coin)
            margin = available * v
            notional = margin * Decimal(str(cfg.leverage))
        else:
            raise ValueError(f"{symbol}: order_size_type inválido: {cfg.order_size_type}")

        if last_price <= 0:
            raise RuntimeError(f"{symbol}: last_price inválido")

        qty = notional / last_price
        qty = round_down(qty, base_precision)

        if min_trade_volume > 0 and qty < min_trade_volume:
            qty = round_down(min_trade_volume, base_precision)

        return qty

    def _open_new_position(self, symbol: str, side: str, cfg: PairConfig) -> None:
        info = self.client.get_symbol_info(symbol)
        bp = int(info.get("basePrecision", 0))
        qp = int(info.get("quotePrecision", 0))
        min_trade_volume = _d(info.get("minTradeVolume") or 0)

        last_price = self.client.get_last_price(symbol)
        qty = self._calc_qty(symbol, cfg, last_price, bp, min_trade_volume)
        if qty <= 0:
            raise RuntimeError(f"{symbol}: qty calculada <= 0")

        qty_str = fmt_decimal(qty, bp)

        open_ts_ms = int(time.time() * 1000)
        prov_ids: List[str] = []
        sl_prov_str: Optional[str] = None

        if cfg.sl_enabled:
            sl_pct = Decimal(str(cfg.sl_pct))
            sl_prov = compute_sl_from_entry(last_price, qp, side, sl_pct)
            sl_prov = clamp_sl_not_instant(side, sl_prov, last_price, qp, self.min_ticks_away)
            sl_prov_str = fmt_decimal(sl_prov, qp)

            print(f"▶️ {symbol}: OPEN {side} MARKET qty={qty_str} con SL provisional={sl_prov_str}")
            out = self.client.open_market_with_provisional_sl(
                symbol=symbol,
                qty=qty_str,
                position_side=side,
                sl_price=sl_prov_str,
                sl_stop_type=self.tp_sl_stop_type,
                sl_order_type="MARKET",
            )
        else:
            print(f"▶️ {symbol}: OPEN {side} MARKET qty={qty_str} (sin SL provisional)")
            out = self.client.open_market(symbol=symbol, qty=qty_str, position_side=side, trade_side="OPEN")

        order_id = str((out or {}).get("orderId") or "")
        if not order_id:
            order_id = self.client.extract_order_id(out)
        if not order_id:
            raise RuntimeError(f"{symbol}: no recibí orderId al abrir")

        od = self._wait_order_filled(order_id, timeout_sec=60)
        trade_qty = _d(od.get("tradeQty"))
        fill_price = self._get_fill_price(od)
        if fill_price <= 0:
            fill_price = last_price

        if cfg.sl_enabled and sl_prov_str:
            prov_ids = self.client.capture_provisional_sl_ids(
                symbol=symbol,
                sl_price_str=sl_prov_str,
                since_ms=open_ts_ms - 60_000,
            )

        pos = self._wait_position(symbol, approx_qty=(trade_qty if trade_qty > 0 else qty), timeout_sec=45, prefer_side=side)
        if not pos:
            raise RuntimeError(f"{symbol}: no apareció la posición (quizá se cerró por SL provisional)")

        position_id = str(pos.get("positionId") or "")
        pos_qty = abs(_d(pos.get("qty")))
        entry_price = _d(pos.get("avgOpenPrice") or fill_price)

        if not position_id or pos_qty <= 0:
            raise RuntimeError(f"{symbol}: positionId/qty inválidos: {pos}")

        pos_sl_order_id = ""
        if cfg.sl_enabled:
            sl_pct = Decimal(str(cfg.sl_pct))
            sl_pos = compute_sl_from_entry(entry_price, qp, side, sl_pct)

            cur = self.client.get_last_price(symbol)
            if cur > 0:
                sl_pos = clamp_sl_not_instant(side, sl_pos, cur, qp, self.min_ticks_away)

            sl_pos_str = fmt_decimal(sl_pos, qp)
            print(f"🛡️ {symbol}: SL de POSICIÓN -> {sl_pos_str}")
            pos_sl_order_id = self.client.ensure_position_sl(symbol, position_id, sl_pos_str, sl_stop_type=self.tp_sl_stop_type)

        if cfg.tp_enabled and cfg.tp_levels:
            self._place_tps(symbol, position_id, side, entry_price, bp, qp, pos_qty, cfg.tp_levels)

        if prov_ids:
            for oid in prov_ids:
                if pos_sl_order_id and oid == pos_sl_order_id:
                    continue
                try:
                    self.client.cancel_tpsl_order(symbol, oid)
                except Exception as e:
                    print(f"⚠️ {symbol}: no pude cancelar SL provisional {oid}: {e}")

        self._set_monitor_position(
            symbol,
            OpenPosition(
                symbol=symbol,
                position_id=position_id,
                side=side,
                entry_price=entry_price,
                initial_qty=pos_qty,
                base_precision=bp,
                quote_precision=qp,
                margin_coin=self.margin_coin,
            ),
            cfg,
        )

        print(f"✅ {symbol}: posición {side} lista | positionId={position_id} | qty={pos_qty} | entry={entry_price}")

    def _close_position_market(self, symbol: str, pos: OpenPosition) -> None:
        """
        FIX: Bitunix CLOSE requiere position_id y el side de cierre lo maneja el client.
        """
        # refrescar qty real (por si hubo TPs parciales)
        cur = self._get_open_position(symbol)
        if not cur:
            print(f"⚠️ {symbol}: no veo posición para cerrar")
            self._set_monitor_position(symbol, None, None)
            return

        qty = cur.initial_qty
        if qty <= 0:
            print(f"⚠️ {symbol}: qty=0, nada que cerrar")
            self._set_monitor_position(symbol, None, None)
            return

        qty_str = fmt_decimal(qty, cur.base_precision)
        print(f"⛔ {symbol}: CLOSE {cur.side} MARKET qty={qty_str}")

        # ✅ FIX REAL: pasamos position_id
        self.client.close_market(
            symbol=symbol,
            qty=qty_str,
            position_side=cur.side,
            position_id=cur.position_id,
        )

        self._set_monitor_position(symbol, None, None)

    def _reset_orders(self, symbol: str, pos: OpenPosition, cfg: PairConfig) -> None:
        cur = self._get_open_position(symbol)
        if not cur:
            return

        entry = cur.entry_price
        qty = cur.initial_qty
        bp = cur.base_precision
        qp = cur.quote_precision
        side = cur.side

        try:
            pending = self.client.get_pending_tpsl_orders(symbol=symbol, limit=200)
        except Exception:
            pending = []

        for o in pending:
            tpp = str(o.get("tpPrice") or "").strip()
            if not tpp:
                continue
            oid = self.client._extract_id_field(o)
            if not oid:
                continue
            try:
                self.client.cancel_tpsl_order(symbol, oid)
            except Exception as e:
                print(f"⚠️ {symbol}: no pude cancelar TP {oid}: {e}")

        if cfg.sl_enabled:
            sl_pct = Decimal(str(cfg.sl_pct))
            new_sl = compute_sl_from_entry(entry, qp, side, sl_pct)
            try:
                cur_price = self.client.get_last_price(symbol)
                if cur_price > 0:
                    new_sl = clamp_sl_not_instant(side, new_sl, cur_price, qp, self.min_ticks_away)
            except Exception:
                pass
            sl_str = fmt_decimal(new_sl, qp)
            try:
                self.client.ensure_position_sl(symbol, cur.position_id, sl_str, sl_stop_type=self.tp_sl_stop_type)
            except Exception as e:
                print(f"⚠️ {symbol}: no pude resetear SL: {e}")

        if cfg.tp_enabled and cfg.tp_levels:
            self._place_tps(symbol, cur.position_id, side, entry, bp, qp, qty, cfg.tp_levels)

        self._set_monitor_position(symbol, cur, cfg)

    def _place_tps(
        self,
        symbol: str,
        position_id: str,
        side: str,
        entry_price: Decimal,
        base_precision: int,
        quote_precision: int,
        total_qty: Decimal,
        levels: List[TPLevel],
    ) -> None:
        lv = [x for x in levels if x.is_enabled]
        if not lv or total_qty <= 0:
            return

        used = Decimal("0")
        qtys: List[Decimal] = []

        for t in lv:
            q = round_down(total_qty * Decimal(str(t.close_frac)), base_precision)
            if q <= 0:
                q = Decimal("0")
            qtys.append(q)
            used += q

        runner = round_down(total_qty - used, base_precision)
        if runner < 0:
            runner = Decimal("0")

        info = self.client.get_symbol_info(symbol)
        min_trade_volume = _d(info.get("minTradeVolume") or 0)
        if min_trade_volume > 0 and runner > 0 and runner < min_trade_volume and qtys:
            qtys[-1] = round_down(qtys[-1] + runner, base_precision)
            runner = Decimal("0")

        for t, q in zip(lv, qtys):
            if q <= 0:
                continue
            tp_price = compute_tp_from_entry(entry_price, quote_precision, side, Decimal(str(t.target_pct)))
            tp_price_str = fmt_decimal(tp_price, quote_precision)
            tp_qty_str = fmt_decimal(q, base_precision)
            try:
                self.client.place_tp_partial(
                    symbol=symbol,
                    position_id=position_id,
                    tp_price=tp_price_str,
                    tp_qty=tp_qty_str,
                    tp_stop_type=self.tp_sl_stop_type,
                    tp_order_type="MARKET",
                )
                print(f"🎯 {symbol}: TP{t.level} price={tp_price_str} qty={tp_qty_str}")
            except Exception as e:
                print(f"⚠️ {symbol}: fallo TP{t.level}: {e}")

        if runner > 0:
            print(f"🏃 {symbol}: runner qty={fmt_decimal(runner, base_precision)} (sin TP)")

    def _wait_order_filled(self, order_id: str, timeout_sec: int = 60) -> Dict[str, Any]:
        t0 = time.time()
        last: Dict[str, Any] = {}
        while time.time() - t0 <= timeout_sec:
            od = self.client.get_order_detail(order_id)
            last = od if isinstance(od, dict) else {}
            status = str(last.get("status", "")).upper()
            trade_qty = _d(last.get("tradeQty"))
            if status in ("FILLED", "PART_FILLED") and trade_qty > 0:
                return last
            if status == "CANCELED":
                raise RuntimeError(f"orden {order_id} CANCELED")
            time.sleep(1.5)
        return last

    def _get_fill_price(self, od: Dict[str, Any]) -> Decimal:
        for k in ("avgPrice", "avgTradePrice", "avgDealPrice", "avgFillPrice"):
            v = _d(od.get(k))
            if v > 0:
                return v
        deal_money = _d(od.get("dealMoney") or od.get("tradeAmount") or od.get("amount"))
        trade_qty = _d(od.get("tradeQty"))
        if deal_money > 0 and trade_qty > 0:
            return deal_money / trade_qty
        return Decimal("0")

    def _wait_position(self, symbol: str, approx_qty: Decimal, timeout_sec: int, prefer_side: Optional[str]) -> Optional[Dict[str, Any]]:
        t0 = time.time()
        while time.time() - t0 <= timeout_sec:
            pos = self.client.get_pending_positions(symbol)
            nonzero = [p for p in pos if abs(_d(p.get("qty"))) > 0]
            if nonzero:
                candidates = nonzero
                if prefer_side:
                    preferred = [p for p in nonzero if side_matches(prefer_side, str(p.get("side", "")))]
                    if preferred:
                        candidates = preferred
                candidates.sort(key=lambda p: abs(abs(_d(p.get("qty"))) - approx_qty))
                return candidates[0]
            time.sleep(1.5)
        return None

    def _ensure_monitor(self, symbol: str) -> None:
        with self._monitors_lock:
            if symbol in self._monitors:
                return
            self._monitors[symbol] = SymbolMonitor(self.client, symbol)

    def _set_monitor_position(self, symbol: str, pos: Optional[OpenPosition], cfg: Optional[PairConfig]) -> None:
        with self._monitors_lock:
            mon = self._monitors.get(symbol)
        if mon:
            mon.set_position(pos, cfg)
