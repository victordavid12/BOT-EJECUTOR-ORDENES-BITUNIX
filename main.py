# main_gui.py
# -*- coding: utf-8 -*-
"""
GUI (PySide6/Qt) - Control + Config de bot Bitunix Futures

✅ Opción A: la GUI controla el bot como SUBPROCESO
✅ Logs en vivo: Python unbuffered (-u) + PYTHONUNBUFFERED=1
✅ Emojis OK en Windows: UTF-8 forzado (PYTHONUTF8 / PYTHONIOENCODING)
✅ stdout + stderr mezclados: QProcess.MergedChannels

✅ Editor SQLite:
- pairs_config (CRUD)
- tp_levels (CRUD por símbolo)

✅ UI numérica “humana”:
- Campos %: usuario escribe 1 => se guarda 0.01
- Campos frac%: usuario escribe 30 => se guarda 0.30
- order_size_value si PCT_BALANCE: 10 => 0.10
"""

from __future__ import annotations

import os
import sys
import sqlite3
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QProcess, QTimer, QProcessEnvironment
from PySide6.QtGui import QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (
    QStyledItemDelegate,
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QTabWidget, QMessageBox,
    QTableView, QHeaderView, QComboBox, QSplitter,
    QFileDialog
)

# --------------------------- Paths ---------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_DB_PATH = os.path.join(BASE_DIR, "bot_config.db")
DEFAULT_APP_PATH = os.path.join(BASE_DIR, "app.py")

PYTHON_EXE = sys.executable


# --------------------------- DB columns ---------------------------

PAIRS_COLUMNS = [
    ("symbol", "symbol", "str"),
    ("is_enabled", "enabled (0/1)", "bool01"),
    ("margin_mode", "margin_mode (ISOLATION/CROSS)", "str"),
    ("leverage", "leverage", "int"),
    ("order_size_type", "order_size_type", "str"),
    ("order_size_value", "order_size_value", "float"),
    ("sl_enabled", "sl_enabled (0/1)", "bool01"),
    ("sl_pct", "sl_pct (%)", "pct"),
    ("tp_enabled", "tp_enabled (0/1)", "bool01"),

    ("breakeven_enabled", "breakeven_enabled (0/1)", "bool01"),
    ("breakeven_trigger_pct", "breakeven_trigger_pct (%)", "pct"),
    ("breakeven_offset_pct", "breakeven_offset_pct (%)", "pct"),

    ("trailing_enabled", "trailing_enabled (0/1)", "bool01"),
    ("trailing_trigger_pct", "trailing_trigger_pct (%)", "pct"),

    ("trailing_step_pct", "trailing_step_pct (%)", "pct"),
    ("trailing_distance_pct", "trailing_distance_pct (%)", "pct"),
    ("trailing_move_immediately", "trailing_move_immediately (0/1)", "bool01"),

    ("same_side_policy", "same_side_policy (IGNORE/RESET_ORDERS)", "str"),
]

TP_COLUMNS = [
    ("symbol", "symbol", "str"),
    ("level", "level", "int"),
    ("target_pct", "target_pct (%)", "pct"),
    ("close_frac", "close_frac (%)", "fracpct"),
    ("is_enabled", "enabled (0/1)", "bool01"),
]


# --------------------------- Conversions ---------------------------

def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    try:
        s = str(x).strip().replace(",", ".")
        if s == "":
            return 0.0
        return float(s)
    except Exception:
        return 0.0


def _to_int(x: Any) -> int:
    try:
        return int(float(str(x).strip().replace(",", ".")))
    except Exception:
        return 0


def _to_bool01(x: Any) -> int:
    if x is None:
        return 0
    s = str(x).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on", "enabled", "enable"):
        return 1
    if s in ("0", "false", "f", "no", "n", "off", "disabled", "disable"):
        return 0
    try:
        v = int(float(s.replace(",", ".")))
        return 1 if v != 0 else 0
    except Exception:
        return 0


def ui_to_db(value: Any, ftype: str) -> Any:
    if ftype == "pct":
        return _to_float(value) / 100.0
    if ftype == "fracpct":
        return _to_float(value) / 100.0
    if ftype == "bool01":
        return _to_bool01(value)
    if ftype == "int":
        return _to_int(value)
    if ftype == "float":
        return _to_float(value)
    return ("" if value is None else str(value).strip())


def db_to_ui(value: Any, ftype: str) -> str:
    if value is None:
        return ""
    if ftype in ("pct", "fracpct"):
        return str(_to_float(value) * 100.0)
    if ftype == "bool01":
        return "Enabled" if _to_bool01(value) else "Disabled"
    if ftype == "int":
        return str(_to_int(value))
    if ftype == "float":
        return str(_to_float(value))
    return str(value)


def order_size_value_ui_to_db(order_size_type: str, ui_value: Any) -> float:
    t = (order_size_type or "").upper().strip()
    v = _to_float(ui_value)
    if t == "PCT_BALANCE":
        return v / 100.0
    return v


def order_size_value_db_to_ui(order_size_type: str, db_value: Any) -> str:
    t = (order_size_type or "").upper().strip()
    v = _to_float(db_value)
    if t == "PCT_BALANCE":
        return str(v * 100.0)
    return str(v)



# --------------------------- Delegates ---------------------------

class BoolComboDelegate(QStyledItemDelegate):
    """Editor tipo combo para columnas booleanas (Enabled/Disabled)."""

    def __init__(self, parent=None):
        super().__init__(parent)

    def createEditor(self, parent, option, index):  # type: ignore[override]
        combo = QComboBox(parent)
        combo.addItems(["Disabled", "Enabled"])
        combo.setEditable(False)
        return combo

    def setEditorData(self, editor, index):  # type: ignore[override]
        if not isinstance(editor, QComboBox):
            return super().setEditorData(editor, index)
        v = str(index.data() or "").strip().lower()
        enabled = v in ("1", "true", "enabled", "on", "yes", "y")
        editor.setCurrentIndex(1 if enabled else 0)

    def setModelData(self, editor, model, index):  # type: ignore[override]
        if not isinstance(editor, QComboBox):
            return super().setModelData(editor, model, index)
        model.setData(index, editor.currentText())

    def displayText(self, value, locale):  # type: ignore[override]
        v = str(value or "").strip().lower()
        enabled = v in ("1", "true", "enabled", "on", "yes", "y")
        return "Enabled" if enabled else "Disabled"

# --------------------------- DB helpers ---------------------------

def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables_exist(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for t in ("pairs_config", "tp_levels"):
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
        if not cur.fetchone():
            raise RuntimeError(f"No existe la tabla '{t}' en la DB.")


def load_pairs(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM pairs_config ORDER BY symbol")
    return cur.fetchall()


def load_tp_levels(conn: sqlite3.Connection, symbol: Optional[str] = None) -> List[sqlite3.Row]:
    cur = conn.cursor()
    if symbol:
        cur.execute("SELECT * FROM tp_levels WHERE symbol=? ORDER BY level", (symbol.upper(),))
    else:
        cur.execute("SELECT * FROM tp_levels ORDER BY symbol, level")
    return cur.fetchall()


def upsert_pair(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    cols = [c[0] for c in PAIRS_COLUMNS]
    symbol = (row.get("symbol") or "").upper().strip()
    if not symbol:
        raise ValueError("symbol vacío")

    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO pairs_config ({','.join(cols)}) VALUES ({placeholders})"
    values = [row.get(c) for c in cols]
    conn.execute(sql, values)


def delete_pair(conn: sqlite3.Connection, symbol: str) -> None:
    sym = (symbol or "").upper().strip()
    if not sym:
        return
    conn.execute("DELETE FROM pairs_config WHERE symbol=?", (sym,))
    conn.execute("DELETE FROM tp_levels WHERE symbol=?", (sym,))


def upsert_tp(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    sym = (row.get("symbol") or "").upper().strip()
    lvl = int(row.get("level") or 0)
    if not sym or lvl <= 0:
        raise ValueError("TP: symbol vacío o level inválido")

    conn.execute("DELETE FROM tp_levels WHERE symbol=? AND level=?", (sym, lvl))

    cols = [c[0] for c in TP_COLUMNS]
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO tp_levels ({','.join(cols)}) VALUES ({placeholders})"
    values = [row.get(c) for c in cols]
    conn.execute(sql, values)


def delete_tp(conn: sqlite3.Connection, symbol: str, level: int) -> None:
    sym = (symbol or "").upper().strip()
    lvl = int(level or 0)
    if not sym or lvl <= 0:
        return
    conn.execute("DELETE FROM tp_levels WHERE symbol=? AND level=?", (sym, lvl))


# --------------------------- GUI ---------------------------

class BotGUI(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bitunix Bot - Control & Config")
        self.resize(1250, 820)

        self.db_path = DEFAULT_DB_PATH
        self.app_path = DEFAULT_APP_PATH

        self.process: Optional[QProcess] = None

        self._build_ui()
        self._refresh_all()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_running_label)
        self.timer.start(400)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        top = QHBoxLayout()
        root_layout.addLayout(top)

        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_restart = QPushButton("Restart")
        self.btn_start.clicked.connect(self.start_bot)
        self.btn_stop.clicked.connect(self.stop_bot)
        self.btn_restart.clicked.connect(self.restart_bot)

        self.lbl_status = QLabel("Status: STOPPED")
        self.lbl_status.setStyleSheet("font-weight: bold;")

        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        top.addWidget(self.btn_restart)
        top.addSpacing(18)
        top.addWidget(self.lbl_status)
        top.addSpacing(18)
        top.addStretch(1)

        # Help (Telegram)
        self.lbl_help = QLabel('<a href="https://t.me/Victtordfg">Help (Telegram)</a>')
        self.lbl_help.setOpenExternalLinks(True)
        root_layout.addWidget(self.lbl_help)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, 1)

        self.tab_logs = QWidget()
        self.tabs.addTab(self.tab_logs, "Logs")
        self._build_logs_tab()

        self.tab_cfg = QWidget()
        self.tabs.addTab(self.tab_cfg, "Config DB")
        self._build_config_tab()

    def _build_logs_tab(self) -> None:
        layout = QVBoxLayout(self.tab_logs)
        bar = QHBoxLayout()
        layout.addLayout(bar)

        self.btn_clear_logs = QPushButton("Clear")
        self.btn_clear_logs.clicked.connect(lambda: self.txt_logs.clear())
        bar.addWidget(self.btn_clear_logs)
        bar.addStretch(1)

        self.txt_logs = QTextEdit()
        self.txt_logs.setReadOnly(True)
        self.txt_logs.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.txt_logs, 1)

    def _build_config_tab(self) -> None:
        layout = QVBoxLayout(self.tab_cfg)

        bar = QHBoxLayout()
        layout.addLayout(bar)

        self.btn_reload_db = QPushButton("Reload DB")
        self.btn_save_db = QPushButton("Save DB")
        self.btn_reload_db.clicked.connect(self._refresh_all)
        self.btn_save_db.clicked.connect(self.save_all)

        bar.addWidget(self.btn_reload_db)
        bar.addWidget(self.btn_save_db)
        bar.addStretch(1)

        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter, 1)

        # ---- pairs_config
        pairs_box = QWidget()
        pairs_layout = QVBoxLayout(pairs_box)
        splitter.addWidget(pairs_box)

        pairs_bar = QHBoxLayout()
        pairs_layout.addLayout(pairs_bar)

        self.btn_add_pair = QPushButton("Add Pair")
        self.btn_del_pair = QPushButton("Delete Pair")
        self.btn_add_pair.clicked.connect(self.add_pair_row)
        self.btn_del_pair.clicked.connect(self.delete_selected_pair)

        pairs_bar.addWidget(QLabel("pairs_config"))
        pairs_bar.addStretch(1)
        pairs_bar.addWidget(self.btn_add_pair)
        pairs_bar.addWidget(self.btn_del_pair)

        self.pairs_model = QStandardItemModel(0, len(PAIRS_COLUMNS))
        self.pairs_model.setHorizontalHeaderLabels([c[1] for c in PAIRS_COLUMNS])

        self.tbl_pairs = QTableView()
        self.tbl_pairs.setModel(self.pairs_model)

        # Estilo: grilla sólida y líneas visibles
        self.tbl_pairs.setShowGrid(True)
        self.tbl_pairs.setGridStyle(Qt.SolidLine)
        self.tbl_pairs.setStyleSheet("QTableView{gridline-color:#B0B0B0;} QTableView::item{border-right:1px solid #B0B0B0; border-bottom:1px solid #B0B0B0;} QHeaderView::section{border:1px solid #B0B0B0; padding:4px;}")
        self.tbl_pairs.verticalHeader().setVisible(False)

        # Delegates: columnas bool01 como combo Enabled/Disabled
        self._bool_delegate = BoolComboDelegate(self.tbl_pairs)
        for cidx, c in enumerate(PAIRS_COLUMNS):
            if c[2] == "bool01":
                self.tbl_pairs.setItemDelegateForColumn(cidx, self._bool_delegate)

        self.tbl_pairs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_pairs.horizontalHeader().setStretchLastSection(True)
        self.tbl_pairs.setSelectionBehavior(QTableView.SelectRows)
        self.tbl_pairs.setSelectionMode(QTableView.SingleSelection)
        self.tbl_pairs.clicked.connect(self._on_pair_selected)
        pairs_layout.addWidget(self.tbl_pairs, 1)

        # ---- tp_levels
        tp_box = QWidget()
        tp_layout = QVBoxLayout(tp_box)
        splitter.addWidget(tp_box)

        tp_bar = QHBoxLayout()
        tp_layout.addLayout(tp_bar)

        self.cmb_symbol = QComboBox()
        self.cmb_symbol.currentTextChanged.connect(self._load_tp_for_symbol)

        self.btn_add_tp = QPushButton("Add TP")
        self.btn_del_tp = QPushButton("Delete TP")
        self.btn_add_tp.clicked.connect(self.add_tp_row)
        self.btn_del_tp.clicked.connect(self.delete_selected_tp)

        tp_bar.addWidget(QLabel("tp_levels | symbol:"))
        tp_bar.addWidget(self.cmb_symbol)
        tp_bar.addStretch(1)
        tp_bar.addWidget(self.btn_add_tp)
        tp_bar.addWidget(self.btn_del_tp)

        self.tp_model = QStandardItemModel(0, len(TP_COLUMNS))
        self.tp_model.setHorizontalHeaderLabels([c[1] for c in TP_COLUMNS])

        self.tbl_tp = QTableView()
        self.tbl_tp.setModel(self.tp_model)

        # Estilo: grilla sólida y líneas visibles
        self.tbl_tp.setShowGrid(True)
        self.tbl_tp.setGridStyle(Qt.SolidLine)
        self.tbl_tp.setStyleSheet("QTableView{gridline-color:#B0B0B0;} QTableView::item{border-right:1px solid #B0B0B0; border-bottom:1px solid #B0B0B0;} QHeaderView::section{border:1px solid #B0B0B0; padding:4px;}")
        self.tbl_tp.verticalHeader().setVisible(False)

        for cidx, c in enumerate(TP_COLUMNS):
            if c[2] == "bool01":
                self.tbl_tp.setItemDelegateForColumn(cidx, self._bool_delegate)

        self.tbl_tp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_tp.horizontalHeader().setStretchLastSection(True)
        self.tbl_tp.setSelectionBehavior(QTableView.SelectRows)
        self.tbl_tp.setSelectionMode(QTableView.SingleSelection)
        tp_layout.addWidget(self.tbl_tp, 1)

        splitter.setSizes([470, 330])

    # ---------- Logging ----------

    def _log(self, s: str) -> None:
        self.txt_logs.moveCursor(QTextCursor.End)
        self.txt_logs.insertPlainText(s)
        self.txt_logs.moveCursor(QTextCursor.End)

    # ---------- Process control ----------

    def start_bot(self) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self._log("⚠️ Bot ya está corriendo.\n")
            return

        if not os.path.isfile(self.app_path):
            QMessageBox.critical(self, "Error", f"No existe app.py en:\n{self.app_path}")
            return

        p = QProcess(self)
        p.setProgram(PYTHON_EXE)

        # ✅ -u = unbuffered (logs en vivo)
        p.setArguments(["-u", self.app_path])
        p.setWorkingDirectory(os.path.dirname(self.app_path))

        # ✅ Emojis + logs vivos en Windows
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        p.setProcessEnvironment(env)

        # ✅ Mezcla stdout+stderr para ver todo en Logs
        p.setProcessChannelMode(QProcess.MergedChannels)

        # leemos solo stdout (ya incluye stderr)
        p.readyReadStandardOutput.connect(lambda: self._read_proc(p))
        p.finished.connect(lambda *_: self._log("\n🛑 Bot process finished.\n"))

        self.process = p
        p.start()

        if not p.waitForStarted(3000):
            QMessageBox.critical(self, "Error", "No pude arrancar el proceso del bot.")
            self.process = None
            return

        self._log(f"▶️ START bot: {PYTHON_EXE} {self.app_path}\n")

    def stop_bot(self) -> None:
        if not self.process or self.process.state() == QProcess.NotRunning:
            self._log("ℹ️ Bot no está corriendo.\n")
            return

        self._log("⛔ STOP bot...\n")
        self.process.terminate()
        if not self.process.waitForFinished(2500):
            self._log("⚠️ No terminó con terminate(). Haciendo kill()...\n")
            self.process.kill()
            self.process.waitForFinished(2500)

    def restart_bot(self) -> None:
        self.stop_bot()
        self.start_bot()

    def _read_proc(self, proc: QProcess) -> None:
        data = proc.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        self._log(text)

    def _update_running_label(self) -> None:
        running = bool(self.process and self.process.state() != QProcess.NotRunning)
        self.lbl_status.setText("Status: RUNNING" if running else "Status: STOPPED")
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    # ---------- Pickers ----------

    def pick_db(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Selecciona bot_config.db", BASE_DIR,
            "SQLite DB (*.db *.sqlite);;All (*.*)"
        )
        if path:
            self.db_path = path
            self._update_paths_label()
            self._refresh_all()

    def pick_app(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Selecciona app.py", BASE_DIR,
            "Python (*.py);;All (*.*)"
        )
        if path:
            self.app_path = path
            self._update_paths_label()

    def _update_paths_label(self) -> None:
        # Paths ocultas por UI (se mantienen internamente)
        return

    # ---------- Load/refresh ----------

    def _refresh_all(self) -> None:
        try:
            conn = connect_db(self.db_path)
            ensure_tables_exist(conn)

            pairs = load_pairs(conn)
            self._fill_pairs(pairs)
            self._fill_symbol_combo(pairs)

            if self.cmb_symbol.count() > 0:
                self._load_tp_for_symbol(self.cmb_symbol.currentText())
            else:
                self.tp_model.removeRows(0, self.tp_model.rowCount())

        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fill_pairs(self, rows: List[sqlite3.Row]) -> None:
        self.pairs_model.removeRows(0, self.pairs_model.rowCount())
        for r in rows:
            row_map = {k: r[k] for k in r.keys()}
            ost = str(row_map.get("order_size_type") or "")
            osv_ui = order_size_value_db_to_ui(ost, row_map.get("order_size_value"))

            items: List[QStandardItem] = []
            for col_name, _label, ftype in PAIRS_COLUMNS:
                if col_name == "order_size_value":
                    txt = osv_ui
                else:
                    txt = db_to_ui(row_map.get(col_name), ftype)
                it = QStandardItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                items.append(it)
            self.pairs_model.appendRow(items)

    def _fill_symbol_combo(self, pair_rows: List[sqlite3.Row]) -> None:
        syms = [str(r["symbol"]).upper() for r in pair_rows if str(r["symbol"] or "").strip() != ""]
        cur = self.cmb_symbol.currentText().upper().strip()

        self.cmb_symbol.blockSignals(True)
        self.cmb_symbol.clear()
        self.cmb_symbol.addItems(syms)
        if cur and cur in syms:
            self.cmb_symbol.setCurrentText(cur)
        self.cmb_symbol.blockSignals(False)

    def _load_tp_for_symbol(self, symbol: str) -> None:
        sym = (symbol or "").upper().strip()
        self.tp_model.removeRows(0, self.tp_model.rowCount())
        if not sym:
            return

        try:
            conn = connect_db(self.db_path)
            ensure_tables_exist(conn)
            tps = load_tp_levels(conn, sym)

            for r in tps:
                row_map = {k: r[k] for k in r.keys()}
                items: List[QStandardItem] = []
                for col_name, _label, ftype in TP_COLUMNS:
                    it = QStandardItem(db_to_ui(row_map.get(col_name), ftype))
                    it.setTextAlignment(Qt.AlignCenter)
                    items.append(it)
                self.tp_model.appendRow(items)

        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _on_pair_selected(self) -> None:
        idx = self.tbl_pairs.currentIndex()
        if not idx.isValid():
            return
        row = idx.row()
        sym_item = self.pairs_model.item(row, 0)
        if not sym_item:
            return
        sym = sym_item.text().upper().strip()
        if sym:
            self.cmb_symbol.setCurrentText(sym)

    # ---------- CRUD UI ----------

    def add_pair_row(self) -> None:
        defaults: Dict[str, str] = {
            "symbol": "NEWPAIRUSDT",
            "is_enabled": "1",
            "margin_mode": "ISOLATION",
            "leverage": "10",
            "order_size_type": "MARGIN_USDT",
            "order_size_value": "5",
            "sl_enabled": "1",
            "sl_pct": "1",
            "tp_enabled": "1",
            "breakeven_enabled": "0",
            "breakeven_trigger_pct": "1",
            "breakeven_offset_pct": "0",
            "trailing_enabled": "0",
            "trailing_trigger_pct": "2",
            "trailing_step_pct": "1",
            "trailing_distance_pct": "1",
            "trailing_move_immediately": "1",
            "same_side_policy": "IGNORE",
        }

        items: List[QStandardItem] = []
        for col_name, _label, _ftype in PAIRS_COLUMNS:
            it = QStandardItem(defaults.get(col_name, ""))
            it.setTextAlignment(Qt.AlignCenter)
            items.append(it)
        self.pairs_model.appendRow(items)
        self._log("➕ Added pair row (no guardado aún).\n")

    def delete_selected_pair(self) -> None:
        idx = self.tbl_pairs.currentIndex()
        if not idx.isValid():
            return
        row = idx.row()
        sym = (self.pairs_model.item(row, 0).text() if self.pairs_model.item(row, 0) else "").upper().strip()

        if not sym:
            self.pairs_model.removeRow(row)
            return

        if QMessageBox.question(self, "Confirm", f"Eliminar {sym} (pairs_config + tp_levels)?") != QMessageBox.Yes:
            return

        try:
            conn = connect_db(self.db_path)
            ensure_tables_exist(conn)
            with conn:
                delete_pair(conn, sym)
            self._log(f"🗑️ Deleted {sym} from DB.\n")
            self._refresh_all()
        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def add_tp_row(self) -> None:
        sym = self.cmb_symbol.currentText().upper().strip()
        if not sym:
            QMessageBox.warning(self, "Info", "No hay símbolo seleccionado.")
            return

        max_level = 0
        for r in range(self.tp_model.rowCount()):
            it = self.tp_model.item(r, 1)
            max_level = max(max_level, _to_int(it.text() if it else 0))
        next_level = max_level + 1

        defaults = {
            "symbol": sym,
            "level": str(next_level),
            "target_pct": "1",
            "close_frac": "30",
            "is_enabled": "1",
        }
        items: List[QStandardItem] = []
        for col_name, _label, _ftype in TP_COLUMNS:
            it = QStandardItem(defaults.get(col_name, ""))
            it.setTextAlignment(Qt.AlignCenter)
            items.append(it)
        self.tp_model.appendRow(items)
        self._log(f"➕ Added TP row for {sym} (no guardado aún).\n")

    def delete_selected_tp(self) -> None:
        idx = self.tbl_tp.currentIndex()
        if not idx.isValid():
            return
        row = idx.row()
        sym = (self.tp_model.item(row, 0).text() if self.tp_model.item(row, 0) else "").upper().strip()
        lvl = _to_int(self.tp_model.item(row, 1).text() if self.tp_model.item(row, 1) else 0)

        if not sym or lvl <= 0:
            self.tp_model.removeRow(row)
            return

        if QMessageBox.question(self, "Confirm", f"Eliminar TP {sym} level={lvl}?") != QMessageBox.Yes:
            return

        try:
            conn = connect_db(self.db_path)
            ensure_tables_exist(conn)
            with conn:
                delete_tp(conn, sym, lvl)
            self._log(f"🗑️ Deleted TP {sym} level={lvl} from DB.\n")
            self._load_tp_for_symbol(sym)
        except Exception as e:
            QMessageBox.critical(self, "DB Error", str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- Save ----------

    def save_all(self) -> None:
        try:
            conn = connect_db(self.db_path)
            ensure_tables_exist(conn)

            pairs_rows = self._collect_pairs_rows()
            tp_rows = self._collect_tp_rows()

            self._validate_pairs(pairs_rows)
            self._validate_tps(tp_rows)

            with conn:
                for pr in pairs_rows:
                    upsert_pair(conn, pr)
                for tr in tp_rows:
                    upsert_tp(conn, tr)

            self._log("✅ Save DB OK.\n")
            self._refresh_all()

        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _collect_pairs_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        idx_ost = next(i for i, c in enumerate(PAIRS_COLUMNS) if c[0] == "order_size_type")
        idx_osv = next(i for i, c in enumerate(PAIRS_COLUMNS) if c[0] == "order_size_value")

        for r in range(self.pairs_model.rowCount()):
            d: Dict[str, Any] = {}

            ost_item = self.pairs_model.item(r, idx_ost)
            ost = (ost_item.text() if ost_item else "").upper().strip()

            for cidx, (col_name, _label, ftype) in enumerate(PAIRS_COLUMNS):
                it = self.pairs_model.item(r, cidx)
                txt = (it.text() if it else "").strip()

                if col_name == "symbol":
                    d["symbol"] = txt.upper()
                    continue

                if cidx == idx_osv:
                    d["order_size_value"] = order_size_value_ui_to_db(ost, txt)
                    continue

                d[col_name] = ui_to_db(txt, ftype)

            rows.append(d)
        return rows

    def _collect_tp_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in range(self.tp_model.rowCount()):
            d: Dict[str, Any] = {}
            for cidx, (col_name, _label, ftype) in enumerate(TP_COLUMNS):
                it = self.tp_model.item(r, cidx)
                txt = (it.text() if it else "").strip()
                if col_name == "symbol":
                    d["symbol"] = txt.upper()
                else:
                    d[col_name] = ui_to_db(txt, ftype)
            rows.append(d)
        return rows

    def _validate_pairs(self, pairs: List[Dict[str, Any]]) -> None:
        seen = set()
        for p in pairs:
            sym = (p.get("symbol") or "").upper().strip()
            if not sym:
                raise ValueError("pairs_config: hay una fila con symbol vacío.")
            if sym in seen:
                raise ValueError(f"pairs_config: symbol duplicado: {sym}")
            seen.add(sym)

            mm = str(p.get("margin_mode") or "").upper().strip()
            if mm not in ("ISOLATION", "CROSS"):
                raise ValueError(f"{sym}: margin_mode inválido: {mm}")

            lev = int(p.get("leverage") or 0)
            if lev < 1:
                raise ValueError(f"{sym}: leverage inválido: {lev}")

            ost = str(p.get("order_size_type") or "").upper().strip()
            if ost not in ("MARGIN_USDT", "NOTIONAL_USDT", "PCT_BALANCE"):
                raise ValueError(f"{sym}: order_size_type inválido: {ost}")

            for k in (
                "sl_pct",
                "breakeven_trigger_pct", "breakeven_offset_pct",
                "trailing_trigger_pct",
                "trailing_step_pct", "trailing_distance_pct",
            ):
                v = float(p.get(k) or 0.0)
                if v < 0 or v > 1:
                    raise ValueError(f"{sym}: {k} fuera de rango 0..1 (BD). Valor={v}")

            ssp = str(p.get("same_side_policy") or "").upper().strip()
            if ssp not in ("IGNORE", "RESET_ORDERS"):
                raise ValueError(f"{sym}: same_side_policy inválido: {ssp}")

    def _validate_tps(self, tps: List[Dict[str, Any]]) -> None:
        for t in tps:
            sym = (t.get("symbol") or "").upper().strip()
            lvl = int(t.get("level") or 0)
            if not sym:
                raise ValueError("tp_levels: hay una fila con symbol vacío.")
            if lvl <= 0:
                raise ValueError(f"{sym}: TP level inválido: {lvl}")

            target = float(t.get("target_pct") or 0.0)
            closef = float(t.get("close_frac") or 0.0)
            if target <= 0 or target > 1:
                raise ValueError(f"{sym} TP{lvl}: target_pct fuera de rango (0..1). Valor={target}")
            if closef <= 0 or closef > 1:
                raise ValueError(f"{sym} TP{lvl}: close_frac fuera de rango (0..1). Valor={closef}")


def main() -> None:
    app = QApplication(sys.argv)
    w = BotGUI()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
