"""ESP Flash Backup Tool — PyQt6 GUI around esptool.

Reads/writes full flash images on ESP8266 and ESP32 family chips
(ESP32, ESP32-C3, -S2, -S3, …). Chip and flash size are auto-detected;
all heavy lifting goes through `python -m esptool` in a QProcess so the
UI stays responsive and operations are cancellable.
"""

import glob
import mmap
import os
import re
import sys

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QProcess

import serial
import serial.tools.list_ports


def list_serial_ports() -> list[tuple[str, str]]:
    """Return [(device_path, description)] tuples.

    pyserial's enumeration occasionally misses freshly-attached USB UARTs
    (race with udev, missing permissions on sysfs). We merge in a glob
    fallback so /dev/ttyUSB0, /dev/ttyACM0 etc. still appear.
    """
    seen: dict[str, str] = {}
    try:
        for p in serial.tools.list_ports.comports():
            seen[p.device] = p.description or "n/a"
    except Exception:
        pass

    if sys.platform.startswith("linux"):
        patterns = ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/serial/by-id/*")
    elif sys.platform == "darwin":
        patterns = ("/dev/cu.usb*", "/dev/cu.SLAB*", "/dev/cu.wchusb*",
                    "/dev/tty.usb*", "/dev/tty.SLAB*")
    else:
        patterns = ()
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            seen.setdefault(path, "(detected)")

    return sorted(seen.items())


FLASH_SIZE_MAP: dict[str, int | None] = {
    "Auto-detect": None,
    "256 KB":  0x40000,
    "512 KB":  0x80000,
    "1 MB":    0x100000,
    "2 MB":    0x200000,
    "4 MB":    0x400000,
    "8 MB":    0x800000,
    "16 MB":   0x1000000,
    "32 MB":   0x2000000,
}

BAUD_RATES = ["115200", "230400", "460800", "921600", "1500000"]
DEFAULT_BAUD = "460800"

CHIP_CHOICES = [
    ("Auto", None),
    ("ESP8266", "esp8266"),
    ("ESP32",   "esp32"),
    ("ESP32-S2", "esp32s2"),
    ("ESP32-S3", "esp32s3"),
    ("ESP32-C3", "esp32c3"),
    ("ESP32-C2", "esp32c2"),
    ("ESP32-C6", "esp32c6"),
    ("ESP32-H2", "esp32h2"),
]


class HexModel(QtCore.QAbstractTableModel):
    HEADERS = ("Offset", "Hex", "ASCII")
    BYTES_PER_ROW = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fh = None
        self._mm: mmap.mmap | None = None
        self._size = 0

    def open(self, path: str):
        self.beginResetModel()
        self.close()
        self._fh = open(path, "rb")
        self._size = os.path.getsize(path)
        if self._size > 0:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.endResetModel()

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._size = 0

    def size(self) -> int:
        return self._size

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid():
            return 0
        return (self._size + self.BYTES_PER_ROW - 1) // self.BYTES_PER_ROW

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orientation == QtCore.Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self._mm is None:
            return None
        row, col = index.row(), index.column()
        offset = row * self.BYTES_PER_ROW
        end = min(offset + self.BYTES_PER_ROW, self._size)
        chunk = self._mm[offset:end]

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return f"{offset:08X}"
            if col == 1:
                cells = [f"{b:02X}" for b in chunk]
                while len(cells) < self.BYTES_PER_ROW:
                    cells.append("  ")
                return " ".join(cells[:8]) + "  " + " ".join(cells[8:])
            if col == 2:
                return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        elif role == QtCore.Qt.ItemDataRole.TextAlignmentRole and col == 0:
            return int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return None


class HexViewer(QtWidgets.QDialog):
    def __init__(self, parent=None, path: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Hex Viewer")
        self.resize(880, 600)

        self.model = HexModel(self)

        v = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.path_lbl = QtWidgets.QLabel("(no file)")
        self.path_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.path_lbl, 1)

        open_btn = QtWidgets.QPushButton("Open…")
        open_btn.clicked.connect(self._open_dialog)
        bar.addWidget(open_btn)

        bar.addWidget(QtWidgets.QLabel("Goto:"))
        self.goto_edit = QtWidgets.QLineEdit()
        self.goto_edit.setPlaceholderText("0x1000")
        self.goto_edit.setMaximumWidth(120)
        self.goto_edit.returnPressed.connect(self._goto)
        bar.addWidget(self.goto_edit)
        go_btn = QtWidgets.QPushButton("Go")
        go_btn.clicked.connect(self._goto)
        bar.addWidget(go_btn)
        v.addLayout(bar)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.table.setFont(mono)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)

        self.size_lbl = QtWidgets.QLabel("")
        v.addWidget(self.size_lbl)

        if path:
            self.open_file(path)

    def _open_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open file", "", "Binary (*.bin);;All files (*)"
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str):
        try:
            self.model.open(path)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))
            return
        self.path_lbl.setText(path)
        self.size_lbl.setText(f"{self.model.size():,} bytes")
        self.setWindowTitle(f"Hex Viewer — {os.path.basename(path)}")

    def _goto(self):
        text = self.goto_edit.text().strip()
        if not text:
            return
        try:
            offset = int(text, 0)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Hex Viewer", f"Invalid offset: {text!r}")
            return
        row = offset // HexModel.BYTES_PER_ROW
        if 0 <= row < self.model.rowCount():
            idx = self.model.index(row, 0)
            self.table.scrollTo(idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop)
            self.table.selectRow(row)

    def closeEvent(self, e):
        self.model.close()
        super().closeEvent(e)


class SerialReader(QtCore.QThread):
    """Background reader: pulls bytes off a pyserial Serial and signals them."""
    bytes_received = QtCore.pyqtSignal(bytes)
    error = QtCore.pyqtSignal(str)

    def __init__(self, ser: "serial.Serial", parent=None):
        super().__init__(parent)
        self._ser = ser
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                n = self._ser.in_waiting
                if n:
                    data = self._ser.read(n)
                    if data:
                        self.bytes_received.emit(bytes(data))
                else:
                    self.msleep(15)
            except (serial.SerialException, OSError) as e:
                self.error.emit(str(e))
                return

    def stop(self):
        self._stop = True


class SerialMonitor(QtWidgets.QDialog):
    """minicom-style serial monitor: live read, send, reset, hex/timestamps."""
    BAUDS = ["9600", "19200", "38400", "57600", "74880", "115200",
             "230400", "460800", "921600", "1500000"]
    EOLS = [("None", b""), ("LF (\\n)", b"\n"),
            ("CR (\\r)", b"\r"), ("CRLF", b"\r\n")]

    def __init__(self, parent=None, port: str | None = None, baud: str = "115200"):
        super().__init__(parent)
        self.setWindowTitle("Serial Monitor")
        self.resize(880, 620)

        self.ser: serial.Serial | None = None
        self.reader: SerialReader | None = None
        self._line_buf = b""
        self._line_start = True

        self._build_ui()
        self._populate_ports(prefer=port)
        self.baud_combo.setCurrentText(baud)
        self._update_ui_state()

    def _build_ui(self):
        v = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Port:"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(280)
        top.addWidget(self.port_combo, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._populate_ports())
        top.addWidget(refresh)

        top.addWidget(QtWidgets.QLabel("Baud:"))
        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(self.BAUDS)
        self.baud_combo.setCurrentText("115200")
        top.addWidget(self.baud_combo)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        top.addWidget(self.connect_btn)
        v.addLayout(top)

        bar = QtWidgets.QHBoxLayout()
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._clear)
        bar.addWidget(clear)
        self.reset_btn = QtWidgets.QPushButton("Reset Device")
        self.reset_btn.setToolTip("Pulse RTS low/high to reset the chip")
        self.reset_btn.clicked.connect(self._reset_device)
        bar.addWidget(self.reset_btn)
        save = QtWidgets.QPushButton("Save Log…")
        save.clicked.connect(self._save_log)
        bar.addWidget(save)
        bar.addStretch(1)
        self.echo_chk = QtWidgets.QCheckBox("Local echo")
        self.timestamps_chk = QtWidgets.QCheckBox("Timestamps")
        self.autoscroll_chk = QtWidgets.QCheckBox("Auto-scroll")
        self.autoscroll_chk.setChecked(True)
        self.hex_chk = QtWidgets.QCheckBox("Hex")
        for w in (self.echo_chk, self.timestamps_chk, self.autoscroll_chk, self.hex_chk):
            bar.addWidget(w)
        v.addLayout(bar)

        self.display = QtWidgets.QPlainTextEdit()
        self.display.setReadOnly(True)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.display.setFont(mono)
        self.display.setMaximumBlockCount(20000)
        v.addWidget(self.display, 1)

        send = QtWidgets.QHBoxLayout()
        send.addWidget(QtWidgets.QLabel("Send:"))
        self.send_edit = QtWidgets.QLineEdit()
        self.send_edit.returnPressed.connect(self._send)
        send.addWidget(self.send_edit, 1)
        self.eol_combo = QtWidgets.QComboBox()
        for label, _ in self.EOLS:
            self.eol_combo.addItem(label)
        self.eol_combo.setCurrentIndex(1)  # LF default
        send.addWidget(self.eol_combo)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        send.addWidget(self.send_btn)
        v.addLayout(send)

        self.status = QtWidgets.QLabel("Disconnected")
        v.addWidget(self.status)

    def _populate_ports(self, prefer: str | None = None):
        prev = self._current_port_text() if hasattr(self, "port_combo") else None
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for path, desc in list_serial_ports():
            self.port_combo.addItem(f"{path}  ({desc})", path)
        self.port_combo.blockSignals(False)

        target = prefer or prev
        if target:
            for i in range(self.port_combo.count()):
                if self.port_combo.itemData(i) == target:
                    self.port_combo.setCurrentIndex(i)
                    return
            self.port_combo.setEditText(target)

    def _current_port_text(self) -> str:
        text = self.port_combo.currentText().strip()
        if "  (" in text:
            text = text.split("  (", 1)[0]
        return text

    def _update_ui_state(self):
        connected = self.ser is not None
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.reset_btn.setEnabled(connected)
        self.send_edit.setEnabled(connected)
        self.send_btn.setEnabled(connected)
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)

    def _toggle_connection(self):
        if self.ser is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        port = self._current_port_text()
        if not port:
            QtWidgets.QMessageBox.warning(self, "Serial Monitor", "Pick or type a port.")
            return
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Serial Monitor", "Invalid baud rate.")
            return
        try:
            ser = serial.Serial(port, baud, timeout=0)
            ser.dtr = False
            ser.rts = True
        except (serial.SerialException, OSError) as e:
            QtWidgets.QMessageBox.critical(self, "Serial Monitor", f"Open failed:\n{e}")
            return
        self.ser = ser
        self.reader = SerialReader(ser, self)
        self.reader.bytes_received.connect(self._on_bytes)
        self.reader.error.connect(self._on_reader_error)
        self.reader.start()
        self.status.setText(f"Connected: {port} @ {baud}")
        self._update_ui_state()

    def _disconnect(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(2000)
            self.reader = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.status.setText("Disconnected")
        self._update_ui_state()

    def _on_bytes(self, data: bytes):
        if self.hex_chk.isChecked():
            self._append_text(" ".join(f"{b:02X}" for b in data) + " ")
            return
        if self.timestamps_chk.isChecked():
            self._append_with_timestamps(data)
            return
        self._append_text(data.decode("utf-8", errors="replace"))

    def _append_with_timestamps(self, data: bytes):
        self._line_buf += data
        while b"\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split(b"\n", 1)
            ts = QtCore.QDateTime.currentDateTime().toString("hh:mm:ss.zzz")
            text = line.rstrip(b"\r").decode("utf-8", errors="replace")
            self._append_text(f"[{ts}] {text}\n")

    def _on_reader_error(self, msg: str):
        self._append_text(f"\n[serial error] {msg}\n")
        self._disconnect()

    def _append_text(self, text: str):
        cursor = self.display.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if self.autoscroll_chk.isChecked():
            self.display.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _send(self):
        if not self.ser:
            return
        text = self.send_edit.text()
        eol = self.EOLS[self.eol_combo.currentIndex()][1]
        payload = text.encode("utf-8", errors="replace") + eol
        try:
            self.ser.write(payload)
        except (serial.SerialException, OSError) as e:
            self._append_text(f"\n[write error] {e}\n")
            self._disconnect()
            return
        if self.echo_chk.isChecked():
            self._append_text(text + eol.decode("utf-8", errors="replace"))
        self.send_edit.clear()

    def _reset_device(self):
        if not self.ser:
            return
        try:
            self.ser.rts = False
            QtCore.QThread.msleep(50)
            self.ser.rts = True
        except (serial.SerialException, OSError) as e:
            self._append_text(f"\n[reset error] {e}\n")

    def _clear(self):
        self.display.clear()
        self._line_buf = b""

    def _save_log(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save monitor log", "monitor.log",
            "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.display.toPlainText())
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Save log", str(e))

    def closeEvent(self, e):
        self._disconnect()
        super().closeEvent(e)


class EspFlasher(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP Flash Backup Tool")
        self.resize(760, 720)

        self.process: QProcess | None = None
        self.current_op: str | None = None

        self.detected_flash_bytes: int | None = None
        self._chain_after_detect: tuple[str, ...] | None = None
        self._pending_restore: tuple[int, str] | None = None

        self._build_ui()
        self._build_menus()
        self.refresh_ports()

    def _build_menus(self):
        tools = self.menuBar().addMenu("&Tools")
        mon = QtGui.QAction("Serial Monitor…", self)
        mon.setShortcut("Ctrl+M")
        mon.triggered.connect(self._open_monitor)
        tools.addAction(mon)
        hx = QtGui.QAction("Hex Viewer…", self)
        hx.setShortcut("Ctrl+H")
        hx.triggered.connect(self._open_hex_viewer)
        tools.addAction(hx)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # --- Connection ---
        conn = QtWidgets.QGroupBox("Connection")
        g = QtWidgets.QGridLayout(conn)

        g.addWidget(QtWidgets.QLabel("Port:"), 0, 0)
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(280)
        self.port_combo.lineEdit().setPlaceholderText("/dev/ttyUSB0 or COM3")
        g.addWidget(self.port_combo, 0, 1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        g.addWidget(self.refresh_btn, 0, 2)

        g.addWidget(QtWidgets.QLabel("Baud:"), 1, 0)
        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText(DEFAULT_BAUD)
        g.addWidget(self.baud_combo, 1, 1)
        self.detect_btn = QtWidgets.QPushButton("Detect Chip")
        self.detect_btn.clicked.connect(self.start_detect)
        g.addWidget(self.detect_btn, 1, 2)

        g.addWidget(QtWidgets.QLabel("Force chip:"), 2, 0)
        self.chip_override = QtWidgets.QComboBox()
        for label, _ in CHIP_CHOICES:
            self.chip_override.addItem(label)
        g.addWidget(self.chip_override, 2, 1)
        self.monitor_btn = QtWidgets.QPushButton("Serial Monitor…")
        self.monitor_btn.clicked.connect(self._open_monitor)
        g.addWidget(self.monitor_btn, 2, 2)

        self.chip_lbl = QtWidgets.QLabel("Chip: —")
        self.mac_lbl = QtWidgets.QLabel("MAC: —")
        self.flash_lbl = QtWidgets.QLabel("Flash size: —")
        for w, row in ((self.chip_lbl, 3), (self.mac_lbl, 4), (self.flash_lbl, 5)):
            f = w.font(); f.setBold(True); w.setFont(f)
            g.addWidget(w, row, 0, 1, 3)

        root.addWidget(conn)

        # --- Backup ---
        backup = QtWidgets.QGroupBox("Backup  (read flash → file)")
        b = QtWidgets.QGridLayout(backup)

        b.addWidget(QtWidgets.QLabel("Output file:"), 0, 0)
        self.backup_path = QtWidgets.QLineEdit()
        b.addWidget(self.backup_path, 0, 1)
        bb = QtWidgets.QPushButton("Browse…")
        bb.clicked.connect(self._pick_backup_file)
        b.addWidget(bb, 0, 2)
        bv = QtWidgets.QPushButton("View")
        bv.clicked.connect(self._view_backup)
        b.addWidget(bv, 0, 3)

        b.addWidget(QtWidgets.QLabel("Flash size:"), 1, 0)
        self.backup_size_combo = QtWidgets.QComboBox()
        self.backup_size_combo.addItems(FLASH_SIZE_MAP.keys())
        b.addWidget(self.backup_size_combo, 1, 1)

        b.addWidget(QtWidgets.QLabel("Start address:"), 2, 0)
        self.backup_addr = QtWidgets.QLineEdit("0x0")
        b.addWidget(self.backup_addr, 2, 1)

        self.backup_btn = QtWidgets.QPushButton("Backup Flash")
        self.backup_btn.clicked.connect(self.start_backup)
        b.addWidget(self.backup_btn, 3, 0, 1, 3)
        root.addWidget(backup)

        # --- Restore ---
        restore = QtWidgets.QGroupBox("Restore  (file → flash)")
        r = QtWidgets.QGridLayout(restore)

        r.addWidget(QtWidgets.QLabel("Firmware file:"), 0, 0)
        self.restore_path = QtWidgets.QLineEdit()
        r.addWidget(self.restore_path, 0, 1)
        rb = QtWidgets.QPushButton("Browse…")
        rb.clicked.connect(self._pick_restore_file)
        r.addWidget(rb, 0, 2)
        rv = QtWidgets.QPushButton("View")
        rv.clicked.connect(self._view_restore)
        r.addWidget(rv, 0, 3)

        r.addWidget(QtWidgets.QLabel("Address:"), 1, 0)
        self.restore_addr = QtWidgets.QLineEdit("0x0")
        r.addWidget(self.restore_addr, 1, 1)
        self.erase_chk = QtWidgets.QCheckBox("Erase entire flash first")
        self.erase_chk.setChecked(True)
        r.addWidget(self.erase_chk, 1, 2)

        self.restore_btn = QtWidgets.QPushButton("Restore Flash")
        self.restore_btn.clicked.connect(self.start_restore)
        r.addWidget(self.restore_btn, 2, 0, 1, 3)
        root.addWidget(restore)

        # --- Progress + cancel ---
        prow = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        prow.addWidget(self.progress, 1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        prow.addWidget(self.cancel_btn)
        root.addLayout(prow)

        # --- Log ---
        log_box = QtWidgets.QGroupBox("Log")
        lv = QtWidgets.QVBoxLayout(log_box)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        mono = QtGui.QFont("monospace")
        mono.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.log.setFont(mono)
        lv.addWidget(self.log)
        root.addWidget(log_box, 1)

        self.statusBar().showMessage("Ready")

    def refresh_ports(self):
        prev = self._port_text()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        ports = list_serial_ports()
        for path, desc in ports:
            self.port_combo.addItem(f"{path}  ({desc})", path)
        self.port_combo.blockSignals(False)

        if prev:
            for i in range(self.port_combo.count()):
                if self.port_combo.itemData(i) == prev:
                    self.port_combo.setCurrentIndex(i)
                    break
            else:
                self.port_combo.setEditText(prev)
        if not ports:
            self.append_log(
                "No serial ports detected. Plug in a device and click Refresh, "
                "or type the path (e.g. /dev/ttyUSB0) directly.\n"
            )

    def _port_text(self) -> str:
        text = self.port_combo.currentText().strip()
        if "  (" in text:
            text = text.split("  (", 1)[0]
        return text

    def selected_port(self) -> str | None:
        return self._port_text() or None

    def selected_chip_flag(self) -> str | None:
        return CHIP_CHOICES[self.chip_override.currentIndex()][1]

    def _pick_backup_file(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save backup as", "flash_backup.bin", "Binary (*.bin);;All files (*)"
        )
        if path:
            self.backup_path.setText(path)

    def _pick_restore_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose firmware file", "", "Binary (*.bin);;All files (*)"
        )
        if path:
            self.restore_path.setText(path)

    def _view_backup(self):
        self._open_hex_viewer(self.backup_path.text().strip() or None)

    def _view_restore(self):
        self._open_hex_viewer(self.restore_path.text().strip() or None)

    def _open_hex_viewer(self, path: str | None = None):
        if path and not os.path.isfile(path):
            QtWidgets.QMessageBox.warning(self, "Hex Viewer", f"File not found:\n{path}")
            path = None
        viewer = HexViewer(self, path=path)
        viewer.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.show()

    def _open_monitor(self):
        port = self._port_text() or None
        mon = SerialMonitor(self, port=port, baud="115200")
        mon.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        mon.show()

    def _esptool_argv(self, *args, include_baud=True) -> list[str]:
        port = self.selected_port()
        if not port:
            raise RuntimeError("No serial port selected.")
        argv = ["-m", "esptool"]
        chip = self.selected_chip_flag()
        if chip:
            argv += ["--chip", chip]
        argv += ["--port", port]
        if include_baud:
            argv += ["--baud", self.baud_combo.currentText()]
        argv += list(args)
        return argv

    def _start(self, op: str, argv: list[str]):
        if self.process is not None:
            QtWidgets.QMessageBox.warning(self, "Busy", "An operation is already running.")
            return
        self.current_op = op
        self.progress.setValue(0)
        self.set_running(True)
        self.append_log(f"\n>>> {sys.executable} {' '.join(argv)}\n")

        proc = QProcess(self)
        proc.setProgram(sys.executable)
        proc.setArguments(argv)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self.process = proc
        proc.start()

    def cancel(self):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.append_log("\n[cancel] terminating esptool…\n")
            self.process.kill()

    def start_detect(self):
        try:
            argv = self._esptool_argv("flash_id", include_baud=False)
        except RuntimeError as e:
            self._error(str(e)); return
        self.detected_flash_bytes = None
        self.chip_lbl.setText("Chip: …")
        self.mac_lbl.setText("MAC: …")
        self.flash_lbl.setText("Flash size: …")
        self._start("detect", argv)

    def start_backup(self):
        out = self.backup_path.text().strip()
        if not out:
            self._error("Pick an output file."); return
        size_label = self.backup_size_combo.currentText()
        size_bytes = FLASH_SIZE_MAP[size_label]
        if size_bytes is None:
            if self.detected_flash_bytes is not None:
                self._do_backup(out, self.detected_flash_bytes)
            else:
                self.append_log("[backup] flash size = Auto, running detection first…\n")
                self._chain_after_detect = ("backup", out)
                self.start_detect()
            return
        self._do_backup(out, size_bytes)

    def _do_backup(self, out: str, size_bytes: int):
        addr = self._parse_addr(self.backup_addr.text(), default=0)
        if addr is None:
            self._error("Invalid start address."); return
        try:
            argv = self._esptool_argv("read_flash", hex(addr), str(size_bytes), out)
        except RuntimeError as e:
            self._error(str(e)); return
        self.append_log(f"[backup] reading {size_bytes} bytes from {hex(addr)} → {out}\n")
        self._start("backup", argv)

    def start_restore(self):
        path = self.restore_path.text().strip()
        if not path or not os.path.isfile(path):
            self._error("Pick an existing firmware file."); return
        addr = self._parse_addr(self.restore_addr.text(), default=0)
        if addr is None:
            self._error("Invalid address."); return
        if self.erase_chk.isChecked():
            self._pending_restore = (addr, path)
            try:
                argv = self._esptool_argv("erase_flash")
            except RuntimeError as e:
                self._error(str(e)); return
            self.append_log("[restore] erasing flash first…\n")
            self._start("erase", argv)
        else:
            self._do_restore(addr, path)

    def _do_restore(self, addr: int, path: str):
        try:
            argv = self._esptool_argv("write_flash", hex(addr), path)
        except RuntimeError as e:
            self._error(str(e)); return
        self.append_log(f"[restore] writing {path} to {hex(addr)}\n")
        self._start("restore", argv)

    def _on_stdout(self):
        if not self.process:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        for piece in re.split(r"[\r\n]+", data):
            if not piece.strip():
                continue
            self.append_log(piece + "\n")
            self._scan_line(piece)

    def _scan_line(self, line: str):
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
        if m:
            try:
                pct = int(float(m.group(1)))
                self.progress.setValue(max(0, min(100, pct)))
            except ValueError:
                pass

        m = re.search(r"Chip is\s+(.+)$", line)
        if m:
            self.chip_lbl.setText(f"Chip: {m.group(1).strip()}")

        m = re.search(r"\bMAC:\s*([0-9a-fA-F:]{11,})", line)
        if m:
            self.mac_lbl.setText(f"MAC: {m.group(1)}")

        m = re.search(r"flash size:\s*([0-9]+)\s*MB", line, re.IGNORECASE)
        if m:
            mb = int(m.group(1))
            self.detected_flash_bytes = mb * 1024 * 1024
            self.flash_lbl.setText(f"Flash size: {mb} MB")
            idx = self.backup_size_combo.findText(f"{mb} MB")
            if idx >= 0:
                self.backup_size_combo.setCurrentIndex(idx)

    def _on_finished(self, exit_code: int, _exit_status):
        op = self.current_op
        self.process = None
        self.current_op = None
        self.set_running(False)

        if exit_code == 0:
            self.append_log(f"[{op}] done.\n")
            self.statusBar().showMessage(f"{op}: success", 5000)
            if op == "detect" and self._chain_after_detect:
                kind, *rest = self._chain_after_detect
                self._chain_after_detect = None
                if kind == "backup":
                    if self.detected_flash_bytes:
                        self._do_backup(rest[0], self.detected_flash_bytes)
                    else:
                        self._error("Flash size auto-detect failed; pick a size manually.")
            elif op == "erase" and self._pending_restore:
                addr, path = self._pending_restore
                self._pending_restore = None
                self._do_restore(addr, path)
        else:
            self.append_log(f"[{op}] FAILED (exit {exit_code}).\n")
            self.statusBar().showMessage(f"{op}: failed", 5000)
            self._chain_after_detect = None
            self._pending_restore = None

    def _on_error(self, _err):
        msg = self.process.errorString() if self.process else "process error"
        self.append_log(f"[process error] {msg}\n")

    def append_log(self, text: str):
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def set_running(self, running: bool):
        for w in (self.detect_btn, self.backup_btn, self.restore_btn,
                  self.refresh_btn, self.port_combo, self.baud_combo,
                  self.chip_override, self.monitor_btn):
            w.setEnabled(not running)
        self.cancel_btn.setEnabled(running)

    def _error(self, msg: str):
        QtWidgets.QMessageBox.critical(self, "Error", msg)

    @staticmethod
    def _parse_addr(text: str, default: int = 0) -> int | None:
        text = text.strip()
        if not text:
            return default
        try:
            return int(text, 0)
        except ValueError:
            return None


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = EspFlasher()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
