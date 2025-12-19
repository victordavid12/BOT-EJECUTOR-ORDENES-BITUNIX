# symbol_queue.py
# -*- coding: utf-8 -*-
"""
Cola FIFO en memoria por símbolo.

Reglas:
- Cada símbolo tiene su propia cola.
- No se procesa una señal si la anterior del MISMO símbolo no terminó.
- Diferentes símbolos sí pueden procesarse en paralelo.

Implementación:
- Un worker thread por símbolo (se crea bajo demanda).
- Cada worker consume su Queue FIFO y ejecuta el callback de procesamiento.
- Si el callback falla, se loguea y se continúa con la siguiente señal (no se muere el worker).
"""

from __future__ import annotations

import threading
import queue
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class EnqueuedSignal:
    symbol: str
    payload: dict
    received_ts: float


class SymbolQueueManager:
    def __init__(
        self,
        processor: Callable[[EnqueuedSignal], None],
        max_queue_per_symbol: int = 500,
        daemon_workers: bool = True,
    ) -> None:
        """
        processor: función que procesa UNA señal (bloqueante). Se llama en el worker del símbolo.
        max_queue_per_symbol: límite duro FIFO por símbolo (si se llena, se rechaza la señal).
        """
        self._processor = processor
        self._max_q = int(max_queue_per_symbol)
        self._daemon = bool(daemon_workers)

        self._lock = threading.RLock()
        self._queues: Dict[str, "queue.Queue[EnqueuedSignal]"] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, threading.Event] = {}

    def enqueue(self, signal: EnqueuedSignal) -> bool:
        """
        Encola una señal. Devuelve:
        - True si se encoló
        - False si se rechazó por cola llena
        """
        symbol = (signal.symbol or "").upper().strip()
        if not symbol:
            raise ValueError("signal.symbol vacío")

        with self._lock:
            q = self._queues.get(symbol)
            if q is None:
                q = queue.Queue(maxsize=self._max_q)
                self._queues[symbol] = q

            # cola llena -> rechazo
            if q.full():
                return False

            q.put_nowait(signal)

            # crea worker si no existe
            if symbol not in self._threads or not self._threads[symbol].is_alive():
                stop_ev = threading.Event()
                self._stop_flags[symbol] = stop_ev
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(symbol, stop_ev),
                    name=f"symbol-worker:{symbol}",
                    daemon=self._daemon,
                )
                self._threads[symbol] = t
                t.start()

            return True

    def qsize(self, symbol: str) -> int:
        symbol = (symbol or "").upper().strip()
        with self._lock:
            q = self._queues.get(symbol)
            return q.qsize() if q else 0

    def stop_symbol(self, symbol: str) -> None:
        """
        Señala al worker de un símbolo que pare (cuando pueda).
        Nota: no elimina la cola; solo detiene el thread.
        """
        symbol = (symbol or "").upper().strip()
        with self._lock:
            ev = self._stop_flags.get(symbol)
            if ev:
                ev.set()

    def stop_all(self) -> None:
        """
        Señala a todos los workers que paren.
        """
        with self._lock:
            for ev in self._stop_flags.values():
                ev.set()

    def _worker_loop(self, symbol: str, stop_ev: threading.Event) -> None:
        """
        Loop FIFO del símbolo. Garantiza serialización por símbolo.
        """
        while not stop_ev.is_set():
            sig: Optional[EnqueuedSignal] = None

            # leer cola (con timeout para poder observar stop_ev)
            with self._lock:
                q = self._queues.get(symbol)

            if q is None:
                return

            try:
                sig = q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._processor(sig)  # BLOQUEANTE
            except Exception as e:
                # No matamos el worker; seguimos con la siguiente
                print(f"⚠️ Worker {symbol}: error procesando señal: {e}")
            finally:
                try:
                    q.task_done()
                except Exception:
                    pass
