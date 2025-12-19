# bitunix_client.py
# -*- coding: utf-8 -*-
"""
Cliente REAL para Bitunix Futures (sin mocks).

FIX importante:
- Cierre de posición (tradeSide=CLOSE):
  - requiere positionId
  - para cerrar LONG => side="BUY"
  - para cerrar SHORT => side="SELL"
  Doc: Place Order /trade/place_order  :contentReference[oaicite:2]{index=2}
"""

from __future__ import annotations

import time
import uuid
import json
import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests


class BitunixClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.bitunix.com",
        timeout_sec: int = 20,
        user_agent: str = "bitunix-bot/real/1.0",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        if not self.api_key or not self.api_secret:
            raise ValueError("Faltan api_key/api_secret")

        self.base_url = base_url.rstrip("/")
        self.timeout = int(timeout_sec)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # ----------------- helpers -----------------

    @staticmethod
    def _d(x: Any) -> Decimal:
        try:
            return Decimal(str(x))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    @staticmethod
    def _sha256_hex(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _body_for_sign(body: Optional[Dict[str, Any]]) -> str:
        if not body:
            return ""
        return json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _qp_for_sign(params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return ""
        parts: List[str] = []
        for k in sorted(params.keys(), key=lambda x: str(x)):
            v = params[k]
            if v is None:
                continue
            parts.append(f"{k}{v}")
        return "".join(parts)

    def _sign_request(
        self,
        nonce: str,
        timestamp: str,
        qp_for_sign: str,
        body_for_sign: str,
    ) -> str:
        digest = self._sha256_hex(nonce + timestamp + self.api_key + qp_for_sign + body_for_sign)
        return self._sha256_hex(digest + self.api_secret)

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        method = method.upper()
        url = self.base_url + path

        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))

        qp_for_sign = self._qp_for_sign(params)
        body_for_sign = self._body_for_sign(body)
        sign = self._sign_request(nonce, timestamp, qp_for_sign, body_for_sign)

        headers = {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
            "language": "en-US",
            "Content-Type": "application/json",
        }

        if method == "GET":
            r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        else:
            r = self.session.request(method, url, params=params, headers=headers, data=body_for_sign, timeout=self.timeout)

        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"HTTP {r.status_code} no JSON: {r.text[:400]}")

        if data.get("code") != 0:
            raise RuntimeError(f"API error code={data.get('code')} msg={data.get('msg')} data={data.get('data')}")
        return data.get("data")

    def _public_request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Public API error code={data.get('code')} msg={data.get('msg')} data={data.get('data')}")
        return data.get("data")

    @staticmethod
    def extract_order_id(resp: Any) -> str:
        if resp is None:
            return ""
        if isinstance(resp, dict):
            return str(resp.get("orderId") or resp.get("id") or "")
        if isinstance(resp, list):
            if not resp:
                return ""
            first = resp[0]
            if isinstance(first, dict):
                return str(first.get("orderId") or first.get("id") or "")
            return str(first)
        return ""

    @staticmethod
    def _extract_id_field(o: Dict[str, Any]) -> str:
        return str(o.get("id") or o.get("orderId") or "")

    # ----------------- public market data -----------------

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        data = self._public_request("/api/v1/futures/market/trading_pairs", {"symbols": symbol})
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"No encontré info del símbolo {symbol}")
        return next((x for x in data if str(x.get("symbol", "")).upper() == symbol.upper()), data[0])

    def get_last_price(self, symbol: str) -> Decimal:
        data = self._public_request("/api/v1/futures/market/tickers", {"symbols": symbol})
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"No pude leer ticker para {symbol}")
        t = next((x for x in data if str(x.get("symbol", "")).upper() == symbol.upper()), data[0])
        return self._d(t.get("lastPrice") or t.get("last") or t.get("markPrice"))

    # ----------------- account / position -----------------

    def get_account_available(self, margin_coin: str = "USDT") -> Decimal:
        data = self._signed_request("GET", "/api/v1/futures/account", {"marginCoin": margin_coin})
        if not isinstance(data, list) or not data:
            return Decimal("0")
        return self._d(data[0].get("available"))

    def get_pending_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._signed_request("GET", "/api/v1/futures/position/get_pending_positions", params)
        if not isinstance(data, list):
            return []
        return data

    def get_pending_tpsl_orders(self, symbol: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": int(limit), "skip": 0}
        if symbol:
            params["symbol"] = symbol
        data = self._signed_request("GET", "/api/v1/futures/tpsl/get_pending_orders", params)
        if not isinstance(data, list):
            return []
        return data

    def get_order_detail(self, order_id: str) -> Dict[str, Any]:
        data = self._signed_request("GET", "/api/v1/futures/trade/get_order_detail", {"orderId": order_id})
        if not isinstance(data, dict):
            raise RuntimeError(f"Respuesta rara en get_order_detail: {data}")
        return data

    # ----------------- margin/leverage -----------------

    def set_margin_mode(self, symbol: str, margin_coin: str, margin_mode: str) -> None:
        payload = {"marginMode": margin_mode, "symbol": symbol, "marginCoin": margin_coin}
        self._signed_request("POST", "/api/v1/futures/account/change_margin_mode", body=payload)

    def set_leverage(self, symbol: str, margin_coin: str, leverage: int) -> None:
        payload = {"symbol": symbol, "leverage": int(leverage), "marginCoin": margin_coin}
        self._signed_request("POST", "/api/v1/futures/account/change_leverage", body=payload)

    # ----------------- trading: open/close market -----------------

    def open_market(
        self,
        symbol: str,
        qty: str,
        position_side: str,      # LONG | SHORT
        trade_side: str = "OPEN",
    ) -> Dict[str, Any]:
        # apertura estándar: LONG=BUY, SHORT=SELL
        side = "BUY" if position_side.upper() == "LONG" else "SELL"
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "tradeSide": trade_side,   # OPEN
            "orderType": "MARKET",
        }
        return self._signed_request("POST", "/api/v1/futures/trade/place_order", body=payload)

    def close_market(
        self,
        symbol: str,
        qty: str,
        position_side: str,      # LONG | SHORT (posición actual)
        position_id: str,        # REQUIRED para tradeSide=CLOSE :contentReference[oaicite:3]{index=3}
    ) -> Dict[str, Any]:
        """
        Cierre en hedge-mode (Bitunix):
          - close long  => side="BUY",  tradeSide="CLOSE"
          - close short => side="SELL", tradeSide="CLOSE"
        y requiere positionId. :contentReference[oaicite:4]{index=4}
        """
        ps = position_side.upper()
        if ps not in ("LONG", "SHORT"):
            raise ValueError(f"position_side inválido: {position_side}")
        if not position_id:
            raise ValueError("position_id requerido para CLOSE")

        side = "BUY" if ps == "LONG" else "SELL"

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "tradeSide": "CLOSE",
            "positionId": position_id,
            "orderType": "MARKET",
            "reduceOnly": True,
        }
        return self._signed_request("POST", "/api/v1/futures/trade/place_order", body=payload)

    def open_market_with_provisional_sl(
        self,
        symbol: str,
        qty: str,
        position_side: str,      # LONG | SHORT
        sl_price: str,
        sl_stop_type: str = "LAST_PRICE",
        sl_order_type: str = "MARKET",
    ) -> Dict[str, Any]:
        side = "BUY" if position_side.upper() == "LONG" else "SELL"
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "tradeSide": "OPEN",
            "orderType": "MARKET",
            "slPrice": sl_price,
            "slStopType": sl_stop_type,
            "slOrderType": sl_order_type,
        }
        return self._signed_request("POST", "/api/v1/futures/trade/place_order", body=payload)

    # ----------------- SL de posición -----------------

    def place_position_sl(
        self,
        symbol: str,
        position_id: str,
        sl_price: str,
        sl_stop_type: str = "LAST_PRICE",
    ) -> str:
        payload = {"symbol": symbol, "positionId": position_id, "slPrice": sl_price, "slStopType": sl_stop_type}
        out = self._signed_request("POST", "/api/v1/futures/tpsl/position/place_order", body=payload)
        return self.extract_order_id(out)

    def modify_position_sl(
        self,
        symbol: str,
        position_id: str,
        sl_price: str,
        sl_stop_type: str = "LAST_PRICE",
    ) -> str:
        payload = {"symbol": symbol, "positionId": position_id, "slPrice": sl_price, "slStopType": sl_stop_type}
        out = self._signed_request("POST", "/api/v1/futures/tpsl/position/modify_order", body=payload)
        return self.extract_order_id(out)

    def ensure_position_sl(
        self,
        symbol: str,
        position_id: str,
        sl_price: str,
        sl_stop_type: str = "LAST_PRICE",
    ) -> str:
        try:
            oid = self.place_position_sl(symbol, position_id, sl_price, sl_stop_type=sl_stop_type)
            if oid:
                return oid
        except Exception:
            pass
        return self.modify_position_sl(symbol, position_id, sl_price, sl_stop_type=sl_stop_type)

    # ----------------- TP reduce-only -----------------

    def place_tp_partial(
        self,
        symbol: str,
        position_id: str,
        tp_price: str,
        tp_qty: str,
        tp_stop_type: str = "LAST_PRICE",
        tp_order_type: str = "MARKET",
    ) -> str:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "positionId": position_id,
            "tpPrice": tp_price,
            "tpStopType": tp_stop_type,
            "tpOrderType": tp_order_type,
            "tpQty": tp_qty,
        }
        out = self._signed_request("POST", "/api/v1/futures/tpsl/place_order", body=payload)
        return self.extract_order_id(out)

    # ----------------- cancelación tpsl -----------------

    def cancel_tpsl_order(self, symbol: str, tpsl_id: str) -> Any:
        payload1 = {"symbol": symbol, "orderId": tpsl_id}
        try:
            return self._signed_request("POST", "/api/v1/futures/tpsl/cancel_order", body=payload1)
        except Exception:
            payload2 = {"symbol": symbol, "id": tpsl_id}
            return self._signed_request("POST", "/api/v1/futures/tpsl/cancel_order", body=payload2)

    # ----------------- util: capturar SL provisional -----------------

    def capture_provisional_sl_ids(
        self,
        symbol: str,
        sl_price_str: str,
        since_ms: int,
        tries: int = 6,
        sleep_sec: float = 1.0,
    ) -> List[str]:
        ids: List[str] = []

        for _ in range(max(1, int(tries))):
            try:
                pending = self.get_pending_tpsl_orders(symbol=symbol, limit=200)
            except Exception:
                pending = []

            for o in pending:
                if str(o.get("symbol", "")).upper() != symbol.upper():
                    continue

                ctime = 0
                for k in ("createTime", "ctime", "time", "mtime"):
                    if o.get(k) is not None:
                        try:
                            ctime = int(o.get(k))
                            break
                        except Exception:
                            pass
                if ctime and ctime < since_ms:
                    continue

                slp = str(o.get("slPrice") or "").strip()
                tpp = str(o.get("tpPrice") or "").strip()
                slq = self._d(o.get("slQty") or 0)

                if slp and slq > 0 and not tpp:
                    if sl_price_str and slp != sl_price_str:
                        continue
                    oid = self._extract_id_field(o)
                    if oid and oid not in ids:
                        ids.append(oid)

            if ids:
                break
            time.sleep(sleep_sec)

        return ids
